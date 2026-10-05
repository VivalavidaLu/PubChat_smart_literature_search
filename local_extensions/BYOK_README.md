# PubChat 本地 BYOK 改造：使用与维护

## 入口

打开 http://localhost:8000/search/task ，按 Ctrl+F5 强制刷新。原 AI 配置区前新增 **BYOK / YOUR MODELS · YOUR ENDPOINT** 面板；展开后勾选 Enable BYOK。

## 配置

- `openai`：OpenAI-compatible **Chat Completions**，填写 API Base URL（通常以 `/v1` 结尾），不是完整 `/chat/completions` 地址。只提供 Responses API 的服务当前不支持。
- `gemini`：Gemini 原生 generateContent，Base URL 可填写 `https://generativelanguage.googleapis.com` 或服务商提供的 `/v1beta` 根地址。
- `anthropic`：Anthropic 原生 Messages，Base URL 通常 `https://api.anthropic.com` 或其 `/v1` 根地址。通过 OpenAI-compatible 网关调用 Claude 时应选择 openai。
- `Pro model ID`：精确模型 ID。
- `Flash model ID`：可选；为空时复用主模型。保留上游各阶段选择主/快速模型的行为。
- `API Key`：只在网页直接输入，不发到聊天、不写入 URL、不加入 Git。

对 Windows 宿主机上的本地模型服务器，容器内的 `localhost` 指容器本身，应填写类似 `http://host.docker.internal:11434/v1` 的地址。具体端口和模型 ID 以本地服务实际设置为准。HTTP 明文传输仅适合可信本机/私有网络。

模型 ID 不映射、不改变大小写、不自动去除首尾空格；首尾空格会被校验拒绝。

## Embedding

默认启用，需要单独提供 OpenAI-compatible 或 Gemini embedding 根地址和模型 ID，可复用主 Key，也可使用独立 Key。

Anthropic Messages 不是 embedding API；使用 Anthropic 文本模型时可另外选择 OpenAI-compatible/Gemini embedding 服务。

若服务不提供 embedding，可**明确取消** Enable embeddings。系统会提示语义预筛选关闭；不会自动关闭，也不应把关闭后的结果视为原论文配置的复现。更换 embedding 模型后，原相似度阈值未必适合新向量空间，需另行评价。

## 按钮与使用

1. Validate：仅本地校验，不发模型请求。
2. Test connection：确认后对主/快速模型（相同 ID 仅测一次）及已启用 embedding 发最小请求，**可能计费**。它验证连通性及响应结构，不验证文献检索质量。
3. Clear keys：清空当前页面 BYOK 密钥。
4. 检索问题、模式、语言、筛选条件及 PubMed Key 保留原页面操作方式。当前版本原有 PubMed Key 校验未改变。
5. 关闭 Enable BYOK 后，原来的 Google Gemini/OpenRouter Gemini 预设继续使用原逻辑。

刷新页面即清空 BYOK Key。非敏感设置只保存在当前浏览器标签页的 sessionStorage；刷新后若 BYOK 仍启用但未重新输入 Key，将阻止提交，不回退到 Gemini。

## 安全范围

BYOK Key 不写入 PostgreSQL。任务 Key 临时放在 Docker 私有网络中的 Redis，TTL 为 24 小时，任务正常完成/失败或点击停止时删除；异常中断时 TTL 是后备清理。Redis 临时值不是加密凭据库，启用 Redis 磁盘快照时可能进入快照，故不要公开数据库/Redis、不要共享其数据文件。

服务只绑定 `127.0.0.1:8000`，未新增公开端口。连接测试与任务创建仅接受本机页面 Origin（或无 Origin 的本机 CLI）；整体应用仍不是面向公网的完整多用户鉴权系统。

BYOK 禁用上游耗尽 Key 的远程错误上报，并对已加载任务 Key 做日志脱敏。原预设配置和历史数据未批量改写。

## 实现与兼容性

- 扩展原始验证基线：`c72172f17c4f3b1fb9064d054685b04e75dcdb6c`；公开版基于当前上游 `e29d40ce46c0ce224d7f1a50f77788701573b5e3`，不重新引入上游已移除的旧资料。
- worker 镜像锁定：`wuyuxuan1037/pubchat-celery-worker@sha256:32d3956c821c4a80b275040e5434c2eb5ddbf1f44f30a7a4e074b1ca6ba85535`。
- Python 扩展：`common_utils/byok_protocols.py`、`local_extensions/worker/byok_worker.py`。
- 前端扩展：`frontend/dist/byok.js`、`frontend/dist/byok.css`；原压缩 bundle 仅三处入口补丁，详见 BYOK_UI.md。
- PostgreSQL 仅新增 `tasks.byok_config` JSONB 列，不删除数据。
- Python 协议适配仅依赖标准库，不安装新生产包。前端无新生产依赖。
- Playwright 1.63.0 只用于独立浏览器测试，保存在 local_extensions/test_tools；不使用用户浏览器登录态。

不要直接用上游更新覆盖本地 bundle 或 worker 镜像；更新后需重新审查扩展接口并运行回归。

## 验证

在项目目录运行：

```bash
npm ci --prefix local_extensions/test_tools
npx --prefix local_extensions/test_tools playwright install chromium
python scripts/verify_byok.py --ui
```

完整验证需要先启动 Docker 引擎及此项目服务。Windows 上浏览器测试默认优先使用已安装的 Google Chrome；也可通过 `CHROME_PATH` 指定可执行文件，其他平台默认使用 Playwright Chromium。生成的 `local_extensions/verification.json` 仅存于本地，不上传 GitHub。

此前对应部署版本已通过 23 项独立后端测试、隔离 React UI 回归，以及网页 → Nginx → API → 本地模拟提供商的三协议联调。发布复核时 Docker 未启动，容器测试未能重跑；宿主适配器和隔离 UI 测试可独立运行，见 [发布指南](../README_BYOUK.md)。不要把此前验证或独立 UI 测试当作当前容器已启动的证据。

模拟响应在测试代码中明确标记，不是实际科研输出。未使用真实模型 Key、未调用付费模型、未执行完整真实文献检索。实际服务商、模型输出质量、API 额度和完整检索任务仍需用您选定的服务验证。

## 发布范围与维护

- 原版项目与历史保留，来源是 [PubChatOfficial/PubChat_smart_literature_search](https://github.com/PubChatOfficial/PubChat_smart_literature_search)。
- 此 fork 只发布扩展代码、Compose 覆盖、前端补丁、测试及使用说明。
- 本机备份、运行报告、镜像运行记录、API 文档下载副本、数据库、检索结果、密钥和依赖目录不纳入新增提交。
- 依赖个人部署备份的回滚脚本不随此公开版发布。调整或回滚前，先完成/停止任务并备份数据库和检索结果；不要执行删除数据库卷的命令。
- 上游跟踪的环境配置模板沿用原内容；不要将本机改过的真实凭据提交。此版本仅适用于本机，不应直接暴露到公网。
