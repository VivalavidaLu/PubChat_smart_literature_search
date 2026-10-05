# PubChat BYOUK — Bring Your Own URL & Key

这是 [PubChatOfficial/PubChat_smart_literature_search](https://github.com/PubChatOfficial/PubChat_smart_literature_search) 的本地部署扩展 fork。网页中沿用 **BYOK** 名称，支持用户自行提供 Base URL、API Key、主模型与快速模型 ID，而不是只选择原版 Gemini 预设。原版检索流程和 Gemini 预设保留。

> 这是接口与部署扩展，不是原论文对替换模型的验证。更换模型、embedding 或关闭语义预筛选可能改变检索/筛选效果，需要单独评价。此版本不适合直接暴露到公网。

## 功能与限制

| 能力 | 支持情况 |
| --- | --- |
| 文本生成 | OpenAI-compatible Chat Completions、Gemini generateContent、Anthropic Messages |
| 自定义模型 ID | 原样发送；拒绝首尾空格，不重命名或自动映射 |
| 快速模型 | 可独立填写；留空时复用主模型 |
| Embedding | 独立配置 OpenAI-compatible 或 Gemini；默认启用 |
| 无 embedding 服务 | 用户明确取消后关闭，并显示降级提示；不静默关闭 |
| 原 Gemini 预设 | BYOK 关闭时保留 |
| 连接测试 | 确认后发送最小请求，可能产生费用 |
| OpenAI Responses API | 当前不支持；需 Chat Completions 兼容端点 |

## 新部署

需要 Docker 引擎和 Docker Compose **2.24.4 或更高版本**（覆盖文件使用 `!override` 标签），以及访问 Docker Hub、PubMed 和所选模型服务的网络。本地部署不等于完全离线。

```bash
git clone https://github.com/vivalavidalu/PubChat_smart_literature_search.git
cd PubChat_smart_literature_search
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
```

打开 **http://localhost:8000/search/task**，已有缓存时按 Ctrl+F5 刷新。

- `docker-compose.override.yml` 会自动加载，绑定 **127.0.0.1:8000**，不向局域网/公网发布服务。
- 当前前端固定使用 `http://localhost:8000` API 地址，不要随意替换端口或用远程主机 URL。
- 项目名及容器前缀为 `pubchat-local`。若同一台机器已经部署此版本，请不要从第二个目录启动同名项目，以免冲突或切换挂载目录；先明确现有服务与数据路径。
- worker 使用锁定 digest 的上游预构建镜像，通过可读扩展模块接入 BYOK。Dockerfile、Compose 覆盖、前端扩展和 worker 扩展需要一起使用。
- 上游已有环境配置模板仍保留，任何自行修改的真实凭据都不应提交到 Git。

## 网页使用

1. 在 AI 配置上方展开 **BYOK / YOUR MODELS · YOUR ENDPOINT**，启用自定义接口。
2. 选择协议，填写 Base URL、精确模型 ID 与 Key。
3. Base URL 填服务根地址，不填完整 `/chat/completions`、`/messages` 或 `/embeddings` 端点。通过 OpenAI-compatible 网关使用 Claude 时选择 `openai` 协议。
4. 默认需要 embedding 配置；没有对应服务时明确取消启用，理解语义预筛选关闭的影响。
5. **Validate** 只做本地检查；**Test connection** 会发送最小模型请求，可能计费。连接成功不代表完整检索或科学质量已验证。
6. Key 只在页面直接输入；刷新后需重新填写。

Windows 宿主机上的本地模型服务应使用类似 `http://host.docker.internal:11434/v1` 的地址，而不是容器内的 `localhost`。端口与模型 ID 以实际服务为准。HTTP 仅适合可信本机/私有网络。

详见 [配置与安全边界](local_extensions/BYOK_README.md) 和 [前端维护说明](local_extensions/BYOK_UI.md)。

## 安全与数据

- BYOK Key 不存入浏览器持久存储或 PostgreSQL。任务期间临时存在 Redis，TTL 为 24 小时，完成/失败/停止时清理；异常中断时由 TTL 后备清理。
- Redis 不是加密凭据库，开启持久化后临时 Key 可能进入快照。不要公开 Redis、数据库或共享它们的数据文件。
- 本机 Origin 检查不等于完整鉴权。此 fork 是单机本地部署，不是已加固的公网多用户系统。
- HTTP 网络、模型服务商的数据处理和模型调用费用由使用者自行评估。
- 不上传个人数据库、检索结果、真实 Key、本机备份、运行报告或依赖目录。

## 独立测试（不需要 Docker）

运行环境：Python 3.11+；网页测试另需 Node.js/npm 和 Chrome 或 Playwright Chromium。生产 Docker 后端使用上游 Dockerfile 中的 Python 版本。

```bash
python local_extensions/tests/test_byok.py
node --check frontend/dist/byok.js
node --check frontend/dist/assets/index-D__M8vFc.js
npm ci --prefix local_extensions/test_tools
npx --prefix local_extensions/test_tools playwright install chromium
node local_extensions/tests/byok-ui.test.cjs
```

Windows 优先使用已安装的 Google Chrome，其他情况使用 Playwright Chromium；可设置 `CHROME_PATH`。测试依赖 Playwright 锁定为 **1.63.0**。测试使用全新浏览器上下文，不读取用户登录态；所有模型响应都是明确标记的本地 fixture，不访问付费模型。

## 完整容器联调

先启动 Docker 引擎及此项目，然后运行：

```bash
python scripts/verify_byok.py --ui
```

该命令还会检查容器内 worker/API、Compose 与已部署网页经 Nginx 到本地 fixture provider 的三协议联调。它不是实际文献检索或真实模型服务验证。输出报告保存在被忽略的 `local_extensions/verification.json`。

**验证状态：**此前对应部署版本已通过 23 项独立后端测试及三协议本地联调。此次发布时 Docker 未启动，完整容器联调未重跑；独立宿主适配器、语法和隔离 UI 测试单独复核。实际服务商连通性和完整真实文献检索仍需自行验证。

## 启停和维护

```bash
docker compose stop
docker compose start
docker compose ps
```

不要执行 `docker compose down -v`，以免删除数据库卷。升级前备份数据库与检索结果、完成/停止任务，并重新验证前端选择器、固定 API 地址和锁定 worker 接口。

扩展原始验证基线为 `c72172f17c4f3b1fb9064d054685b04e75dcdb6c`。此公开 fork 基于当前上游提交 `e29d40ce46c0ce224d7f1a50f77788701573b5e3` 发布，保留其移除旧资料的结果；生产扩展代码与原部署一致。只公开可维护扩展、前端最小补丁和测试；依赖个人本机备份的回滚脚本及备份不包含在此 fork 中。原版作者、说明与历史均保留。
