# 定制覆盖与 Harbor 一键发布

本仓库不再通过合并定制分支来同步上游。发布时始终临时拉取 GitHub 最新 `master`，再用 `custom-overlay/files/` 覆盖定制文件，测试通过后构建并推送 Harbor。

唯一发布入口：

```bash
./scripts/build-and-push.sh
```

脚本不会修改当前 Git 分支、远程或工作区，也不会向 GitHub 推送。

## 发布结果

脚本只构建一次镜像，然后为同一个镜像添加并推送两个 Tag：

```text
harbor.beeintel.com/crawler-platform/gcli2api:vYYYYMMDD
harbor.beeintel.com/crawler-platform/gcli2api:latest
```

日期固定按 `Asia/Shanghai` 生成。例如 2026 年 7 月 27 日发布：

```text
harbor.beeintel.com/crawler-platform/gcli2api:v20260727
harbor.beeintel.com/crawler-platform/gcli2api:latest
```

同一天重复执行会重新推送当天 Tag 和 `latest`。如果 Harbor 项目启用了 Tag 不可变策略，已经存在的日期 Tag 可能拒绝覆盖，此时需要调整 Harbor 策略或在下一日期发布。

## GitHub Actions 自动发布（ghcr.io）

`.github/workflows/ghcr-sync-release.yml` 每天 09:17（Asia/Shanghai）自动执行与 `build-and-push.sh` 等价的流程：拉上游 `master` → 应用覆盖层 → 质量门禁 → 构建镜像，推送到 GitHub Container Registry：

```text
ghcr.io/wzh0718/gcl2api-plus:vYYYYMMDD
ghcr.io/wzh0718/gcl2api-plus:latest
```

- 上游无新提交时跳过（`.last-released-upstream-sha` 记录上次发布的上游 SHA）；
- 认证使用内置 `GITHUB_TOKEN`（`packages: write`），无需配置额外 secret；
- 也可在 Actions 页面手动触发（workflow_dispatch）；
- 脚本通过环境变量 `HARBOR_IMAGE=ghcr.io/wzh0718/gcl2api-plus` 复用同一套发布逻辑，本地手动发布到 Harbor 的方式不受影响。

## 首次准备

发布机器需要：

- `git`
- `uv`
- `python3`
- `node`
- 可用的 Docker 服务
- 访问 GitHub、Python 包源、基础镜像仓库和 Harbor 的网络

Harbor 登录只需要提前执行一次，密码不写入脚本：

```bash
docker login harbor.beeintel.com
```

## 一键发布流程

`scripts/build-and-push.sh` 顺序执行：

1. 校验覆盖层中没有 `.env`、`creds/`、`.codegraph/` 或数据库文件。
2. 检查 Docker 服务可用。
3. 在 `/tmp` 下创建临时目录。
4. 从 `https://github.com/su-kaka/gcli2api.git` 拉取最新 `master`。
5. 将 `custom-overlay/files/` 原样覆盖到最新源码。
6. 运行计费、Proxy、SQLite 迁移专项测试和完整 pytest。
7. 运行 Python 编译、JavaScript 语法和差异空白检查。
8. 使用上游最新 Dockerfile 构建一次镜像，同时添加日期 Tag 和 `latest`。
9. 先推送日期 Tag，再推送 `latest`。
10. 输出镜像名和实际使用的上游 Git Commit，然后删除临时目录。

任何测试、构建或推送步骤失败，脚本立即退出，不继续后续步骤。

## 定制覆盖层

`custom-overlay/files/` 是发布时的定制插件层，目录结构与上游项目根目录一致。例如：

```text
custom-overlay/files/
├── .dockerignore
├── config.py
├── front/
├── src/
├── tests/
└── web.py
```

覆盖规则是完整文件替换，不是 Git merge：

- 上游没有改动同名文件时，直接加入定制功能。
- 上游改动了同名文件时，覆盖层版本优先。
- 不会产生 Git 冲突，但上游接口变化可能导致测试失败。
- 测试失败时需要把上游必要变化人工同步到覆盖层文件，再重新执行脚本。

禁止放入覆盖层：

- `.env`
- `creds/`
- `.codegraph/`
- `*.db`、`*.sqlite`、WAL/SHM 文件
- Harbor、GitHub 或第三方服务的真实密码和 Token

`.dockerignore` 会阻止 Git 元数据、环境文件、凭证、数据库、缓存和测试目录进入最终镜像。测试会在 Docker 构建前完成。

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
6. 生产更新前备份 `credentials.db`，数据库文件不得进入覆盖层或镜像。

## 主要环境变量

| 变量 | 作用 | 默认值/说明 |
| --- | --- | --- |
| `CREDENTIALS_DIR` | SQLite 与账号目录 | `./creds` |
| `PROXY` | 全局代理 | `inherit` 账号使用 |
| `ENABLE_GEMINICLI` | 是否启用 GeminiCLI | `false` |
| `ANTIGRAVITY_STREAM2NOSTREAM` | 流转非流模式 | `false` |
| `MAX_NON_STREAM_BUFFER_BYTES` | 最大非流缓冲区 | `16777216` |
| `CREDENTIAL_CANDIDATE_LIMIT` | 账号候选上限 | 最大 `32` |
| `DINGTALK_WEBHOOK_URL` | 全账号不可用钉钉告警 | 首次立即发送，之后所有反代不可用提醒每小时最多一次；机器人关键词设为 `Gemini反代` |
| `BILLING_CURRENCY` | 计费币种 | `CNY` |
| `BILLING_DEDUPE_TTL_DAYS` | 请求去重时间 | `7` 天 |
| `BILLING_REDIS_PREFIX` | Redis 键前缀 | `gcli:billing` |
| `BILLING_REDIS_TTL_DAYS` | Redis 镜像保留期 | `45` 天 |
| `BILLING_REDIS_SCAN_BATCH_SIZE` | 看板单批读取键数 | `200`，最大 `1000` |
| `BILLING_REDIS_SCAN_MAX_KEYS` | 单次看板扫描安全上限 | `100000`，超过则拒绝响应 |
| `REDIS_URL` | Redis 地址 | 可选的计费镜像与看板加速；不可用时自动回退 SQLite |
| `REDIS_USER` | Redis 用户名 | 可选；推荐与 `REDIS_PASSWORD` 分离配置，例如 `default` |
| `REDIS_PASSWORD` | Redis 密码 | 可选；作为客户端参数传递，特殊字符不需要 URL 编码 |

直接运行 `python web.py` 时，服务会自动读取项目根目录 `.env`；系统或容器已经显式注入的变量优先，不会被 `.env` 覆盖。Docker Compose 会将 `.env` 中的 `REDIS_URL`、`REDIS_USER`、`REDIS_PASSWORD` 传入容器。推荐让 `REDIS_URL` 只包含协议、主机、端口和数据库，将用户名、密码分别配置；这样密码中的 `#`、`@`、`:` 会原样传给 Redis 客户端，不需要 URL 编码。旧的完整 URL 仍兼容，但其中的特殊字符必须百分号编码，且不能和分离认证混用。修改 `.env` 后，直接运行方式需要重启进程，Compose 方式执行 `docker compose up -d` 以重建容器；仅修改 `.env` 不需要重建镜像。

## 发布脚本配置

通常不需要传参数。必要时可以通过环境变量覆盖：

```bash
UPSTREAM_BRANCH=master \
HARBOR_IMAGE=harbor.beeintel.com/crawler-platform/gcli2api \
./scripts/build-and-push.sh
```

可用变量：

| 变量 | 默认值 |
| --- | --- |
| `UPSTREAM_URL` | `https://github.com/su-kaka/gcli2api.git` |
| `UPSTREAM_BRANCH` | `master` |
| `CUSTOM_OVERLAY_DIR` | `custom-overlay/files` |
| `HARBOR_IMAGE` | `harbor.beeintel.com/crawler-platform/gcli2api` |
| `RELEASE_DATE` | 上海时区当天，格式 `YYYYMMDD` |
| `KEEP_BUILD_DIR` | `0`；设为 `1` 时保留临时源码用于排查 |

`RELEASE_DATE` 主要用于补发或测试。例如：

```bash
RELEASE_DATE=20260727 ./scripts/build-and-push.sh
```

## 回滚

`latest` 始终指向最近一次成功推送的构建。需要回滚时，部署明确的历史日期 Tag，而不是重新构建旧代码：

```text
harbor.beeintel.com/crawler-platform/gcli2api:v20260727
```

日期 Tag 提供可追溯的发布锚点；镜像标签中还记录了构建使用的上游 Git Commit。
