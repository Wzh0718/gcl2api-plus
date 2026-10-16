# ADR-001：gcli2api 架构演进——完整项目化与 agy exe 补充环境

> 状态：**阶段 1 已落地（2026-10-16）**；阶段 0/2 待评审
> 日期：2026-10-14
> 依据：Antigravity IDE v2.23.1（language_server CL 998881661）逆向报告（`/home/libre/project/antigravity-re/REPORT.md`）+ gcli2api 现状代码走读
> 评审人：libre
>
> **落地记录（2026-10-16）**：方案 C 阶段 1「完整项目化」已执行——上游 master（105 commits）合入主干，
> `custom-overlay/` 删除，`scripts/build-and-push.sh` 改为本仓库 `git archive` 直接构建，
> CI 每日自动同步改为仅手动触发（`.github/workflows/ghcr-release.yml`），
> 开发文档见 `docs/CUSTOMIZATION.md`。阶段 0（协议对齐）与阶段 2（sidecar 控制面）尚未执行。

---

## 1. 背景与驱动

### 1.1 现状

gcli2api 当前以 **custom-overlay 插件形态**存在：

- 上游 `su-kaka/gcli2api` 提供 GeminiCLI / Antigravity / Vertex 三套协议路由的基础实现；
- 我们的核心价值（API Key 签发、计费镜像、多账号 credentials 池、账号健康/403 复核、代理分组、调用监控、模型配额降级链）全部在 `custom-overlay/files/`；
- 发布 = GitHub Actions **每日 09:17 拉取上游 master + 叠加覆盖层** 出多架构 GHCR 镜像。

Antigravity 协议层为纯 Python 手写直译（`src/api/antigravity.py`，2476 行）：

- OAuth：自建 callback（端口 11451）+ RT 导入，凭证落 `creds/credentials.db`；
- 请求：`wrap_cli_request` 手工构造 `{project, requestId, request, model, userAgent, requestType, enabledCreditTypes}` 包装层；
- 指纹：`build_antigravity_user_agent` 伪装 Antigravity CLI，`antigravity_cli_version.py` 每天轮询官方 updater manifest，**用真实凭证探测验证后才敢热切换 UA 版本**；
- 漂移对抗模块群：`antigravity_403_recheck.py`、`antigravity_account_health.py`、`antigravity_full_check.py`、`antigravity_error_classifier.py`、`model_list.py`（models 列表真实性核查）。

### 1.2 驱动事件：逆向实测结论

2026-10-14 对 Antigravity IDE 官方包完成逆向（产物 `/home/libre/project/antigravity-re/`），关键事实：

| # | 事实 | 证据等级 |
|---|---|---|
| F1 | 主端点生产用 `https://daily-cloudcode-pa.googleapis.com`（Electron 注入），二进制默认才是 `cloudcode-pa` | 实测抓包 |
| F2 | 请求/响应均为 **protojson**（models 为 `map<string,ModelInfo>` 而非数组） | 实测（解析报错反证） |
| F3 | `streamGenerateContent` 包装层含 `requestId`（`chat/<uuid>`）、`userAgent`、`requestType`（`chat`/`agent`/`image_gen`）、`sessionId`；UA 格式 `antigravity/hub/<ver> (aidev_client; os_type=<os>; arch=<arch>; cl=<CL>)` | 实测抓包 |
| F4 | 登录两套客户端（个人 `1071006060591-…`/商业 `884354919052-…`，配对 secret 已验证），PKCE S256；**standalone 模式走本地回调 `http://localhost:<port>/auth/callback` 自动收 code（支持手动粘贴回退）** | 实测 |
| F5 | token 存 keyring（service `jetski-standalone-oauth-token`），文件存储回退 | 实测 |
| F6 | LS 本地 Connect-RPC（`x-codeium-csrf-token`）开放 331 RPC：`Login / StartCascade / SendUserCascadeMessage / GetModelResponse / GetAvailableModels / retrieveUserQuota…` | 实测可调通 |
| F7 | `--cloud_code_endpoint` **官方可配置**——LS 的全部出网可被导向自建代理 | 实测 |
| F8 | agent 模式（cascade planner）深度耦合服务端 model_configs，仅给枚举值报 `plan model not specified`；`--api_server_url` 缺省时 LS 报 `unsupported protocol scheme ""` | 实测未解 |
| F9 | 550 个 proto 描述符已提取（LanguageServerService 331 RPC 全集、ApiServerService 236 msg、JetskiService/CloudCode/PredictionService 方法面） | 静态 |
| F10 | 模型体系：`MODEL_CHAT_*` ~50 枚举 + `PLACEHOLDER_M0~M1673` 占位；modelId 含 gemini-3.1-pro/ultra、claude-opus-4-8/5-5 等 | 静态 |

### 1.3 问题陈述

1. **overlay 脆弱性**：按文件路径补丁，上游任意重构即可能经每日自动同步直达生产镜像；我们的定制体量已超过上游增量。
2. **协议漂移成本持续上涨**：版本探测、models 核查、403 分类器等都是在给手写协议层打补丁；F1–F3 证明上游仍在快速变化。
3. **登录/模型/指纹/额度**四件"环境"事务目前各自手写、各自脆弱，而官方二进制全部免费做好（F4–F7）。

---

## 2. 备选方案

### 方案 A：完整项目化 + 纯协议直译（去插件化，不动运行时）

固化 overlay 为主干，上游降为只读参考（手工挑拣 cherry-pick），协议层继续 Python 手写并补齐逆向发现的差异（F1–F3）。

- **优点**：部署形态不变（单容器、纯 Python）；多账号池/计费/路由零改动、零回归风险；协议层已验证与线格式匹配。
- **缺点**：漂移对抗照旧（版本探测、models 核查不能退役）；登录仍靠自建 callback + RT 导入；agent 能力（F8）继续缺失；上游非 Antigravity 部分（GeminiCLI/Vertex/通用修复）从此自负盈亏。
- **工作量**：S（1–2 天）+ 持续维护成本不变。

### 方案 B：全量 sidecar 驱动（agy exe 进热路径）

gcli2api 改为驱动官方 language_server：API 请求 → 翻译为 LS Connect-RPC（`GetModelResponse` / `SendUserCascadeMessage`）→ LS → Google。计费/监控在 LS 与 Google 之间插入自建代理（利用 F7 把 `--cloud_code_endpoint` 指向自己）。

- **优点**：协议保真度上限（Google 更新 LS 即跟进）；登录/模型/指纹/额度全免维护；agent 模式理论可得。
- **缺点**：
  - **多账号经济性差**：一个 LS 实例绑定一个账号（F5 keyring 单槽位），N 账号 = N 个 LS 进程。单实例磁盘 187MB；实测沙箱内 idle RSS 数值失真（namespace 限制），按 Go 服务常规估算每实例 150–400MB，N 个账号内存线性增长——与现有单进程凭证池模型相比是数量级恶化；
  - 计费链路变两层（API 翻译层 + LS↔Google 代理层），排障面翻倍；
  - LS 有状态（端口/CSRF/数据目录/崩溃看护），进程编排复杂；
  - agent 模式仍卡在 model_configs（F8），B 并不自动解锁。
- **工作量**：L（2–4 周）+ 运行时架构重写。

### 方案 C：混合——完整项目化 + agy exe 作"补充环境"（控制面 sidecar）【推荐】

热路径保持 Python 直译（已验证），agy exe（language_server standalone 模式）仅承担四件控制面事务：

| 事务 | 现状（手写） | sidecar 后（官方兜底） | 依据 |
|---|---|---|---|
| 登录 provisioning | 自建 callback:11451 + RT 导入 | 驱动 LS standalone 登录（F4 本地回调自动收 code），收割 RT 入 credentials.db；批量开号可脚本化 | F4/F5 |
| 模型目录 | model_list.py 真实性核查 | LS `GetAvailableModels` / `fetchAvailableModels` 官方全量，protojson map 直解 | F2/F6 |
| 版本/指纹 | manifest 轮询 + 凭证探测采纳 | 读 LS `--stamp`（CL 998881661）与版本号，UA 原样照抄，**免探测** | F3 |
| 额度/健康 | 自研口径对账 | LS `retrieveUserQuota(Summary)` 官方口径 | F6 |

二进制**不进镜像**：部署/首次运行时从官方 updater manifest 按需下载 + SHA256 固定（该 manifest 域名现有 `antigravity_cli_version.py` 已在轮询，通道现成），规避再分发风险；sidecar 崩溃不阻断热路径（降级为现状手写逻辑）。

- **优点**：漂移成本最高的四件事全部官方化；热路径与多账号模型零改动；sidecar 可灰度（逐功能接入、可独立开关）；契约测试可行——把逆向抓包（`capture.log`）作为 golden 回放，CI 检测上游协议漂移，取代"凭证探测"式验证。
- **缺点**：引入一个外部进程依赖（但可降级）；登录收割流程需要处理 keyring/文件两种存储（F5）；sidecar 与热路径的模型/指纹一致性需要一次性对齐逻辑。
- **工作量**：M（约 1 周，四个接入点相互独立）。

### 对比矩阵

| 维度 | A 纯直译 | B 全驱动 | C 混合（推荐） |
|---|---|---|---|
| 协议保真 | 中（手写追逐） | 高 | 中高（热路径手写 + 指纹官方供给） |
| 多账号经济 | 优（单进程池） | 差（N 进程） | 优（热路径单进程池；sidecar 按需短驻） |
| 计费/监控可见性 | 优（现状） | 中（需插代理层） | 优（现状） |
| 登录体验 | 弱（手工 RT） | 优 | 优（可脚本化批量） |
| 版本指纹维护 | 差（探测采纳） | 免维护 | 免维护 |
| 运维复杂度 | 低 | 高（进程编排+双层代理） | 中（单 sidecar，可降级） |
| 迁移工作量 | S | L | M |
| 生产风险 | 低 | 高（热路径重写） | 低（热路径不动，逐项灰度） |
| agent 模式 | 无 | 仍卡 F8 | 后续可解（sidecar 已在场） |

---

## 3. 推荐方案的落地计划（方案 C）

### 阶段 0：协议对齐（0.5–1 天，纯 Python，无副作用）

1. 默认端点切 `daily-cloudcode-pa.googleapis.com`（保留可配）；
2. `fetchAvailableModels` 解析改 protojson map 语义（F2），补全 ModelInfo 字段集；
3. 包装层字段对齐：`requestId` 格式 `chat/<uuid>` 与 `agent` 通道区分校验、`sessionId` 语义复核；
4. UA 模板补 `cl=<CL>` 段（F3），CL 初始值 998881661；
5. 补 `x-cloudaicompanion-trace-id` 透传（排障用）。

### 阶段 1：完整项目化（1–2 天）

1. `custom-overlay/files/` 内容合入主干，overlay 机制退役；
2. 上游 remote 保留只读，新设「上游挑拣」流程（周级人工评审，替代每日自动同步）；
3. CI 契约测试：`antigravity-re/run/capture.log` 转为 golden 用例——`wrap_cli_request` 输出与抓包逐字段 diff，漂移即红灯；
4. GHCR 发布改手动/评审触发。

### 阶段 2：agy sidecar 控制面（3–4 天，四个独立接入点）

1. **sidecar 管理器**：下载（updater manifest + SHA256 固定）、拉起（standalone/headless、随机端口、CSRF）、看护（崩溃重启/超时回收）、降级开关；
2. **登录 provisioning**：驱动 `Login` RPC + standalone 本地回调（F4），产出 RT 写 credentials.db（兼容现有 schema；keyring 不可用时文件存储路径对齐 F5）；
3. **模型目录**：`GetAvailableModels` 定时同步进 config store，`model_list.py` 改读官方源，真实性核查模块退役；
4. **指纹供给**：`--stamp` CL + LS 版本 → UA 模板；`antigravity_cli_version.py` 探测逻辑退役（保留 env 保底开关）；
5. **额度**：`retrieveUserQuotaSummary` 改经 sidecar 复核现有对账口径。

### 阶段 3（可选，另立 ADR）：agent 模式

打通 model_configs 链路（F8）后评估 `SendUserCascadeMessage` 暴露 agent 能力；或利用 F7 在 sidecar 出网侧插代理获得完整计费镜像。

### 回滚

每个阶段独立可回滚：阶段 0 字段级 feature flag；阶段 1 保留 overlay 分支只读归档；阶段 2 sidecar 全局开关，关闭即回现状。

---

## 4. 风险与开放问题（TBD）

| # | 事项 | 说明 | 建议 |
|---|---|---|---|
| TBD-1 | LS idle/负载内存真实值 | 沙箱测量失真，需生产环境实测 N 账号场景（影响方案 B/C 的 sidecar 驻留策略：长驻 vs 按需拉起） | 阶段 2 前补测 |
| TBD-2 | ~~standalone 登录的 keyring 依赖~~ | **已解（2026-10-14 ENV_CAPTURE §1）**：文件回退路径 `~/.gemini/jetski-standalone-oauth-token`，格式 = oauth2.Token 扁平 JSON；凭证池 RT 可直接注入，无需 keyring | 登录维持自建 callback，sidecar 凭证注入按此实施 |
| TBD-3 | 二进制再分发合规 | 按需下载 + SHA 固定是否满足 Google ToS；IDE 包 vs CLI 包哪个更适合 sidecar（CLI 包更小、即"agy exe"本义） | 阶段 2.1 先验证 CLI 渠道 |
| TBD-4 | ~~agent 模式 model_configs~~ | **已解（2026-10-14 ENV_CAPTURE §2/§3）**：枚举映射随 fetchAvailableModels 下发（ModelInfo.model 字段）；planModel 用真实枚举名（如 MODEL_PLACEHOLDER_M318）即可驱动 planner，57.6KB agent 完整请求已捕获 | 阶段 3 解锁，可提前评估 |
| TBD-5 | 上游非 AGY 部分挑拣成本 | GeminiCLI/Vertex 路由后续修复需手工合并 | 阶段 1 后观察一个月 |

> **决策更新（2026-10-14 libre 拍板）**：登录链路维持自建 callback，不做 sidecar 登录 provision；sidecar 范围收敛为「环境补充」——模型目录 / 版本指纹 / 额度三项。环境技术未知项已全部实测打通，详见 `antigravity-re/ENV_CAPTURE.md`。

## 5. 决策请求

1. 是否采纳 **方案 C（完整项目化 + agy exe 控制面补充）**？
2. 阶段 0（协议对齐）是否立即执行？
3. TBD-3：sidecar 二进制渠道偏好（CLI 优先 / IDE 优先 / 再评估）？

---

### 附：关键证据索引

- 逆向总报告：`/home/libre/project/antigravity-re/REPORT.md`
- 线格式抓包（脱敏）：`/home/libre/project/antigravity-re/run/capture.log`
- proto 产物：`/home/libre/project/antigravity-re/descriptors/`（550 个 .proto）
- standalone 登录日志（本地回调流）：会话 job bash-1094 输出（2026-10-14）
- gcli2api 协议层：`src/api/antigravity.py`（wrap_cli_request:242 / build_antigravity_headers:301）
- 版本追踪现状：`src/antigravity_cli_version.py`
