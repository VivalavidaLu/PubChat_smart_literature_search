"""Readable extension over the pinned upstream worker; no bytecode edits.

The original PubMed retrieval, prompts, screening and exports remain upstream.
This module only routes BYOK model/embedding calls and task-scoped settings.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import time
from types import SimpleNamespace

import psycopg2
import redis
from common_utils.byok_protocols import BYOKError, embed, generate, restore_config, validate_config

upstream = importlib.import_module("celery_worker.celery_worker")
workflow_module = importlib.import_module(upstream.SearchWorkflow.__module__)
unified_module = importlib.import_module(workflow_module.UnifiedAIClient.__module__)
celery_app = upstream.celery_app
VERSION = "1.0.0"


def _redis():
    return redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=int(os.getenv("REDIS_PORT", "6379")), decode_responses=True)


# Redact task credentials even in upstream log messages (including PubMed keys).
_active_secrets = set()
_old_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _old_factory(*args, **kwargs)
    message = record.getMessage()
    for secret in _active_secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    record.msg, record.args = message, ()
    return record


logging.setLogRecordFactory(_record_factory)


def _load_config(task_id):
    with psycopg2.connect(**upstream.DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT byok_config FROM "userSchema".tasks WHERE id = %s', (str(task_id),))
            row = cur.fetchone()
    if not row or not row[0]:
        raise BYOKError("任务缺少 BYOK 配置；不会回退到 Gemini")
    raw = _redis().get(f"task:{task_id}:byok_keys")
    if not raw:
        raise BYOKError("任务的临时 API Key 已过期；请重新提交任务")
    credentials = json.loads(raw)
    config = restore_config(row[0], credentials)
    _active_secrets.clear()
    for values in credentials.values():
        if isinstance(values, list):
            _active_secrets.update(str(v) for v in values if v)
    return config


AI = workflow_module.UnifiedAIClient
_old_ai_init = AI.__init__
_old_resolver = AI._resolve_provider_settings
_old_builder = AI._init_model_builder
_old_generate = AI._generate_content


def _ai_init(self, llm_config, task_id=None, model_pro=None, model_flash=None, custom_base_url=None):
    self._byok = validate_config(llm_config)["byok"] if (llm_config or {}).get("model") == "byok" else None
    _old_ai_init(self, llm_config, task_id, model_pro, model_flash, custom_base_url)
    if self._byok:
        # Upstream can remotely report exhausted API keys. Never do that for BYOK.
        self.key_pool._log_api_error_async = lambda *args, **kwargs: None


def _resolver(llm_config, custom_base_url=None):
    if (llm_config or {}).get("model") != "byok":
        return _old_resolver(llm_config, custom_base_url)
    cfg = validate_config(llm_config)["byok"]
    return "byok_" + cfg["protocol"], "byok", cfg["base_url"], cfg["model_pro"], cfg["model_flash"]


def _builder(self, sdk_type):
    if sdk_type != "byok":
        return _old_builder(self, sdk_type)
    # Model builders are not used by BYOK: _generate_content calls REST directly.
    self._model_builder_pro = self._model_builder_flash = None


def _generate(self, prompt, use_pro_model=False, task_description="AI Task", max_output_tokens=None):
    if not self._byok:
        return _old_generate(self, prompt, use_pro_model, task_description, max_output_tokens)
    model = self.model_pro_name if use_pro_model else self.model_flash_name
    last_error = None
    for attempt in range(min(max(self.key_pool.total_count, 3), 6)):
        _index, key = self.key_pool.get_next_key()
        if not key:
            break
        unified_module._thread_local.current_key = key
        try:
            answer = generate(self._byok, key, model, prompt, max_tokens=max_output_tokens or 8192)
            self._record_token_usage(SimpleNamespace(usage_metadata={
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "total_tokens": answer.input_tokens + answer.output_tokens,
            }), model)
            return answer.content
        except BYOKError as exc:
            last_error = exc
            detail = str(exc)
            if "HTTP 401" in detail or "HTTP 403" in detail:
                self.key_pool.mark_exhausted(key, "byok_auth_failed")
            elif "HTTP 429" in detail or "HTTP 5" in detail or "网络" in detail:
                time.sleep(min(2 ** attempt, 5))
            else:
                raise
    raise last_error or BYOKError("BYOK 没有可用 API Key；请重新提交任务")


AI.__init__ = _ai_init
AI._resolve_provider_settings = staticmethod(_resolver)
AI._init_model_builder = _builder
AI._generate_content = _generate


class BYOKEmbeddingProvider:
    def __init__(self, config):
        self.config = config
        self._keys = iter(())
        import itertools
        import threading
        self._keys = itertools.cycle(config["api"])
        self._lock = threading.Lock()

    def _key(self):
        with self._lock:
            return next(self._keys)

    def embed_query(self, text):
        return embed(self.config, self._key(), [text], is_query=True)[0]

    def embed_documents(self, texts):
        return embed(self.config, self._key(), list(texts), is_query=False)


_old_embedding_factory = workflow_module.create_embedding_provider


def _embedding_factory(llm_config, workflow_config):
    if (llm_config or {}).get("model") != "byok":
        return _old_embedding_factory(llm_config, workflow_config)
    cfg = validate_config(llm_config)["byok"]["embedding"]
    if not cfg["enabled"]:
        raise BYOKError("Embedding 已明确关闭，不能初始化 embedding provider")
    return BYOKEmbeddingProvider(cfg)


workflow_module.create_embedding_provider = _embedding_factory
_old_initialize_clients = upstream.SearchWorkflow._initialize_clients


def _initialize_clients(self, llm_config):
    if (llm_config or {}).get("model") == "byok":
        config = validate_config(llm_config)
        self.config["embedding_enabled"] = config["byok"]["embedding"]["enabled"]
    return _old_initialize_clients(self, llm_config)


upstream.SearchWorkflow._initialize_clients = _initialize_clients
_old_workflow_init = upstream.SearchWorkflow.__init__
_workflow_signature = inspect.signature(_old_workflow_init)


def _workflow_init(self, *args, **kwargs):
    bound = _workflow_signature.bind(self, *args, **kwargs)
    llm_config = bound.arguments.get("llm_config") or {}
    if llm_config.get("model") == "byok":
        bound.arguments["llm_config"] = _load_config(bound.arguments["task_id"])
    return _old_workflow_init(*bound.args, **bound.kwargs)


upstream.SearchWorkflow.__init__ = _workflow_init
_task = upstream.run_search
_old_run = _task.run


def _run(task_id, *args, **kwargs):
    try:
        return _old_run(task_id, *args, **kwargs)
    finally:
        # Keys are held only for queued/running BYOK tasks; expiration is a fallback.
        try:
            r = _redis()
            r.delete(f"task:{task_id}:byok_keys")
            with psycopg2.connect(**upstream.DB_CONFIG) as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT status FROM "userSchema".tasks WHERE id=%s', (str(task_id),))
                    row = cur.fetchone()
            if row and row[0] == 'failed':
                r.hset(f"task:{task_id}:info", "status", "Failed")
        except Exception:
            logging.getLogger(__name__).warning("BYOK cleanup unavailable; credentials remain bounded by Redis TTL")
        _active_secrets.clear()


_task.run = _run


@celery_app.task(name="pubchat_byok.extension_health")
def extension_health():
    """No model request, no credentials and no patient data."""
    return {"version": VERSION, "protocols": ["openai", "gemini", "anthropic"], "embedding": ["openai", "gemini"]}


logging.getLogger(__name__).info("PubChat BYOK extension %s loaded; original Gemini presets retained", VERSION)
