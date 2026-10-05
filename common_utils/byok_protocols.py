"""PubChat local BYOK REST adapters (stdlib only; no remote logging).

Protocols: OpenAI Chat Completions, Gemini generateContent, Anthropic Messages.
API keys are only placed in request headers; redirects are never followed.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from urllib import error, parse, request

PROTOCOLS = {"openai", "gemini", "anthropic"}
VERSION = "1.0.0"


class BYOKError(RuntimeError):
    """Safe error: never include upstream bodies, request headers or credentials."""


class ConfigError(ValueError):
    pass


def _text(value, label, max_length=512):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError(f"{label} 必须填写，且不能包含首尾空格")
    if len(value) > max_length or any(ord(c) < 32 for c in value):
        raise ConfigError(f"{label} 格式无效")
    return value


def validate_url(value):
    value = _text(value, "Base URL", 2048)
    try:
        url = parse.urlsplit(value)
        port = url.port
    except ValueError:
        raise ConfigError("Base URL 格式无效") from None
    if url.scheme not in {"https", "http"} or not url.hostname:
        raise ConfigError("Base URL 必须是 http:// 或 https:// 地址")
    if url.username or url.password or url.query or url.fragment:
        raise ConfigError("Base URL 不得包含账号密码、查询参数或锚点；Key 请填在独立输入框")
    if port is not None and not 0 < port < 65536:
        raise ConfigError("Base URL 端口无效")
    # HTTP is explicitly permitted for local/self-hosted model servers.
    if value.rstrip("/").endswith(("/chat/completions", "/messages", "/embeddings")) or ":generateContent" in value:
        raise ConfigError("请填写 API Base URL，而不是完整的调用端点")
    return value


def normalize_keys(value, label="API Key"):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} 必须填写")
    result = []
    for key in value:
        if not isinstance(key, str) or not key or key != key.strip() or len(key) > 8192 or any(ord(c) < 32 for c in key):
            raise ConfigError(f"{label} 格式无效")
        result.append(key)
    return result


def validate_config(llm_config):
    if not isinstance(llm_config, dict) or llm_config.get("model") != "byok":
        raise ConfigError("BYOK 配置缺失")
    raw = llm_config.get("byok")
    if not isinstance(raw, dict) or raw.get("protocol") not in PROTOCOLS:
        raise ConfigError("请选择 OpenAI-compatible、Gemini 或 Anthropic 协议")
    cfg = {
        "protocol": raw["protocol"],
        "base_url": validate_url(raw.get("base_url")),
        "model_pro": _text(raw.get("model_pro"), "主模型 ID"),
        "model_flash": _text(raw.get("model_flash") or raw.get("model_pro"), "快速模型 ID"),
    }
    keys = normalize_keys(llm_config.get("api"))
    emb = raw.get("embedding")
    if not isinstance(emb, dict) or not isinstance(emb.get("enabled"), bool):
        raise ConfigError("请明确设置是否启用 embedding；不会自动关闭语义预筛选")
    if emb["enabled"]:
        if emb.get("protocol") not in {"openai", "gemini"}:
            raise ConfigError("Embedding 仅支持 OpenAI-compatible 或 Gemini 协议")
        cfg["embedding"] = {
            "enabled": True,
            "protocol": emb["protocol"],
            "base_url": validate_url(emb.get("base_url")),
            "model": _text(emb.get("model"), "Embedding 模型 ID"),
            "api": normalize_keys(emb.get("api"), "Embedding API Key"),
        }
    else:
        cfg["embedding"] = {"enabled": False}
    return {"model": "byok", "api": keys, "pubmed_api": llm_config.get("pubmed_api") or [], "byok": cfg}


def split_secrets(config):
    """Return database-safe settings plus task-scoped Redis credentials."""
    config = validate_config(config)
    safe = json.loads(json.dumps(config["byok"]))
    embedding_keys = safe["embedding"].pop("api", [])
    credentials = {"api": config["api"], "embedding_api": embedding_keys, "pubmed_api": config["pubmed_api"]}
    return safe, credentials


def restore_config(safe, credentials):
    raw = json.loads(json.dumps(safe))
    if raw["embedding"]["enabled"]:
        raw["embedding"]["api"] = credentials.get("embedding_api")
    return validate_config({"model": "byok", "api": credentials.get("api"), "pubmed_api": credentials.get("pubmed_api") or [], "byok": raw})


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(url, payload, headers, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise BYOKError("API 响应超过安全大小限制")
        return json.loads(raw), None
    except error.HTTPError as exc:
        # Inspect only for bounded parameter compatibility; do not print or return body.
        raw = exc.read(16384).decode("utf-8", errors="replace")
        fallback = exc.code == 400 and "max_tokens" in raw and "max_completion_tokens" in raw
        return None, (exc.code, fallback)
    except (error.URLError, TimeoutError, OSError):
        raise BYOKError("API 网络连接失败或超时；检查地址、容器网络和服务状态") from None
    except (json.JSONDecodeError, UnicodeError):
        raise BYOKError("API 返回的不是有效 JSON；请检查 API 协议及 Base URL") from None


def _checked_post(url, payload, headers, timeout):
    result, failure = _post(url, payload, headers, timeout)
    if failure:
        raise BYOKError(f"API 返回 HTTP {failure[0]}；检查协议、URL、Key、模型及额度")
    if not isinstance(result, dict):
        raise BYOKError("API 响应结构无效")
    return result


def _gemini_path(base_url, model, operation):
    base = base_url.rstrip("/")
    if not base.endswith(("/v1", "/v1beta", "/v1alpha")):
        base += "/v1beta"
    resource = model if model.startswith("models/") else "models/" + model
    return base + "/" + parse.quote(resource, safe="/") + ":" + operation


def _anthropic_path(base_url):
    base = base_url.rstrip("/")
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


@dataclass
class Generation:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0


def generate(config, key, model, prompt, max_tokens=8192, timeout=90):
    protocol = config["protocol"]
    base = config["base_url"].rstrip("/")
    limit = max(1, min(int(max_tokens or 8192), 65536))
    if protocol == "openai":
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False, "max_tokens": limit}
        data, failure = _post(base + "/chat/completions", body, {"Authorization": "Bearer " + key}, timeout)
        if failure and failure[1]:
            body["max_completion_tokens"] = body.pop("max_tokens")
            data, failure = _post(base + "/chat/completions", body, {"Authorization": "Bearer " + key}, timeout)
        if failure:
            raise BYOKError(f"OpenAI-compatible API 返回 HTTP {failure[0]}；检查 URL、Key、模型和额度")
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise BYOKError("模型输出被 token 上限截断；请调整模型或输出长度")
            content = choice["message"]["content"]
            if isinstance(content, list):
                content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            usage = data.get("usage") or {}
            result = Generation(content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BYOKError("响应不符合 OpenAI Chat Completions 协议；Responses API 需要单独适配") from None
    elif protocol == "gemini":
        data = _checked_post(_gemini_path(base, model, "generateContent"), {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": limit},
        }, {"x-goog-api-key": key}, timeout)
        try:
            candidate = data["candidates"][0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise BYOKError("Gemini 输出被 token 上限截断")
            text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
            usage = data.get("usageMetadata") or {}
            result = Generation(text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))
        except (KeyError, IndexError, TypeError, AttributeError):
            raise BYOKError("响应不符合 Gemini generateContent 协议，或被安全策略阻止") from None
    elif protocol == "anthropic":
        data = _checked_post(_anthropic_path(base), {
            "model": model, "max_tokens": limit,
            "messages": [{"role": "user", "content": prompt}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        try:
            if data.get("stop_reason") == "max_tokens":
                raise BYOKError("Anthropic 输出被 token 上限截断")
            text = "".join(p.get("text", "") for p in data["content"] if p.get("type") == "text")
            usage = data.get("usage") or {}
            result = Generation(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        except (KeyError, TypeError, AttributeError):
            raise BYOKError("响应不符合 Anthropic Messages 协议") from None
    else:
        raise BYOKError("不支持的 API 协议；不会回退到 Gemini")
    if not isinstance(result.content, str) or not result.content.strip():
        raise BYOKError("模型没有返回非空文本内容")
    return result


def embed(config, key, texts, is_query=False, timeout=90):
    if not texts:
        return []
    model = config["model"]
    if config["protocol"] == "openai":
        data = _checked_post(config["base_url"].rstrip("/") + "/embeddings", {
            "model": model, "input": texts, "encoding_format": "float",
        }, {"Authorization": "Bearer " + key}, timeout)
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            if [r["index"] for r in rows] != list(range(len(texts))):
                raise BYOKError("Embedding 返回索引与输入不一致")
            vectors = [r["embedding"] for r in rows]
        except (KeyError, TypeError, AttributeError):
            raise BYOKError("响应不符合 OpenAI Embeddings 协议") from None
    elif config["protocol"] == "gemini":
        resource = model if model.startswith("models/") else "models/" + model
        payload = {"requests": [{
            "model": resource,
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT",
        } for text in texts]}
        data = _checked_post(_gemini_path(config["base_url"], model, "batchEmbedContents"), payload, {"x-goog-api-key": key}, timeout)
        try:
            vectors = [r["values"] for r in data["embeddings"]]
        except (KeyError, TypeError):
            raise BYOKError("响应不符合 Gemini batchEmbedContents 协议") from None
    else:
        raise BYOKError("不支持的 embedding 协议")
    if len(vectors) != len(texts) or not vectors:
        raise BYOKError("Embedding 返回数量与输入不一致")
    dimensions = len(vectors[0])
    if not dimensions or any(not isinstance(v, list) or len(v) != dimensions or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v) for v in vectors):
        raise BYOKError("Embedding 向量维度或数值无效")
    return vectors


def test_connection(config):
    config = validate_config(config)
    cfg = config["byok"]
    for model in dict.fromkeys([cfg["model_pro"], cfg["model_flash"]]):
        generate(cfg, config["api"][0], model, "Reply with the single word OK.", max_tokens=256, timeout=15)
    if cfg["embedding"]["enabled"]:
        embedding = cfg["embedding"]
        embed(embedding, embedding["api"][0], ["PubChat connection test"], is_query=True, timeout=15)
    return {"success": True, "message": {"zh": "文本模型及已启用的 embedding 接口测试通过；此测试不代表检索质量验证。", "en": "Configured text and enabled embedding endpoints responded correctly; retrieval quality is not validated."}}
