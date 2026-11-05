# AGY 反代与掉号排查（2026-11-04）

检查时间：Asia/Singapore 12:00–12:15。范围：本地未提交改动、既有官方客户端捕获、远程 Docker 与只读 SQLite/日志。未发送生成探针，未修改服务器配置、账号状态或部署。账号名和出口均使用摘要。

## 结论

存在真实的服务级 TOS 封禁，也存在程序停用和调度排除，不能全部归因于客户端特征识别。远程尚未运行 Kimi 新增的 Go 原生传输，因此本地传输对齐不能解释为线上已经生效。传输差异可以证实，但服务端具体风控规则和封禁触发原因无法由这些证据确定。

## 线上部署

- 容器：`gcli2api`，镜像：`ghcr.io/wzh0718/gcl2api-plus:latest`。
- 镜像 revision：`3c1a23a32e278464b25eae06826bdd303b594bd8`。
- 镜像 ID：`sha256:743ca807dfbb5ad0bab247cb0bb9be74a00d975fd64666bb59ac716f7d6029cd`。
- 2026-11-03 创建并启动，检查时 running、OOMKilled=false。
- 仅挂载 `/opt/gcli2api/data/creds` 至 `/app/creds`，没有源代码挂载。容器中不存在 `/app/src/agy_native_transport.py`。
- 公开前端脚本与本地 HEAD 字节相同，与本地工作区不同。
- 实际 hub 配置：2.28.2 / CL 1009437749；环境开启网络健康检查。
- 数据库约 1.9 GB。查询均为只读，聚合不是严格的同一时刻快照。

## 账号状态：48 个当前账号，44 个 disabled

按当前账号的错误记录与健康记录合并归类，以下四组互斥：

| 已停用账号的证据 | 数量 | 能说明什么 |
|---|---:|---|
| 明确 `TOS_VIOLATION` | 20 | 上游明确停用了该产品服务；不能等同于整个 Google 账号被封，也不能据此识别具体触发信号 |
| `VALIDATION_REQUIRED` | 3 | 需要账号验证；与永久 TOS 封禁不同，不能自动完成验证或直接当作已恢复 |
| `SUBSCRIPTION_REQUIRED` | 2 | 产品许可/订阅资格错误，与 TOS 不同 |
| 只有 `[400]`，无上述明确原因 | 19 | 没有足够证据认定真实封禁，需要取得原错误并核查停用来源 |

TOS 组内有 3 个账号也保留 `[400]`；验证组内有 1 个也保留 `[400]`。因此不能只按 error_codes 计数。健康表还存在不对应当前凭证的记录，不能直接把健康表总行数当作账号总数。

当前配置为 `auto_ban_enabled=true`、`auto_ban_error_codes=[400,403]`。`src/api/utils.py` 的 `handle_error_with_retry` 在状态码命中配置后调用 `handle_auto_ban`，整号 disabled，再考虑换号重试。400 没有按请求参数、模型协议、账号失效细分。这给“请求错误导致程序停用，再把相同请求交给另一个账号”的连锁停用留下路径；尚未取得这 19 个账号最初的响应，不能证明每一个都由这条路径停用。

补充：token 刷新里的 `_is_permanent_refresh_failure` 也有宽泛的 400/401/403 分支，但 AGY 的 `TokenError` 当前不附带结构化 status_code，调用者可能落到文本匹配。不能把普通生成请求的 400 和 OAuth 的 `invalid_grant` 混作同一种故障。

## 实际可选池比面板 enabled 数量更小

4 个未 disabled 的账号：

| 账号摘要 | 网络策略 | 最近记录的出口摘要 | 养号状态 | 普通调度是否会被状态排除 |
|---|---|---|---|---|
| `4030c0716a` | inherit | `6bc7c41a6f` | 无显式记录，默认 off | 否 |
| `c64b18bae7` | inherit | `6bc7c41a6f` | 无显式记录，默认 off | 否 |
| `c65f89dc53` | inherit | `6bc7c41a6f` | cancelled | 是 |
| `c48e03ed13` | group，节点 11 | `9fd9374374` | cancelled | 是 |

`_get_next_antigravity_credential` 明确排除 paused/cancelled/account_forbidden。故检查时普通调度最多只有 2 个候选，且二者最近健康记录显示相同出口；模型冷却、资格等仍可能进一步缩小池。cancelled 的语义是否符合用户意图需要单独确认，不能未经检查就重启这些账号。

全局数据库 proxy 为空；容器 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、对应小写变量和 PROXY 均未配置。inherit 因而没有应用层独立代理配置，仍不排除宿主机网络层转发。历史上还发现一个节点绑定 8 个账号、一个出口有 38 条健康记录；这些包含停用和历史账号，不能据此声称 38 个账号正在同时出网。

当前节奏配置：单账号并发上限 13、基础间隔 80ms、抖动 50%、槽位等待 2s；429 重试开启，间隔 5s、最多重试 3 次。13 是配置上限，AIMD、实时在飞数和其他门控可能降低实际并发，不能当作持续实测值。两个普通可选账号未开启 warming，不能假设养号的 IP 租约正在给它们限流。

## 调用记录

首次查询时滚动 7 天的 Antigravity `call_records`：

| 最终记录状态 | 条数 |
|---|---:|
| 200 / success | 687,036 |
| 429 | 9,575 |
| 400 | 26 |
| 403 | 31 |
| 401 | 1 |
| 404 | 7 |
| 502 | 1 |
| 503 | 3 |
| 无状态码失败 | 57 |
| 500 | 0 |

11 月 4 日第一次查询时，已有 20,438 条成功和 1,714 条 429 记录。最后采样的失败集中在 12:06–12:07、模型 `gemini-3.8-flash-low`。这些是网关调用记录，可能包含本地背压/养号拒绝，不能把全部 429 认定为 Google 上游风控或把成功条数认定为独立用户数。上游尝试、重试和健康检查未必与最终调用记录一一对应。

未在这一窗口的最终调用记录中发现 500，不代表用户此前没有遇到 500，也不排除中间尝试、其他路径或客户端合成错误。19 个无明确原因的 400 停用账号缺少原响应，不能从最终状态补造原因。

补充读取应用日志最近约 3 MB：`无可用` 匹配 9,430 行，`PACING` 匹配 1,989 行；末尾可见“本地槽位繁忙，尝试切换其他账号”以及 `gemini-2.5-flash-lite → gemini-3.1-flash-lite → gemini-3.8-flash-low` 的无可用凭证降级。日志行数不等于独立请求数，但证明当前确实存在本地背压和调度拥塞。该片段没有上游 400/403/500 响应，不能据此复原历史封禁触发时的请求。

`call_count` 在选择凭证时递增，可能包含选择后未出网、重试和历史初值，不应用它计算每账号真实发送量。

## 与官方 AGY 的对比

基准为仓库既有 2.28.2 dummy/loopback 捕获与 `tests/fixtures/agy_transport_2.28.2.json`，不是本次重新抓取线上流量。

| 层面 | 远程已部署版本 | 本地 Kimi 工作区 | 证据和限制 |
|---|---|---|---|
| hub UA / CL | 配置已为 2.28.2 / 1009437749 | 同版本 | 不能把问题简化为旧 UA |
| labels、model_enum、会话包装 | 已有对齐实现 | 延续该实现 | 请求内容仍取决于下游输入；格式相似不证明行为相同 |
| CloudCode TLS / HTTP | Python httpx 默认传输 | 可选 Go helper，CloudCode HTTP/1.1、无 ALPN | 远程没有 helper；已有 loopback 抓包显示 httpx 与官方传输形状不同 |
| OAuth TLS / HTTP | Python 传输，旧 UA 和 client 回退逻辑 | Go helper 的 OAuth profile，HTTP/2、Go UA；固定一个 client 尝试 | 本地开关已开启，远程未部署；固定 client 仍须与 RT 的实际授权客户端对应 |
| 出口分配 | 普通代理组可共享；当前候选共用出口 | 新增 exclusive_assignment 与严格健康门控 | 本地配置不是服务器配置；独占节点不等于自动获得有效或独占的最终出口 |
| 流量行为 | 多账号网关、配置单号并发 13 / 80ms | 仍是网关 | 传输一致不代表配额、订阅资格、账号验证和行为约束相同 |

既有捕获的官方 OAuth JA3 为 `03117a8ed39ef02427ebbc39f121275c`，CloudCode 为 `9b7dcdf3f997f1fb7b4409c94cb7ef36`；本地 native e2e 捕获存在相同值。JA3 仅覆盖握手部分特征，不能由此推导“不可识别”或“不会掉号”，也不能推导服务端实际使用了 JA3 进行此次处置。

## 后续处理顺序

1. 优先补足 400 的错误分类与脱敏 reason 记录，避免普通请求错误直接整号停用。对上述 19 个账号逐一核查资格后才决定是否恢复，不批量解封 TOS/待验证账号。
2. 澄清 cancelled 的产品语义，让面板同时显示 enabled、可调度、冷却和待验证数量，避免误以为仍有 4 个账号承载请求。
3. 对现有合法可用账号按配额与实际出口设置负载预算；记录本地限流、上游限流和重试次数，先解释 429 再调并发。
4. 将 Kimi 的原生传输改动完成验证后再做小范围部署比较，固定请求、账号资格和负载条件；不以真实封禁账号反复探针验证，也不把传输对齐当作可规避服务限制的保证。

## 本次验证

- Go helper：`go test -count=1 ./...` 实际通过。
- 403 分类与 TOS 处置专项：14 passed。
- 较大 Python 组合未完整通过验证：运行停滞，已停止/由超时终止；单独复现的堆栈停在 asyncio runner teardown。不能报告全量测试通过。
- 没有发布、重启容器、修改服务器数据库或启停账号。本地业务代码和 Kimi 原有改动均保留。

## 400 路径复核（第二次检查）

当前快照有 23 个 disabled 账号携带 `[400]`，23 个的 `error_messages` 均为空。其中健康状态 19 个为 eligible、1 个 validation_required、2 个 account_blocked、1 个 unchecked。健康状态与最初的 400 不一定同时产生，不能把 eligible 当作账号当前仍可用的证明，也不能用其他时间的 TOS 记录反推出 400 的内容。

| 400 的来源 | 当前代码处理 | 是否可当作永久封禁 |
|---|---|---|
| 本地模型目录/枚举校验 | `_model_enum_error_response` 返回 INVALID_ARGUMENT；未进入上游错误自动停用分支 | 否 |
| 文本生成上游 400 | 命中 `[400,403]` 配置后进入通用 `handle_error_with_retry`，会整号 disabled；可继续换号重试 | 单凭状态码不能判断 |
| 图片生成上游 400 | 配置把它列入 retryable，可换账号；当前图片循环没有调用通用 `handle_auto_ban` 的 400 分支 | 单凭状态码不能判断；与文本路径不同 |
| AGY OAuth 刷新 400 | 目前 `TokenError` 没有结构化 status_code；凭证管理器可能按文本匹配 invalid_grant 等词判失效 | 要区分授权失效与 client/请求配置问题，不能等同 TOS |
| 非 AGY 普通 OAuth 刷新 400 | 异常附带 status_code；`_is_permanent_refresh_failure` 的宽泛 400 分支会判永久失效 | 当前实现过宽，不能作为通用语义 |
| 登录/资格探测 400 | loadCodeAssist/onboardUser 路径抛异常；健康检查记 error 等状态，不等于普通文本生成的自动停用路径 | 需要端点、reason 和请求上下文 |

Google 的 OAuth 文档说明，刷新时 `invalid_grant` 可表示 token 已过期或被作废，而非直接表示 TOS 封禁；应按实际错误类型处理并核查参数：
[OAuth 2.0 for Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app#errors)。

此次仅检查差异，未修改 400 策略或批量恢复账号。原生传输单独整理在 `/tmp/gcli2api-native`、分支 `codex/agy-native-transport`，保留原有 OAuth client 回退策略，不包含本地代理组独占/严格健康门控和本报告。
