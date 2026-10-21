# agy 2.25.0 hook 实测：封控更新判断 × gcli2api 请求逻辑对照

> **2026-10-21 更新：P0/P1 已落地**（测试 545 全过）：
> - UA 切 hub 轨迹：`antigravity_hub_version=2.25.0` / `antigravity_hub_cl=1003475615`
>   **面板「基础配置」页可直接修改（热更新，优先级：面板 > env > 默认）**；CLI 模板保留给 `antigravity_cli_version` 探测专用；
> - labels 9 键全集 + `model_enum` 用枚举名（新模块 `src/antigravity_model_catalog.py`，fetchAvailableModels 响应热更新）；label 步进从 0、信封步进从 1（与实抓一致）；
> - sessionId 改账号级稳定负 int64（`credential_key` 派生，跨会话不变、跨账号不同）；
> - 摘除 `enabledCreditTypes`、强制 `toolConfig`；generationConfig 按目录补默认值；
> - CLI 版本追踪服务默认停用（`ANTIGRAVITY_CLI_VERSION_TRACKING=true` 可开），消除其周期性账号探测流量；
> - 逐项验证：改造后信封与 2.25.0 实抓逐字段一致（UA/顶层键/labels/model_enum/genConfig/步进全同）。
> - 未做（P2）：quotaInfo 主动轮换、健康检查序列仿真。

> 2026-10-21。方法：hub 更新通道拉最新 language_server 2.25.0（CL 1003475615，**今天构建**），
> sidecar 隔离 HOME 注入凭证池账号，forward_capture 转发代理抓真实出网请求，
> 与 10-14 v2.23.1 捕获（`antigravity-re/agent_request_full.json`）及 gcli2api 当前代码逐项对照。
> 原始捕获为本地工件（`.agy-re/`，已 gitignore，含账号请求数据不入库），复跑见 §5。

## 1. 版本事实（三条更新通道实测）

| 通道 | 当前最新 | 本仓库基线 | 端点 |
|---|---|---|---|
| hub（language_server，出网主体） | **2.25.0**（CL 1003475615，2026-10-21 构建） | 2.23.1（CL 998881661，10-14） | `antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases` |
| IDE 壳 | 2.5.5（stable） | — | `antigravity-ide-auto-updater-...run.app/api/update/linux-x64/stable/latest` |
| CLI | 1.4.3 | 动态跟踪中（探测未过，停留 1.4.2） | `antigravity-cli-auto-updater-.../manifests/linux_amd64.json` |

LS 独立下载：`https://storage.googleapis.com/antigravity-public/antigravity-hub/<ver>-<execution_id>/unsigned/language_server-linux-x64`。

## 2. 2.23.1 → 2.25.0 实测变化（hook 证据）

**没变的（信封核心）：**
- 顶层字段：`model / project / requestId / requestType:"agent" / userAgent:"antigravity" / request` —— 完全一致；
- requestId 格式：`agent/<cascadeId>/<unix_ms>/<trajectoryId>/<stepIndex>`；
- labels 9 键全集：`cascade_id, root_cascade_id, request_id, trajectory_id, last_step_index, model_enum(枚举名), used_claude, used_claude_conservative, used_non_gemini_model`；
- generationConfig：`{"maxOutputTokens":65536,"thinkingConfig":{"includeThoughts":true,"thinkingBudget":-1}}`；
- **sessionId = `-3750763034362895579`，跨版本、跨账号、跨会话完全一致**（安装/机器派生的稳定负 int64，不是随机会话值）；
- checkpoint 调用：`requestType:"checkpoint"`、model `gemini-3.5-flash-lite`、`thinkingLevel:"LOW"`、同一 sessionId。

**变了的：**
1. **UA**：`antigravity/hub/2.23.1 (…cl=998881661)` → `antigravity/hub/2.25.0 (…cl=1003475615)`（2.24.0 = cl=1000335682）。
2. **Agent 工具集 13 → 12**：`view_file→read_file`、`write_to_file→write_file`、`replace_file_content→edit_file`，**`read_url_content` 被移除**。
3. Agent 系统提示词更新（20207 → 20180 字符）。
4. LS 侧新增显式 `SetLocation / SetUserTier / SetProjectID` 状态（auth_provider 日志）——官方在为**按地区/套餐执行策略**铺路（与社区 2.24.0 "HTTP 400 location error" 反馈吻合）。
5. 免费账号 `loadCodeAssist` 返回的 project 已收敛为 **`aicode-consumers`**（10-14 pro 账号同）——Google 服务端项目归并，号池 project 多样性假设失效。

**副产物：dev 凭证 `ag_aicode-consumers` 已 TOS 封号**（`TOS_VIOLATION` + 申诉表单链接）——封号链路仍在活跃执行。

## 3. gcli2api 当前逻辑 × 真实 2.25.0 差距清单

| # | 项 | gcli2api 现状 | 真实 2.25.0 | 判断 |
|---|---|---|---|---|
| 1 | UA 头 | `antigravity/cli/1.4.x (aidev_client; …; auth_method=consumer)` | `antigravity/hub/2.25.0 (aidev_client; os_type=linux; arch=amd64; cl=1003475615)` | **最高优先**：产品线/版本/CL 全不一致，一眼可辨 |
| 2 | labels.model_enum | 填模型 ID（`gemini-3.8-flash-high`） | 填枚举名（`MODEL_PLACEHOLDER_M318`） | **高**：服务端可校验，错格式=非官方客户端 |
| 3 | labels 集合 | 5 键（缺 `cascade_id / root_cascade_id / request_id / used_non_gemini_model`） | 9 键齐全 | **高**：指纹不完整 |
| 4 | sessionId | 每会话随机正数（`uuid.int % 9e18`） | 安装级稳定**负数** | **中高**：分布异常可聚类 |
| 5 | enabledCreditTypes | 非图片请求恒发 `["GOOGLE_ONE_AI"]` | hub 轨迹**不发** | 中：多余字段 |
| 6 | toolConfig | 强制塞 `functionCallingConfig.mode=VALIDATED` | agent 请求**不带 toolConfig** | 中：多余字段 |
| 7 | generationConfig | 透传客户端 | agent 请求恒定 `65536 + includeThoughts + thinkingBudget:-1` | 中：取决于客户端，可补默认值 |
| 8 | requestId 格式 | `agent/{conv}/{ms}/{traj}/{step}` ✓ | 同 | OK |
| 9 | loadCodeAssist body | `{"metadata":{"ideType":"ANTIGRAVITY"}}` ✓ | 同 | OK |
| 10 | requestType/userAgent 字段 | `agent` / `antigravity` ✓ | 同 | OK |
| 11 | 启动/健康行为 | 探测只打 loadCodeAssist(+fetchAvailableModels) | 真实握手：loadCodeAssist → fetchUserInfo / fetchAvailableModels / listExperiments(60s 轮询) | 低：探测行为可更像真人 |
| 12 | quota 利用 | 不用 | fetchAvailableModels 每模型带 `quotaInfo.remainingFraction/resetTime` | **机会点**：可主动轮换，避免打到 429 |

## 4. 优化建议（按封控收益排序）

**P0 — 指纹对齐（直接消除"非官方客户端"信号）：**
1. UA 切 hub 轨迹：`antigravity/hub/2.25.0 (aidev_client; os_type=linux; arch=amd64; cl=1003475615)`；CL 与版本做成 env 可配 + hub `/releases` 通道自动跟踪（替代/并存现有 cli manifest 跟踪）。
2. labels 补全 9 键；`model_enum` 用 fetchAvailableModels 下发的 `ModelInfo.model` 枚举名（按账号缓存 modelId→enum 映射，目录里 23 个模型全有）。
3. sessionId 改为**账号级稳定负 int64**（如对 credential 文件名做带符号 64 位 hash），同账号跨请求恒定。
4. 摘掉 `enabledCreditTypes` 与强制 `toolConfig`（对齐 hub 轨迹）。

**P1 — 行为对齐：**
5. agent 请求 generationConfig 缺省补齐 `maxOutputTokens:65536 + thinkingConfig{includeThoughts:true, thinkingBudget:-1}`（按模型目录的 thinkingBudget 下发值）。
6. 健康检查序列仿真实握手（loadCodeAssist → fetchUserInfo），降频并错峰。

**P2 — 主动配额（减少 429 风暴 → 降低风控触发面）：**
7. 解析 fetchAvailableModels 的 `quotaInfo.remainingFraction/resetTime`，按模型维度过载保护：剩余分数低于阈值主动换号，不再等 429 QUOTA_EXHAUSTED。

**注意**：工具集/系统提示词变化只影响真实 agent 客户端场景，网关透传模式下不影响我方（客户端自带 tools）；但若做 native 协议仿真需更新工具名单。

## 5. 复跑方法

```bash
# 1. 查最新 LS 版本
curl -s $PROXY https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases
# 2. 下载 + stamp
curl -s $PROXY -o ls-new "https://storage.googleapis.com/antigravity-public/antigravity-hub/<ver>-<eid>/unsigned/language_server-linux-x64"
./ls-new --stamp   # 拿 CL
# 3. 隔离 HOME 注入凭证（credentials.db → jetski-standalone-oauth-token），forward_capture.py 起代理
# 4. 启动: --standalone --override_ide_name antigravity --subclient_type hub --override_ide_version <ver> \
#    --https_server_port <p> --csrf_token <uuid> --app_data_dir antigravity-ide \
#    --cloud_code_endpoint http://127.0.0.1:<fwd> --enable_sidecars
# 5. 驱动: StartCascade(source=CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT) → SendUserCascadeMessage(planModel=枚举名)
# 6. 用完 rm -rf 隔离 HOME（含活 token）
```
