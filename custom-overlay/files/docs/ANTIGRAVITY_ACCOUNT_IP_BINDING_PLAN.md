# Antigravity 账号、出口 IP 绑定与 403 分类实施计划

## 实施状态（2026-07-30）

当前已完成：

- SQLite 固定保存账号的 `bound_proxy_node_id`，普通请求不再逐次轮换代理组节点。
- 保存出口 IP、国家/地区、绑定状态、资格状态与最近 403 分类。
- 使用真实 AGY CLI 格式构造可配置 User-Agent。
- 使用同一绑定代理执行 IP 回显与 `loadCodeAssist` 地区资格检测。
- 流式与非流式 403 都先分类，再决定重绑、模型冷却或账号禁用。
- 管理端账号卡片展示绑定节点、出口 IP 和资格状态，并提供检测与重绑入口。
- 根目录与 `custom-overlay/files` 保持字节一致。

检测默认关闭，避免在尚未提供可信 IP 回显服务时阻断已有账号。配置
`ANTIGRAVITY_EGRESS_IP_CHECK_URL` 后，将
`ANTIGRAVITY_NETWORK_CHECK_ENABLED` 改为 `true`。Docker Compose 环境变量变化需要重建容器
（例如 `docker compose up -d --force-recreate`），但不需要重建镜像；直接进程部署则需要重启进程。不需要修改凭证文件。

## 1. 目标

建立可追踪的账号请求链：

```text
credential -> proxy node -> egress IP -> eligibility check -> API request
```

一个 Antigravity 凭证文件代表一个账号。代理组只负责为账号分配节点，普通请求不得逐次轮换节点。Token 刷新、`loadCodeAssist`、模型/额度查询和生成请求必须使用同一账号绑定的代理节点与出口 IP。

403 必须被处理，但不能只按 HTTP 状态码无条件禁用账号。系统需要结合出口 IP、地区资格检测结果和响应内容区分账号问题、地区限制、代理漂移、模型权限和未知拒绝。

## 2. 架构约束

- 仅修改 Antigravity 路径，不扩展 GeminiCLI 或 Vertex 行为。
- SQLite 是账号绑定与健康状态的事实来源。
- Redis 不作为该功能的必需依赖。
- 代理凭据继续保存在账号凭据 JSON 之外，API 与日志不得暴露代理密码。
- 根目录定制必须同步至 `custom-overlay/files`。
- 新功能采用附加式数据库迁移，不破坏已有账号。
- 检测开关启用后，未完成检测或检测失败的账号不能进入业务请求池。
- 不随机轮换 User-Agent、版本、平台或认证方式。

## 3. 数据模型

### 3.1 `antigravity_credentials`

增加：

```text
bound_proxy_node_id INTEGER NULL
```

`proxy_group_id` 表示账号属于哪个代理组；`bound_proxy_node_id` 表示该账号当前实际绑定的节点。代理组的 `round_robin`、`random`、`failover` 策略只在首次绑定或重新绑定时执行。

### 3.2 `antigravity_account_health`

```text
credential_name          TEXT PRIMARY KEY
egress_ip                 TEXT
egress_country            TEXT
binding_status            TEXT NOT NULL DEFAULT 'unchecked'
eligibility_status        TEXT NOT NULL DEFAULT 'unchecked'
eligibility_reason        TEXT
eligibility_checked_at    REAL
last_403_category         TEXT
last_403_reason           TEXT
last_403_at               REAL
updated_at                REAL NOT NULL
```

允许的核心状态：

```text
binding_status:
  unchecked | healthy | proxy_drift | proxy_failed

eligibility_status:
  unchecked | eligible | geo_blocked | account_blocked | error
```

## 4. 固定代理绑定

```text
账号已有健康绑定
  -> 复用 bound_proxy_node_id

账号未绑定
  -> 按代理组策略选择节点
  -> 原子写入 bound_proxy_node_id
  -> 执行出口 IP 与地区资格检测
  -> 检测成功后进入请求池

节点失效、IP 漂移或地区不合格
  -> 标记当前绑定不可用
  -> 重新分配节点并持久化
  -> 重新检测
```

同一账号的 Token 刷新、Project ID 获取、用户信息、额度、模型列表及生成请求必须携带同一 `proxy_url`。重新选择凭据时必须整体切换账号、Token、Project ID、代理节点和计费归属。

## 5. 出口 IP 与地区资格检测

新增账号健康检测服务，使用绑定代理依次执行：

1. 调用可配置的 IP 回显接口获取真实出口 IP。
2. 首次检测时保存 IP；后续检测发现变化时标记 `proxy_drift`。
3. 通过同一代理刷新 Token（如有需要）。
4. 通过同一代理调用 `v1internal:loadCodeAssist`。
5. 解析账号是否已激活、是否具备项目、是否因地区不合格。
6. 原子保存检测结果。

检测触发点：

- OAuth 登录或账号导入完成后。
- 账号首次使用前。
- 检测结果超过 TTL 时。
- 收到 403 后强制重新检测。
- 管理后台手动触发。

建议配置：

```dotenv
ANTIGRAVITY_NETWORK_CHECK_ENABLED=false
ANTIGRAVITY_NETWORK_CHECK_TTL_SECONDS=1800
ANTIGRAVITY_EGRESS_IP_CHECK_URL=
```

优先使用自建 IP 回显接口。配置好接口后再启用检测开关。未配置回显接口时，不得伪造或猜测出口 IP；账号保持 `unchecked`，由管理端明确提示缺少配置。

## 6. User-Agent

真实 AGY 请求格式：

```text
antigravity/cli/{version} (aidev_client; os_type={os}; arch={arch}; auth_method={auth})
```

建议配置：

```dotenv
ANTIGRAVITY_CLI_VERSION=1.1.8
ANTIGRAVITY_CLI_OS_TYPE=linux
ANTIGRAVITY_CLI_ARCH=amd64
ANTIGRAVITY_CLI_AUTH_METHOD=consumer
```

同一构造函数必须覆盖 OAuth 后项目检测、健康检测、额度/模型查询、流式请求和非流式请求。不得按账号随机变化。

## 7. 403 分类与动作

| 分类 | 判断依据 | 动作 |
| --- | --- | --- |
| `geo_blocked` | `loadCodeAssist` 或响应明确表示地区不可用 | 标记绑定/节点不可用，不禁用账号 |
| `proxy_drift` | 实际出口 IP 与已绑定 IP 不一致 | 立即阻止账号请求，重新绑定并检测 |
| `proxy_failed` | 代理连接、TLS、隧道失败 | 冷却代理节点，账号等待重新绑定 |
| `account_forbidden` | 已验证合格的稳定 IP 下仍明确拒绝账号 | 禁用账号或进入人工审核 |
| `model_forbidden` | 仅特定模型被拒绝 | 设置账号与模型级状态，不禁用整个账号 |
| `unknown_403` | 无法可靠归类 | 短期冷却并返回原始 403，不遍历全部账号 |

当前 `AUTO_BAN_ERROR_CODES=[403]` 的无条件禁用行为需要改为分类后的动作。一个坏出口不得触发大量账号被连续检测、禁用或重试。

只有分类为 `account_forbidden` 且 `AUTO_BAN=true`、403 位于自动禁用码列表时才自动禁用账号；其他 403 仍会保存分类与原因，但不会误封账号。

## 8. 请求路径接入

凭据只有同时满足以下条件才可被选择：

```text
disabled == false
bound proxy node is available
binding_status == healthy
eligibility_status == eligible
model cooldown is inactive
```

发生 403 时：

1. 保存原始响应的安全摘要。
2. 强制执行该账号绑定的出口 IP 与地区资格检测。
3. 调用 403 分类器。
4. 按分类更新账号、代理节点或模型状态。
5. 仅在已完成新绑定并检测成功后允许一次受控重试。

流式和非流式请求必须使用相同状态机。

## 9. 管理接口与展示

建议接口：

```text
GET  /creds/network-health/{filename}
POST /creds/network-health/{filename}/check
POST /creds/network-health/{filename}/rebind
POST /creds/network-health/batch-check
```

管理端展示：绑定代理组、节点名称、出口 IP、地区、资格状态、最近检测时间、最近一次 403 分类及时间。代理密码继续脱敏。

## 10. 验证顺序

1. SQLite 迁移与旧数据库兼容测试。
2. 同一账号连续选择 100 次仍使用同一节点。
3. 多协程首次绑定只产生一个最终节点。
4. 出口 IP 漂移会阻止业务请求。
5. 地区不合格不会禁用账号。
6. 稳定合格 IP 下的明确账号拒绝会禁用账号。
7. Token、Project ID、Proxy 和计费归属在重试时整体切换。
8. 流式与非流式行为一致。
9. 根目录与 `custom-overlay/files` 字节一致。
10. 单账号、单代理完成 `loadCodeAssist`、一次最小生成及 10 次串行灰度验证。

未经明确授权，不构建镜像、不推送镜像、不重建或重启容器。
