# AGY Hub 2.31.0 特征采集（2026-11-11）

## 结果与采用策略

- 官方 Hub 最新版本为 **2.31.0**，execution id `6379060755038208`。
- 官方二进制 `--stamp`：CL **1017595969**，构建日期 **2026-11-10**。
- 本项目更新 Hub 版本与 CL 默认值；已有面板/环境变量覆盖仍优先。
- **CloudCode 继续使用 HTTP/1.1，无 ALPN；OAuth 保持 Go HTTP/2。** 官方采用 HTTP/2 不表示服务端要求 HTTP/2。本轮有效凭证实测：新 UA + HTTP/1.1 调用 `fetchAvailableModels` 返回 200，共 21 个模型。
- 仅失效 Bearer 的 HTTP/1.1 探测返回 401，只能证明协议和端点可达；上述有效凭证的 200 才是业务可用证据。

## 样本与 REA 证据

下载源：
`https://storage.googleapis.com/antigravity-public/antigravity-hub/2.31.0-6379060755038208/unsigned/language_server-linux-x64`

- ELF x86_64，大小 `206811536` 字节。
- SHA256：`e49711c4f7b2fa0e62c0e130c9566579423094b63647bdce82f4ddd81eeb983f`。
- 动态依赖：libresolv、libpthread、libm、libdl、librt、libc、ld-linux。
- REA `capture_process_scenario --stamp`：`ev_34aa8025cd030d936f24e29192d2281f5dc43c0b2d659039ade3cb650765695e`。
- REA 隔离启动：`ev_73d15960325dec9816bc6ef8c33aa550fa5239c5e5d41df76b56776bcfda6130`。

限制：本机 Linux 的 REA `observe_native_calls` 不可用，因此本轮是 **REA 受控进程采集 + 独立 MITM 网络捕获**，不是函数级 hook。REA 的进程证据本身不观察网络；网络结论来自下述捕获。

## 官方客户端实际网络特征

隔离 HOME，文件存储注入 dummy access/refresh token；HTTPS 统一指向本地 MITM，使用测试 CA。MITM 本地返回 OAuth/CloudCode 模拟响应，不向真实服务转发这些 dummy 请求。没有修改官方二进制。

启动参数：`--standalone --override_ide_name antigravity --subclient_type hub --override_ide_version 2.31.0 --http_server_port 18690 --https_server_port 18691 --csrf_token isolated-capture --app_data_dir antigravity-test --cloud_code_endpoint https://daily-cloudcode-pa.googleapis.com`。

| 特征 | OAuth | CloudCode |
| --- | --- | --- |
| HTTP | HTTP/2.0 | HTTP/2.0 |
| ALPN | h2, http/1.1 | h2, http/1.1 |
| JA3 MD5 | 03117a8ed39ef02427ebbc39f121275c | 03117a8ed39ef02427ebbc39f121275c |
| User-Agent | Go-http-client/2.0 | antigravity/hub/2.31.0 (aidev_client; os_type=linux; arch=amd64; cl=1017595969) |

CloudCode 观察端点：`loadCodeAssist`、`fetchAvailableModels`、`fetchUserInfo`、`listExperiments`。OAuth 表单键仍为 `client_id/client_secret/grant_type/refresh_token`。HTTP/2 请求头迭代顺序随请求变化，不宣称固定顺序。

相对 2.28.2 的主要变化是官方 CloudCode 开始协商 HTTP/2。OAuth 的 JA3、ALPN、UA 没有变化。脱敏网络特征存于 `tests/fixtures/agy_transport_2.31.0.json`；旧版本 fixture 保留作历史记录。

本轮未驱动 agent 推理，不推断生成请求信封、工具参数增量或 `emit_answer` 行为发生变化。HTTP/1.1 的成功模型目录请求也不能代替推理流式测试。

## 复跑

1. 查询官方 `/releases`，按版本与 execution id 下载二进制。
2. 校验大小、SHA256、ELF 元数据，再通过 REA 执行 `--stamp`。
3. 使用 `.agy-re/mitm_capture.py` 起本地 MITM；`connection_strategy=lazy`、`upstream_cert=false`，隔离实例设置代理与 `SSL_CERT_FILE`。
4. REA 启动隔离实例，采集约 16 秒后 SIGTERM；单独解析 MITM JSONL 的 ClientHello 和脱敏请求头。
5. 使用有效凭证经真实代理单独验证 HTTP/1.1；仅输出状态码、协商版本、模型数量，不输出或落盘凭证与响应全文。
