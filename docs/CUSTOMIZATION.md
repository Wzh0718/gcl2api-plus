# 开发与发布指南

本仓库 [Wzh0718/gcl2api-plus](https://github.com/Wzh0718/gcl2api-plus) 是独立维护的完整项目：
以 `su-kaka/gcli2api` 为代码基线二次开发，所有定制直接落在仓库主树，**不再有 custom-overlay 覆盖层，也不再做每日自动上游同步**（2026-10-16 起，见 `docs/ADR-001-完整项目化与agy-exe补充环境.md`）。

唯一发布入口：

```bash
./scripts/build-and-push.sh
```

脚本从本仓库当前 HEAD 用 `git archive` 导出干净源码，跑质量门禁后构建并推送 GitHub Container Registry。**只有已提交进 Git 的内容会进入镜像**，未提交改动不会被打包。

## 日常开发流程

1. 直接修改仓库主树代码（`src/`、`front/`、`web.py`、`config.py`、`tests/` 等）。
2. 本地验证：

   ```bash
   .venv/bin/python -m pytest tests/ -q
   python3 -m compileall -q src tests web.py config.py
   node --check front/common.js
   ```

3. 提交后执行 `./scripts/build-and-push.sh` 发布。

## 上游挑拣流程（人工）

上游 remote 保留只读，用于不定期挑拣修复：

```bash
git fetch upstream
git log --oneline HEAD..upstream/master        # 看上游新增提交
git cherry-pick <sha>                          # 按需挑拣，冲突按我方定制为准解决
```

- 挑拣是**人工、按需**行为，不再有自动同步；挑拣后必须跑全量 pytest。
- 冲突解决原则：定制功能（计费、调用监控、凭证池、403 复检、模型降级链、敏感词、图像协议等）以我方版本为准；上游在共享函数上的通用修复手工移植。

## 发布结果

脚本用 Docker Buildx 一次构建 `linux/amd64` 与 `linux/arm64` 双架构镜像（可用 `PLATFORMS` 调整），然后为同一个多架构镜像清单添加并推送两个 Tag：

```text
ghcr.io/wzh0718/gcl2api-plus:vYYYYMMDD
ghcr.io/wzh0718/gcl2api-plus:latest
```

amd64 与 arm64 主机直接 `docker pull` 同名 Tag 即可，无需指定平台。

日期固定按 `Asia/Shanghai` 生成。例如 2026 年 7 月 27 日发布：

```text
ghcr.io/wzh0718/gcl2api-plus:v20260727
ghcr.io/wzh0718/gcl2api-plus:latest
```

同一天重复执行会重新推送当天 Tag 和 `latest`。如果仓库启用了不可变 Tag 策略，已经存在的日期 Tag 可能拒绝覆盖，此时应使用新的日期 Tag。

## GitHub Actions 发布（ghcr.io）

`.github/workflows/ghcr-release.yml` 与 `build-and-push.sh` 等价：导出本仓库源码 → 质量门禁 → 用 QEMU + Buildx 构建多架构镜像，推送到 GitHub Container Registry。

- **仅手动触发**：在 Actions 页面 workflow_dispatch 执行，无定时任务；
- 认证使用内置 `GITHUB_TOKEN`（`packages: write`），无需配置额外 secret；
- 通过环境变量 `IMAGE_NAME=ghcr.io/wzh0718/gcl2api-plus` 复用同一套发布逻辑。

## 首次准备

发布机器需要：

- `git`
- `uv`
- `python3`
- `node`
- 可用的 Docker 服务（含 Docker Buildx 插件；本地构建 arm64 需要 QEMU binfmt，Docker Desktop 自带，普通 Linux 主机执行一次 `docker run --privileged --rm tonistiigi/binfmt --install arm64`）
- 访问 Python 包源、基础镜像仓库和 GitHub Container Registry 的网络

GitHub Container Registry 登录只需要提前执行一次，密码不写入脚本：

```bash
echo "$CR_PAT" | docker login ghcr.io -u <GitHub 用户名> --password-stdin
```

## 一键发布流程

`scripts/build-and-push.sh` 顺序执行：

1. 校验 `BUILD_REF`（默认 `HEAD`）可解析为 commit。
2. 检查 Docker 服务与 Buildx 可用。
3. 在 `/tmp` 下创建临时目录，用 `git archive` 导出 `BUILD_REF` 的干净源码。
4. 运行计费、Proxy、SQLite 迁移专项测试和完整 pytest。
5. 运行 Python 编译、JavaScript 语法和合并标记检查。
6. 使用 Buildx 按 `PLATFORMS`（默认 `linux/amd64,linux/arm64`）构建多架构镜像，同时添加日期 Tag 和 `latest`，并通过 `--push` 直接推送。
7. 输出镜像名和实际构建的 Git Commit，然后删除临时目录。

任何测试、构建或推送步骤失败，脚本立即退出，不继续后续步骤。

## 当前定制功能

### SQLite 单库存储

- `src/storage_adapter.py` 固定选择 SQLite。
- `src/storage/sqlite_manager.py` 管理账号、配置、Proxy 上下文和账单数据。
- 默认数据库为 `${CREDENTIALS_DIR:-./creds}/credentials.db`。
- Redis 只作为计费汇总镜像；Redis 失败不能阻塞 SQLite 主记录，看板会自动回退 SQLite。

### Antigravity 账号 Proxy

- `proxy_mode` 支持 `inherit`、`custom` 和 `direct`。
- `src/api/antigravity.py:get_effective_proxy_url` 是有效代理入口。
- 账号重试切换会同时切换凭证、Token、Project、Proxy 和请求体。
- OAuth、项目发现、额度检查和 Antigravity 请求使用同一账号网络上下文。

### Token 计费

- `src/billing.py` 负责用量解析、价格计算、请求去重和 Redis 镜像。
- `src/api/antigravity.py` 的 `record_billing_once` 是计费记录入口。
- 同一个 `request_id` 在去重有效期内只入账一次。
- 价格按每 1M Token 配置，金额保存 8 位小数。
- 后续价格变化不重算历史账单。
- 统计日期按上海自然日计算。

### 计费 API 与控制面板

- `src/panel/billing.py` 提供汇总、排行和价格管理 API。
- `src/panel/__init__.py` 与 `web.py` 挂载计费路由。
- `front/common.js` 加载计费数据并保存价格。
- 桌面和移动控制面板提供 Token 计费入口。

## SQLite 迁移规则

1. 迁移必须兼容已有 `credentials.db`。
2. 使用可重复执行的 `CREATE TABLE IF NOT EXISTS` 或缺列后 `ALTER TABLE ADD COLUMN`。
3. 不在启动时删除表、删除列或清空历史账单。
4. 新列必须有兼容旧数据的默认值或允许为空。
5. 修改结构后必须通过 `tests/test_sqlite_billing_schema.py`。
6. 生产更新前备份 `credentials.db`，数据库文件不得进入仓库或镜像。

## 环境变量参考

优先级：显式环境变量 > `.env` > 控制面板/SQLite 配置 > 代码默认值。布尔值接受 `true/1/yes/on` 与 `false/0/no/off`。带 ★ 的变量为本 fork 新增或语义有调整。权威映射见 `config.py` 的 `ENV_MAPPINGS` 与各 getter 的 docstring。

### 服务器与认证

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `HOST` | 监听地址 | `0.0.0.0` |
| `PORT` | 监听端口 | `7861` |
| `WORKERS` | uvicorn worker 进程数（直接运行 `python web.py` 时生效） | `1` |
| `API_PASSWORD` | 聊天 API 端点密码 | 继承 `PASSWORD`，否则 `pwd` |
| `PANEL_PASSWORD` | 控制面板登录密码 | 继承 `PASSWORD`，否则 `pwd` |
| `PASSWORD` | 通用密码，设置后覆盖上面两个专用密码 | `pwd` |
| `CREDENTIALS_DIR` | SQLite 数据库与凭证目录 | `./creds` |

### 代理与上游端点

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `PROXY` | 全局代理，支持 `http/https/socks5`；`inherit` 账号使用 | 空 |
| `OAUTH_PROXY_URL` | Google OAuth2 端点 | `https://oauth2.googleapis.com` |
| `GOOGLEAPIS_PROXY_URL` | Google APIs 端点 | `https://www.googleapis.com` |
| `RESOURCE_MANAGER_API_URL` | Resource Manager API 端点 | `https://cloudresourcemanager.googleapis.com` |
| `SERVICE_USAGE_API_URL` | Service Usage API 端点 | `https://serviceusage.googleapis.com` |
| `CODE_ASSIST_ENDPOINT` | Code Assist API 端点 | `https://cloudcode-pa.googleapis.com` |
| `ANTIGRAVITY_API_URL` ★ | Antigravity 上游地址；保持默认 daily 地址时自动按 sandbox→daily→prod 降级，自定义反代地址后不再降级 | `https://daily-cloudcode-pa.googleapis.com` |

### Antigravity 行为

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `ENABLE_GEMINICLI` ★ | 是否启用 GeminiCLI 路由 | `false` |
| `ANTIGRAVITY_CLI_VERSION` ★ | User-Agent 客户端版本号；上游按版本下发模型列表，过旧会缺新模型，须跟随真实 agy cli | `1.2.7` |
| `ANTIGRAVITY_CLI_OS_TYPE` ★ | User-Agent os_type 字段 | `linux` |
| `ANTIGRAVITY_CLI_ARCH` ★ | User-Agent arch 字段（仅标识串，与镜像架构无关） | `amd64` |
| `ANTIGRAVITY_CLI_AUTH_METHOD` ★ | User-Agent auth_method 字段 | `consumer` |
| `ANTIGRAVITY_HUB_VERSION` ★ | 正式请求使用的 Antigravity hub 版本 | `2.28.2` |
| `ANTIGRAVITY_HUB_CL` ★ | 与 hub 版本配套的 changelist，由官方二进制 `--stamp` 核验 | `1009437749` |
| `ANTIGRAVITY_OAUTH_UA_VERSION` ★ | OAuth token 端点 UA 版本 | 跟随 `ANTIGRAVITY_HUB_VERSION` |
| `ANTIGRAVITY_OAUTH_USER_AGENT` ★ | OAuth token 端点完整 User-Agent 覆盖 | `vscode/1.X.X (Antigravity/{版本})` |
| `ANTIGRAVITY_STREAM2NOSTREAM` ★ | 非流式请求改走流式 API 并收集为完整响应 | `false` |
| `MAX_NON_STREAM_BUFFER_BYTES` ★ | 非流式响应收集缓冲上限（字节） | `16777216` |
| `CREDENTIAL_CANDIDATE_LIMIT` ★ | 单次请求候选凭证上限（1–32） | `32` |
| `ANTIGRAVITY_SWITCH_CREDENTIAL` | 重试时是否切换凭证；关闭则固定当前凭证，直到其对该模型进入冷却或被禁用 | `false` |
| `ANTIGRAVITY_MODEL_FALLBACK_CHAIN` ★ | 配额耗尽型 429 的模型降级链，逗号分隔；留空关闭，图片请求不降级 | 空（关闭） |
| `ANTIGRAVITY_PACING_CONCURRENCY` ★ | 每账号同时在飞的上游请求上限；健康号池按最少使用分配 | `15` |
| `ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS` ★ | 单账号并发占满时的最长排队等待秒数；超时先在本轮请求内换空闲账号，无候选时才返回 429；0 表示立即换号 | `30` |
| `ANTIGRAVITY_NETWORK_CHECK_ENABLED` ★ | 出口 IP 与地区资格检查门禁 | `false` |
| `ANTIGRAVITY_NETWORK_CHECK_TTL_SECONDS` ★ | 检查结果有效期（秒，最小 60） | `1800` |
| `ANTIGRAVITY_EGRESS_IP_CHECK_URL` ★ | 出口 IP 回显接口，返回至少 `{"ip":"..."}`；启用检查前必填 | 空 |
| `ANTIGRAVITY_403_RECHECK_ENABLED` ★ | 403 封禁账号是否定期复检解封 | `true` |
| `ANTIGRAVITY_403_RECHECK_INTERVAL` ★ | 复检轮询间隔（秒，最小 60） | `3600` |

### 重试、封禁与输出

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `RETRY_429_ENABLED` | 429 重试开关 | `true` |
| `RETRY_429_MAX_RETRIES` | 429 最大重试次数 | `5` |
| `RETRY_429_INTERVAL` | 429 重试间隔（秒） | `1` |
| `AUTO_BAN` | 命中错误码自动封禁凭证 | `false` |
| `AUTO_BAN_ERROR_CODES` | 触发封禁的状态码，逗号分隔 | `403` |
| `ANTI_TRUNCATION_MAX_ATTEMPTS` | 流式抗截断最大续写尝试（`流式抗截断/` 前缀模型） | `3` |
| `STREAM_IDLE_TIMEOUT` ★ | 上游流式空闲超时（秒），超时主动断开防僵尸流；`0` 禁用 | `300` |
| `COMPATIBILITY_MODE` | 兼容模式：system 消息全部转 user，可避免流式空回但可能降低理解 | `false` |
| `RETURN_THOUGHTS_TO_FRONTEND` | 是否把思维链返回前端，关闭则过滤 | `true` |
| `ANTHROPIC_DEBUG` | Anthropic 协议转换调试日志 | `true` |

### 计费与看板

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `BILLING_CURRENCY` ★ | 计费币种 | `CNY` |
| `BILLING_DEDUPE_TTL_DAYS` ★ | 请求去重保留天数 | `7` |
| `BILLING_REDIS_PREFIX` ★ | Redis 计费键前缀 | `gcli:billing` |
| `BILLING_REDIS_TTL_DAYS` ★ | Redis 镜像保留天数 | `45` |
| `BILLING_REDIS_SCAN_BATCH_SIZE` ★ | 看板单批扫描键数，最大 `1000` | `200` |
| `BILLING_REDIS_SCAN_MAX_KEYS` ★ | 单次看板扫描安全上限，超过则拒绝响应 | `100000` |
| `REDIS_URL` | Redis 地址；计费镜像与看板加速，不可用时自动回退 SQLite | 空 |
| `REDIS_USER` ★ | Redis 用户名，推荐与密码分离配置（如 `default`） | 空 |
| `REDIS_PASSWORD` ★ | Redis 密码，特殊字符无需 URL 编码 | 空 |

### 运维

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `DINGTALK_WEBHOOK_URL` ★ | 全账号不可用钉钉告警：首次立即发送，之后每小时最多一次；机器人关键词设为 `Gemini反代` | 空 |
| `KEEPALIVE_URL` | 保活目标 URL，定期 GET；留空禁用 | 空 |
| `KEEPALIVE_INTERVAL` | 保活请求间隔（秒） | `60` |
| `ENABLE_LOG` | 设为 `0/false/no/off` 时彻底关闭日志（最高性能） | `1`（开启） |
| `LOG_LEVEL` | 日志级别：`debug/info/warning/error/critical` | `info` |
| `LOG_FILE` | 日志文件路径 | `log.txt` |

历史兼容（SQLite 单库版不会读取）：`POSTGRESQL_URI`、`MONGODB_URI`、`MONGODB_DATABASE`。

直接运行 `python web.py` 时，服务会自动读取项目根目录 `.env`；系统或容器已经显式注入的变量优先，不会被 `.env` 覆盖。Docker Compose 会将 `.env` 中的 `REDIS_URL`、`REDIS_USER`、`REDIS_PASSWORD` 传入容器。推荐让 `REDIS_URL` 只包含协议、主机、端口和数据库，将用户名、密码分别配置；这样密码中的 `#`、`@`、`:` 会原样传给 Redis 客户端，不需要 URL 编码。旧的完整 URL 仍兼容，但其中的特殊字符必须百分号编码，且不能和分离认证混用。修改 `.env` 后，直接运行方式需要重启进程，Compose 方式执行 `docker compose up -d` 以重建容器；仅修改 `.env` 不需要重建镜像。

## 发布脚本配置

通常不需要传参数。必要时可以通过环境变量覆盖：

```bash
BUILD_REF=main \
IMAGE_NAME=ghcr.io/wzh0718/gcl2api-plus \
./scripts/build-and-push.sh
```

可用变量：

| 变量 | 默认值 |
| --- | --- |
| `BUILD_REF` | `HEAD`；要构建的 commit/分支/tag，只有已提交内容会进镜像 |
| `IMAGE_NAME` | `ghcr.io/wzh0718/gcl2api-plus` |
| `SOURCE_URL` | `https://github.com/Wzh0718/gcl2api-plus`；写入 `org.opencontainers.image.source` 标签 |
| `PLATFORMS` | `linux/amd64,linux/arm64`；只需 amd64 时设为 `linux/amd64` |
| `IMAGE_DESCRIPTION` | GHCR 包描述，默认与 `Dockerfile` 的 `description` 标签一致 |
| `RELEASE_DATE` | 上海时区当天，格式 `YYYYMMDD` |
| `KEEP_BUILD_DIR` | `0`；设为 `1` 时保留临时源码用于排查 |

镜像元数据标签的含义：

| 标签 / 注释 | 内容 |
| --- | --- |
| `org.opencontainers.image.source` | 本仓库（真正构建发布方）；GitHub 用它做「包 → 仓库」关联 |
| `org.opencontainers.image.revision` | 本次构建使用的本仓库 commit sha |
| `org.opencontainers.image.created` | 构建时间（上海时区） |
| `org.opencontainers.image.title` / `description` | 来自 `Dockerfile`；多架构时同时写入 manifest index 注释 |

`RELEASE_DATE` 主要用于补发或测试。例如：

```bash
RELEASE_DATE=20260727 ./scripts/build-and-push.sh
```

## 回滚

`latest` 始终指向最近一次成功推送的构建。需要回滚时，部署明确的历史日期 Tag，而不是重新构建旧代码：

```text
ghcr.io/wzh0718/gcl2api-plus:v20260727
```

日期 Tag 提供可追溯的发布锚点；镜像标签中还记录了构建使用的 Git Commit。
