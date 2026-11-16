# OAuth 刷新失败留证

Antigravity 的 `refresh_access_token_with_clients` 在非 200 响应或请求异常时，向现有应用日志输出一条 `[OAUTH_EVIDENCE]` JSON。适用于凭证池刷新与直接 RT 登录，不增加网络请求、重试或客户端切换，不改变错误分类及账号禁用逻辑。普通 Gemini CLI 刷新、授权码兑换不在本次覆盖范围。

## 查询与关联

部署包含此次改动的版本后，可从容器日志查询：

```bash
docker logs --since 24h gcli2api 2>&1 | rg 'OAUTH_EVIDENCE|oauth_event_id='
```

- `event_id`：单次失败事件 ID；原有 `Token刷新失败 <filename>` 日志中的 `oauth_event_id` 指向该事件。诊断描述不会进入对外的 TokenError 文本。
- `timestamp_utc`、`duration_ms`、`http_status`：UTC 时间、调用耗时、HTTP 状态；网络异常没有 HTTP 状态。
- `error`、`error_description`、`error_subtype`：仅保留字符串字段，脱敏且各限 512 字符；缺失或类型不符合则为 null。`invalid_grant` 仍不等于账号永久封禁。
- `credential_ref`：刷新凭证 SHA-256 前 24 个十六进制字符，可关联同一 RT；重新授权或 RT 轮换后会变化，并非永久账号 ID。
- `client_key`、`transport_config`：选择的 OAuth 客户端配置名、请求开始时传输配置。不是抓包或实际 TLS 握手证据。
- `timing_id`：模型 API 请求存在计时上下文时可关联调用；后台操作可能为 null。
- `endpoint_ref`、`proxy_config_ref`：目标 URL 和显式代理配置的摘要，不输出 URL、代理密码。默认代理标记 `default_unresolved`，不冒充已解析出口；显式直连为 `direct`。
- `exception_type`：网络异常只记录异常类型，不记录可能包含敏感 URL 的异常文本。

出口摘要表示配置关联，不能证明实际公网 IP；本次未采集客户端版本、上游 TLS 或实际出口。后续分析应结合发布版本记录与代理侧记录。

## 脱敏与保留限制

只读取不超过 64 KiB 的 JSON 响应，并仅选取上述三个 OAuth 错误字段。已知请求/响应凭证及其 URL 编码形式、URL、邮箱、常见秘密键值和长不透明字符串均会脱敏；不记录响应全文、响应头、请求表单、prompt。非 JSON、超限或数组响应只留下状态和 `body_format=omitted`。自由文本脱敏属于尽力识别，不应把这些运维日志公开发布。

日志沿用现有日志开关、级别、文件轮转和 Docker 日志保留策略，并非独立永久审计库。需要保留事件时应及时导出相关日志。留证失败不会替换原来的 OAuth 异常，成功响应不输出此事件。此功能无法补回 2026-11-14 至 16 日已经丢失的上游详情。
