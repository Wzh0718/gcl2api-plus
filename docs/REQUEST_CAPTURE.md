# 有界请求证据采集

采集 GeminiCLI、Antigravity、Vertex 的推理请求，包括 OpenAI Chat / Responses / Images、Anthropic、Gemini 兼容入口、流式与非流式、假流式、抗截断、换号、模型降级和图片容量重试。OAuth、面板及凭证管理请求不采集。

默认总开关关闭。实现发布后，在桌面或手机面板「配置 → 请求证据采集」启用并保存。各项均可热更新；对应非空环境变量优先且锁定面板字段，Compose 默认传入空值以保留面板控制权。仅目录配置 `REQUEST_CAPTURE_DIR` 是环境变量专属，需要重启。不要通过旧 `CALL_CONTENT_CAPTURE_ENABLED` 开关启用新功能。

## 四种策略及独立预算

| 策略 | 默认容量硬上限 | 最长保留 | 暂停写入的剩余空间 |
| --- | ---: | ---: | ---: |
| attempts：上游尝试与网关最终结果 | 256 MiB | 336 小时 / 14 天 | 64 MiB |
| features：请求特征 | 256 MiB | 336 小时 / 14 天 | 64 MiB |
| normal：普通请求正文 | 2048 MiB | 24 小时 | 1024 MiB |
| errors：异常证据及此前成功样本 | 1024 MiB | 168 小时 / 7 天 | 256 MiB |

每种策略的四个配置键为：

```text
request_capture_{attempts|features|normal|errors}_enabled
request_capture_{attempts|features|normal|errors}_max_mb
request_capture_{attempts|features|normal|errors}_retention_hours
request_capture_{attempts|features|normal|errors}_min_free_mb
```

环境变量是相同名称的大写形式，例如 `REQUEST_CAPTURE_ERRORS_MAX_MB=1024`。容量范围 1–1048576 MiB，保留时间 1–8760 小时；不允许用 0 或负数表示无限。关闭某类采集请用该类 `enabled=false`，已有数据仍按容量和保留期清理，支持关闭后检索。

独立采集目录默认是 `$CREDENTIALS_DIR/request-capture/`。启用默认配置后，四个策略文件预算合计 **3584 MiB（3.5 GiB）**，另有最多 64 KiB 的小型控制文件和根目录预算。各策略不相互借用空间。采集目录必须位于持久卷中。

预算限制的是该功能独立存储，包括策略目录和分段文件的实际分配块；不是整个服务器或主 credentials.db 的限制。原有账单、调用监控摘要、应用日志、Docker 日志和备份各自占用空间，需要分别维护。旧正文仍可读取，本次不自动转换或删除历史数据库内容，也不自动执行 VACUUM。新推理请求的正文不再重复写进主账单库，主库只存采集关联 ID。

## 公共配置

| 配置键 | 默认值 | 用途 |
| --- | ---: | --- |
| request_capture_enabled | false | 总开关 |
| request_capture_min_free_mb | 64 | 所有策略统一停止写入的磁盘剩余空间底线；每类实际使用两者较大值 |
| request_capture_max_entry_kb | 256 | 单条证据未压缩上限，1–4096 KiB |
| request_capture_max_body_kb | 4096 | 入站正文观察缓冲上限，1–16384 KiB；超限继续原样转发请求，仅省略采集内容 |
| request_capture_error_body_kb | 16 | 错误响应正文上限，1–256 KiB |
| request_capture_context_count | 5 | 每次上游异常复制该账号此前成功对照样本数，0–50 |
| request_capture_cleanup_seconds | 60 | 无流量时后台清理间隔，5–3600 秒 |
| request_capture_export_max_mb | 16 | 查询/导出响应的大小预算，1–128 MiB |

正文在复制时就限制大小、深度和节点数，再压缩写入。超限会留下 `capture_truncated` / `capture_omitted`；如果整个记录仍超过单条上限，保存带 SHA256、原字节数及 `capture_state=entry_too_large` 的占位记录。媒体内联数据和 data URI 只保留哈希、大小和类型，不保留 base64。该机制是有界诊断快照，不承诺任意大小请求都能原样重放。

## 记录的请求逻辑

- 每个入站推理请求生成稳定的采集 ID，响应头 `X-GCLI-Capture-ID` 返回该值。关联认证通过的 API Key ID，不采集 Key 明文、请求认证头、Cookie 或 OAuth 凭证。
- 在实际生成调用入口记录开始尝试，在成功、HTTP 错误、连接异常、取消和流关闭时记录结束。同一入站请求的全部尝试共享采集 ID，但每次有独立 attempt_id 和序号。开始记录表示启动一次尝试，不保证进程崩溃前请求已发到上游；没有结束记录时应视为结果未知。
- 每次尝试保存账号、原始请求模型、当前实际请求模型、目标 host/path（排除 URL query）、代理 host、可获得的绑定节点/组信息、时间、状态和白名单响应 trace / retry 标识。不记录代理密码或原始代理 URL。入口路径中的兼容 `/key=...` 段也不保存。
- 特征采集独立于正文开关，保存客户端及上游 system/messages/tools/parameters 的哈希与大小、消息和工具数量，并保存 project/model/userAgent/requestType/sessionId/labels/metadata 等可获得信封字段的独立哈希；入站超过观察上限时仍保留入站字节流 SHA256 与长度。上游实际 sessionId、labels 和信封字段保存在有界上游正文中。
- 非流式上游 403/429/5xx、HTTP 200 中的有界 JSON/SSE 错误、Vertex GraphQL 逻辑鉴权/配额错误、连接错误和中断都会进入异常策略。即使后续换号成功，早先失败也保留。
- 账号并发槽位等待超时记录 `kind=local_queue`，不冒充上游 429。入站最终结果还记录实际尝试次数，所以没有发出上游请求的拒绝、鉴权/参数错误也可区分。
- 异常时将普通策略中该账号最近的成功样本复制到异常区，以免普通区较短保留期很快抹去对照。样本保留 source_request_id/source_created_at，按触发请求 ID 查询可同时找到这些样本。若此前未启用普通正文、样本已淘汰或异常区拒绝写入，就无法补齐。
- 成功响应正文不采集；异常响应只在预算内采集。密钥/令牌结构字段脱敏，但用户提示词与工具结果本身仍属于私有内容，读取必须通过面板认证。

## 磁盘与故障行为

分段独立校验与压缩；写前按分配块计算容量，先淘汰最旧分段，再使用 posix_fallocate 精确分配本次写入区域，避免文件系统推测性追加预分配突破预算。跨 worker 使用 Linux 文件锁，避免多个 worker 各自误用完整预算。单分段最多 4 MiB，小预算按比例缩小，另预留目录增长空间；不创建无界 WAL 或单独无限增长的索引。文件系统不支持区域预分配时按存储错误跳过采集并计数，不退回无界写入。

容量调小后，保存配置会立即执行清理。按最早到达的限制淘汰数据：容量硬上限优先，不保证一定留满设置的天数。按整段淘汰可能提前删除段内相邻记录；查询还会逐条检查保留期限，避免未过期邻居使过期正文继续可见。后台清理在无流量时仍运行。

剩余空间低于阈值时跳过对应策略写入并计数，正常转发请求；达到全局底线时所有策略暂停。存储权限、磁盘写入失败或繁忙锁也不得改变业务请求结果。计数控制文件大小固定有界，磁盘完全不可写时计数只能留在进程内，不能承诺跨崩溃保存。异常退出留下的损坏尾部在下一次写入时校验并截断恢复。

## 查询、导出与可用性

面板「调用监控」可从单条记录打开内容并导出该采集 ID 的尝试/异常证据；配置区可以按采集 ID 或完整凭证文件名下载证据包，并刷新各策略实际占用、预算和丢弃计数。

接口均要求面板 Bearer 认证，返回 `Cache-Control: no-store`：

```text
GET /call-monitor/capture/status
GET /call-monitor/capture/records?policy=attempts&request_id=<capture-id>&limit=100
GET /call-monitor/capture/records?policy=errors&credential_name=<filename>&since=<unix-time>
GET /call-monitor/capture/export?request_id=<capture-id>
GET /call-monitor/capture/export?credential_name=<filename>&since=<unix-time>
```

limit 范围 1–200。返回 next_before 时可用 before 时间戳继续向前查询；导出按策略返回各自的 next_before。单次预算或条数限制触发时 `truncated=true`。单条记录本身超过导出预算时也会标记不完整，可提高导出预算重试。大历史检索在分段之间释放锁，避免长时间阻塞采集。

现有 SSH 可读取持久卷，但推荐通过认证导出保留完整结构。工具依赖当前服务器访问权限，不依赖聊天记忆。关闭、超限省略、过期淘汰、空间不足、取消及异常退出都可能造成内容不全；查询为空不能证明请求从未发生。历史未采集内容无法补回。

## 验证

专项测试覆盖独立物理预算、跨线程/进程并发、重启、容量缩小、过期、损坏尾部、低磁盘、单条大小、敏感字段和图片省略、协议入口、内部失败后成功、异常前成功样本、原生传输、Vertex、面板认证、环境锁定及导出不完整标记。

```bash
ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED=false .venv/bin/python -m pytest tests/ -q
.venv/bin/python -m compileall -q config.py web.py src/
node --check front/common.js
```

本地全量测试关闭私有 .env 的原生传输默认开关，专项测试自行覆盖启用路径。新实现需要按仓库发布流程提交、验证并部署后才会在生产生效。
