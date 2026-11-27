# AGY system 注入与客户端指导

仅作用于 Antigravity 通道。AGY 基础提示注入**默认开启**；客户端 System 转 User **默认关闭**。面板“AGY 系统提示与客户端指导”可保存，下一请求生效；非空环境变量优先于 SQLite 并锁定面板控件。

> **强制约束（默认值与内容来源）**
>
> 1. `antigravity_system_prompt_enabled` 的代码默认值必须是 `true`。除显式保存 `false`（面板取消勾选并保存）或设置 `ANTIGRAVITY_SYSTEM_PROMPT_ENABLED=false` 外，不得把默认值改回关闭。
    旧版本面板写入的历史 `false` 在首次启动时由主库事务一次性纠正为 `true`，同一事务写入标记 `agy_base_prompt_default_migrated_v1`；标记存在后不再覆盖面板保存的 `false`。显式环境变量存在时不动存储。
> 2. 注入的文本必须是官方 AGY（Antigravity）系统提示，来源限定为 `src/prompts/antigravity-<hub 版本>.txt`，当前为 `2.35.2`。该文件是**整段**官方 system（去掉运行时 `<user_information>` 块），运行时只做整段替换，不做分段提取或拼接。禁止用自定义前置文本、其他项目的提示词或模型生成的融合结果替换这段基础提示。
> 3. 版本跟随账号：选号后按该账号 hub 版本替换基线前缀；仓库没有对应版本文件时回退 `2.35.2`，不即时生成未核验内容。旧的 `ANTIGRAVITY_FULL_PROMPT_BASELINE` 环境变量不再替换基础模块。
> 4. “AGY 基础提示”“AGY 提示词融合”“自定义系统提示 / 前置文本”是三个独立功能，不能互相替代。

| 配置键 | 环境变量 | 默认 | 行为 |
| --- | --- | --- | --- |
| `antigravity_system_prompt_enabled` | `ANTIGRAVITY_SYSTEM_PROMPT_ENABLED` | `true` | 将内置 AGY 基础提示词作为 system 注入；客户端无 system 时也注入 |
| `antigravity_system_as_user_enabled` | `ANTIGRAVITY_SYSTEM_AS_USER_ENABLED` | `false` | 将转换后的客户端 system 各 part 放入会话开头的一条 user 指导消息，删除原 system |


两者同时开启时，system 是 AGY 基础提示词和网关环境适配说明；客户端 system 成为 user 层指导，随后是原对话。只注入时，AGY 基础提示词位于 system 前部，客户端 system 保留在后续 parts；只转换角色时不注入 AGY system。

注入文本最后附带独立的网关适配说明，明确仅使用当前请求中实际声明的工具，不假定 AGY 专用工具、路径、UI 存在，并兼容没有 USER_REQUEST 标签的普通用户消息。客户端 system 角色降级后，其优先级随之降低。模型理解工具约定不等于获得工具执行能力；执行仍由调用方工具协议负责。

## 官方来源与范围

2026-11-26 官方更新接口的最新 Hub 为 **2.35.2**，execution ID `4585983447007232`。

- 版本接口：<https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases>
- 官方二进制：<https://storage.googleapis.com/antigravity-public/antigravity-hub/2.35.2-4585983447007232/unsigned/language_server-linux-x64>
- 二进制大小：230003400 字节；运行时自报 CL `1029311855`。
- 基线文件 `src/prompts/antigravity-2.35.2.txt`：12149 字节，SHA256
  `7ead810a96593a0394485665cb7c3851f8b704645679c45d3cc77b7447d0c528`。

取法（离线复现，未使用真实账号、未向 Google 发生成请求）：把 LS 的
`--cloud_code_endpoint` 指向本地 mock，用隔离 HOME 的扁平 oauth2 token 文件供凭证，
再经本地 Connect-RPC `StartCascade` / `SendUserCascadeMessage` 驱动一次真实组装。
`--subclient_type hub` 时 systemInstruction 为 12788 字节，比 standalone 多一段
`<terminal_sandbox>`；去掉运行时 `<user_information>` 后即为基线（12149 字节）。
产物与复现脚本见 `reports/agy-full-prompt-tools-2026-11-26/`。

基线包含的固定段：`<identity>`、`<terminal_sandbox>`、`<artifacts>`、`<guidelines>`、
`<work_efficiently>`、`<communication_style>`、`<slash_commands>`、`<messaging>`。
`<user_information>`（workspaces、cwd、AppDataDir、conversation id）属于每次会话的运行时数据，
不进仓库；`<skills>` 段依赖真实 AppDataDir 的 builtin skills，也不进基线。
实验开关（`generic-system-prompt`、`invoke-subagent-config` 等）会改变官方渲染结果，
换版本时必须重新按同一方法抓取，不得用模板拼装补段。

历史基线：`src/prompts/antigravity-2.31.2/`（identity/communication_style/guidelines 三个模块）
与 `antigravity-2.31.2.txt` 保留，仅作回退与对照；2.35.2 起不再按模块渲染。

## 网关引导词（官方块在前、用户 system 说了算）

顺序固定为：**官方 2.35.2 基线 → 收口语 → 客户端 system**。官方块保持在最前，是为了保住
指纹形态；但它对行为没有裁决权。

收口语（`BRIDGE`，`src/converter/antigravity_system_prompt.py`）以
`=== SESSION MODE: CALLER-MANAGED ===` 开头，语气是“会话模式切换”而不是对抗式命令：
官方块保留为兼容参考、在本会话中只作背景；由调用方 system 定义角色、语言、范围、输出格式与
工具权限；宿主的人格、工具、artifacts、sandbox 设置不适用；未声明工具时只输出文本。
不使用 ignore/override/disregard 一类措辞，避免被安全对齐模型误判为提示词注入。

`BRIDGE_REQUIREMENTS` 定义 8 条检查点，`validate_bridge()` 返回缺失项；
`tests/test_antigravity_system_prompt.py::test_bridge_covers_every_host_adaptation_requirement`
在 CI 中锁定。

### 有效性证据（2026-11-27，12 道冲突探针）

三个臂：U=只有客户端 system；O=官方基线+客户端 system；F=官方基线+收口语+客户端 system。

| 臂 | 通过率 |
| --- | --- |
| U | 12/12 |
| O | 11/12 |
| F（当前实现，caller-managed 表述） | **12/12** |

对比旧版引导词（把客户端 system 称作 "client guidance about the host application"、无收口语）：
F 只有 8/12，还会在未声明工具时凭空返回 `functionCall: run_command`。新版与“只有客户端 system”
逐题一致。实验与原始结果见 `reports/agy-bridge-dominance-2026-11-27/`。

### 已知边界

业务打分对 system 前缀本身敏感：同一请求加一句 “You are a helpful assistant.” 也会让
个别样本（如 29935784）的维度分从 70 掉到 20。这不是官方块独有的问题，换措辞压不住；
这类样本需要业务侧单独回归，不能当作提示词回归失败。

## 日志详情审计（真实请求抽样）

`scripts/audit_bridge_records.py` 从 `call_records.request_details.upstream`
（内容捕获开启时写入的 `{system, messages, tools, parameters, response}`）里抽样，
只读数据库、不发上游请求：

```bash
.venv/bin/python scripts/audit_bridge_records.py --limit 200 --strict
.venv/bin/python scripts/audit_bridge_records.py --replay     # 用当前管线重放历史记录
```

判据分两层。注入层（有 system 即可判）：基线版本、引导词存在且唯一、位于末尾
（允许结构化输出优先级声明作为唯一尾随段）、运行时 `<user_information>` 未泄漏。
依从层（有 response 才判）：是否调用未声明工具（硬失败）、是否声称执行过动作或
编造请求里不存在的绝对路径（提示，需人工看上下文）。

2026-11-27 对本地库的抽样结果：`call_records` 共 576 条，带详情 6 条；其中 1 条
（id 212，gemini-2.5-flash-lite 结构化输出请求）带 system，其余为图片请求（上游
信封不带 system）。其余 5 条为图片请求，审计按 `system_not_expected` 处理（ok=5）。该记录是改动前的
流量，审计结论 `baseline=<=2.31.x`、`bridge_legacy`；`--replay` 用当前管线重新注入后
为 `baseline=2.35.x`、单条新引导词、问题列表为空，`--strict` 通过。要扩大样本，先在面板开启内容捕获，让新流量落详情，再按同一命令抽检。

## 工具声明形态对齐（默认关闭）

面板“AGY 系统提示与客户端指导 → 更多设置”里的**官方工具声明形态**开关
（键 `antigravity_official_tool_alignment_enabled`，环境变量
`ANTIGRAVITY_OFFICIAL_TOOL_ALIGNMENT`）开启后，`wrap_cli_request` 会把客户端的
`tools` 按来源工具集（AGY / DSH / Claude Code / GPT(Codex)，按名字交集自动识别）的规范顺序重排、合并成单个 `functionDeclarations` entry，并把
schema 的 `type` 改成大写枚举、数值约束改成字符串（`"minLength": "1"`）。
只改形态：不重命名工具、不改参数名和枚举值，因此客户端调用与结果映射不受影响。
关闭时请求逐字节不变。规则、各家顺序表与识别逻辑见 `src/converter/tool_alignment.py`，
用例见 `tests/test_tool_alignment.py`。

## 客户端身份归一化（默认开启）

面板键 `antigravity_client_identity_normalization_enabled`，环境变量
`ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION`。开启后，在敏感词过滤之后、注入写回之前，
仅改写客户端 system 的开场身份区：

| 原文 | 改写后 |
| --- | --- |
| `You are [an] [AI] agent powered by <品牌>.`（part 开头） | `You are a coding assistant.` |
| 紧随开场句的 `You are a coding assistant operating inside/within <品牌>.` | 宿主名称改为 `the host application` |
| 标题、代码块、输出模板和身份区之后的正文 | 原样保留 |

身份句只在 part 开头匹配，宿主身份句仅匹配紧随其后的第一条非空行；正文里的同句式、所有标题和代码块均不改写。
业务正文、工具说明、路径和输出约定逐字节保留；未命中时返回原对象，请求完全不变。
实现见 `src/converter/client_identity.py`，用例见 `tests/test_client_identity.py`。

内置词库同时补了 `DeepSeek Harness` / `DSH Agent` / `DSH`（统一替换为 `Antigravity`），
两层叠加：词库先换品牌，归一化再把句式收成中性句。

### 实测依据（2026-11-27，free 号池）

同一把 key、同一账号、`gemini-2.5-flash-lite`，每个臂只改一个维度：

| 变量 | 结果 |
| --- | --- |
| codex / Claude Code / ChatGPT / Gemini CLI / 中性 persona | 200 |
| `You are an AI agent powered by DeepSeek Harness.` | **429 `RESOURCE_EXHAUSTED`** |
| 同句品牌换成 Antigravity / OpenAI / Google / DSH | 200 |
| 单独一行 `DeepSeek Harness`，或 `You are DeepSeek, …` | 200 |
| `# Runtime Context`、`# Tool Specifications` 标题 | 200 |
| 16 个非官方 DSH 工具名 | 200 |

即判据是 `[you are …] powered by … DeepSeek Harness` 这条身份声明本身（大小写不敏感），
与工具集、markdown 标题无关。加规则或开启本功能后，同一批请求全部转为 200。
复现与消融脚本见 `scripts/diagnose_client_fingerprint.py`，报告见
`reports/dsh-429-identity-2026-11-27.md`。

## 与其他配置的关系

- 在协议转换后执行；OpenAI Chat/Responses、Anthropic Messages、Gemini 的生成请求共享规范化入口，流式与非流式使用相同转换。
- 原有敏感词过滤先处理客户端 system，再执行本功能。角色转换后的指导内容因此继承现有过滤结果。
- 原有全局 `system_prompt_append` 在本功能后执行：仅当原始客户端 system 含非空文本时，前置到最终 system 第一个文本 part；不随客户端 system 转移。仅角色转换、最终无 system 时，旧前置功能仍不主动创建 system。
- 原有全局兼容模式可能在协议转换阶段已将 system 改为 user；本功能不会再将这些 user 消息挪动或重复包装。需要本功能的明确指导包装时，关闭全局兼容模式。
- 角色转换不改工具名称、参数和调用结果顺序；原有协议规范化仍执行。
- 基础提示注入默认开启（用户方要求）；它是提示词兼容选项，不构成对账号停用的解决方案，也不能替代出口、负载和凭证层面的治理。
- 与其他提示词选项不同，身份归一化对「第三方 harness 身份句触发的 429」是已实测的确定性修复（见上节）；对配额型、并发型 429 无效。
