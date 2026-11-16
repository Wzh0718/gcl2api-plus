# 刷新、授权与会话审计

结构化审计不保存 token、代理密码或提示词正文。账号/授权版本使用 SHA256 前 24 位关联；request-capture 的协议字段沿用已有 fingerprint 算法。两类摘要算法的输入不同，不应混用。

## 保存位置

- `creds/operational-audit/attempts/*.seg`：刷新生命周期、OAuth HTTP 尝试、脱敏失败证据、实际出口回显采样。复用进程安全的 CaptureStore 帧格式，30 天/256 MiB 双上限，低于 64 MiB 可用空间停止写入；写入失败输出 OPERATIONAL_AUDIT 警告。独立于请求正文采集开关。到期/容量淘汰在写入时执行，安静期文件可能继续存在，查询仍按保留期过滤。
- SQLite `credential_audit_events`：store/CAS/delete 的结果、旧/新/期望授权版本和调用来源。成功事件与凭证写入在同一事务提交；审计写入失败时该次凭证写入也回滚。CAS 冲突/租约拒绝只提交审计，不修改凭证。保留最近 30 天、最多 100000 条，写入时清理。
- 现有 request-capture features：每个关联标签的独立摘要、requestId 中 conversation/trajectory 摘要、request_step/last_step_index 数字。attempts/features 还带实例、PID、构建 revision、账号摘要、授权版本摘要、代理配置摘要及会话分配来源/后端/key_ref/step。

`schema_version=1`，`instance_id` 标识进程启动实例，镜像通过 APP_BUILD_REVISION 写入构建 commit。GitHub Action 同时发布 `sha-<完整 commit>` 标签，部署应先核对 OCI revision 再固定镜像 digest。

## 事件关联

- `refresh_requested`：调用入口、到期余量、created/joined；同一管理器等待者共用 refresh_event_id。
- `refresh_started/finished`：协调操作边界。finished 的 credential_available 表示取得可用凭证，不代表发过 OAuth。
- `refresh_reused/skipped`：复用新凭证或账号已删除。
- `refresh_lease`：首次等待、获取、冷却、超时、丢失、释放；不为每个轮询写日志。
- `oauth_operation_started/succeeded`：管理器调用 Credentials.refresh 的边界。
- `oauth_http_started/finished`：Antigravity 实际 token HTTP 调用，独立 network_event_id；首次 RT 导入也会记录，但没有已有账号的 refresh_event_id。普通 Gemini CLI 的 HTTP 层尚未接入这一事件。
- `oauth_failure_evidence`：与已有 OAUTH_EVIDENCE 使用相同 oauth_event_id，error/subtype/description 经过脱敏及长度限制；网络异常只写类型。
- `refresh_write_result` 与数据库 credential_write：确认 OAuth 成功之后的结果是否真的持久化。
- `egress_observation`：只记录真实低频回显探测，缓存命中不伪造新观测。route_ref 可与同显式代理配置的 proxy_config_ref 关联；继承默认代理仍可能无法直接匹配。

数据库授权审计的 source 是调用方法名，不是操作者身份。外部脚本直接改库绕过应用，无法由这些事件追踪。异常退出会出现有 started 无 finished，不能直接判成远端失败。

## 会话及网络边界

无显式 sessionId 的请求标为 independent_request/new_request；显式会话标为 explicit，分配后端为 redis 或 memory。审计信息仅存在本地上下文，不加入发往 Google 的 JSON。Redis 回退仍不保证跨 worker 连续，日志可识别回退。

记录的代理配置、回显出口不是 Google 实际出口/TLS 的证明。没有增加定期探测、强制刷新、额外生成或伪造遥测。历史日志无法补回。

高频请求的长期结构证据仍受 attempts/features 容量约束；部署时根据磁盘空间增加这两类预算，不无限延长普通正文。审计失败不改变 OAuth 请求结果，但授权审计失败会阻止凭证提交，以免出现“成功改授权却没有同事务记录”。
