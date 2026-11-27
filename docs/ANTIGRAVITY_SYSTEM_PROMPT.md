# AGY system 注入与客户端指导

仅作用于 Antigravity 通道。AGY 基础提示注入**默认开启**；客户端 System 转 User **默认关闭**。面板“AGY 系统提示与客户端指导”可保存，下一请求生效；非空环境变量优先于 SQLite 并锁定面板控件。

> **强制约束（默认值与内容来源）**
>
> 1. `antigravity_system_prompt_enabled` 的代码默认值必须是 `true`。除显式保存 `false`（面板取消勾选并保存）或设置 `ANTIGRAVITY_SYSTEM_PROMPT_ENABLED=false` 外，不得把默认值改回关闭。
    旧版本面板写入的历史 `false` 在首次启动时由主库事务一次性纠正为 `true`，同一事务写入标记 `agy_base_prompt_default_migrated_v1`；标记存在后不再覆盖面板保存的 `false`。显式环境变量存在时不动存储。
> 2. 注入的文本必须是官方 AGY（Antigravity）基础模块，来源限定为 `src/prompts/antigravity-<hub 版本>.txt`，当前为 `2.31.2`。禁止用自定义前置文本、其他项目的提示词或模型生成的融合结果替换这段基础提示。
> 3. 版本跟随账号：选号后按该账号 hub 版本替换基线前缀；仓库没有对应版本文件时回退 `2.31.2`，不即时生成未核验内容。旧的 `ANTIGRAVITY_FULL_PROMPT_BASELINE` 环境变量不再替换基础模块。
> 4. “AGY 基础提示”“AGY 提示词融合”“自定义系统提示 / 前置文本”是三个独立功能，不能互相替代。

| 配置键 | 环境变量 | 默认 | 行为 |
| --- | --- | --- | --- |
| `antigravity_system_prompt_enabled` | `ANTIGRAVITY_SYSTEM_PROMPT_ENABLED` | `true` | 将内置 AGY 基础提示词作为 system 注入；客户端无 system 时也注入 |
| `antigravity_system_as_user_enabled` | `ANTIGRAVITY_SYSTEM_AS_USER_ENABLED` | `false` | 将转换后的客户端 system 各 part 放入会话开头的一条 user 指导消息，删除原 system |


两者同时开启时，system 是 AGY 基础提示词和网关环境适配说明；客户端 system 成为 user 层指导，随后是原对话。只注入时，AGY 基础提示词位于 system 前部，客户端 system 保留在后续 parts；只转换角色时不注入 AGY system。

注入文本最后附带独立的网关适配说明，明确仅使用当前请求中实际声明的工具，不假定 AGY 专用工具、路径、UI 存在，并兼容没有 USER_REQUEST 标签的普通用户消息。客户端 system 角色降级后，其优先级随之降低。模型理解工具约定不等于获得工具执行能力；执行仍由调用方工具协议负责。

## 官方来源与范围

2026-11-12 从官方更新接口获取的最新 Hub 为 **2.31.2**，execution ID `6470801290035200`。

- 版本接口：<https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases>
- 官方二进制：<https://storage.googleapis.com/antigravity-public/antigravity-hub/2.31.2-6470801290035200/unsigned/language_server-linux-x64>
- 二进制大小：206827920 字节。
- SHA256：`dd9737d3744d9f78e3a13be8ce0c7be8ca3f2cbcdaf702842305401dd31511d9`。
- 样本为 ELF x86_64 Go PIE；动态导入包括 libc、libresolv、libpthread、libm、libdl、librt、ld-linux。仅做静态模板提取，未启动官方客户端、未发起生成请求。

原始模块来自 Go embed 文件表内的 `google3/third_party/jetski/prompt/template_provider/templates/system_prompts/`，通过 ELF LOAD 地址映射和 RELATIVE 重定位恢复文件指针及精确长度。保存在 `src/prompts/antigravity-2.31.2/`：

| 模块 | SHA256 |
| --- | --- |
| identity.tmpl | bd8957e589c97f9fa5d5fe3077b01c49b0329f0987bd58cf73bab123ea0a199b |
| communication_style.tmpl | 96e2f45838771003bbfb05206623bb6be4b64187eaa97a4e4d1f2d9e9de86c8f |
| guidelines.tmpl | f4ede326caaa79f7454af6dac251cf0436ab8375c1b06eade975bcaf85a7c439 |

以 Go text/template 渲染：`generic-system-prompt=false`、`IsAutonomous=false`、`SupportsLatexRendering=false`；产物为 `src/prompts/antigravity-2.31.2.txt`，SHA256 `85a8a9070376c8b0a90c0c97b6001991e490a919351cf827e36a446ddacbdd9f`。`tests/test_antigravity_system_prompt.py::test_default_switches_on_official_agy_baseline` 锁定上述四个文件哈希；替换基线必须同时更新该用例与本文档。这三个固定基础模块**不是完整运行时 system**。完整内容还依赖模型、实验、会话、技能、工作区、工具和其他代码生成模块，未冒充或重建这些客户端状态。不在线自动拉取更新，升级时重新核查并随代码发布。

## 客户端身份归一化（默认开启）

面板键 `antigravity_client_identity_normalization_enabled`，环境变量
`ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION`。开启后，在敏感词过滤之后、注入写回之前，
对客户端 system 做三处改写：

| 原文 | 改写后 |
| --- | --- |
| `You are [an] [AI] agent powered by <品牌>.`（part 开头） | `You are a coding assistant.` |
| `operating inside/within <品牌>` | `operating inside the host application` |
| `# Runtime Context` / `# Tool Specifications`（同一 part 内） | `# Environment` / `# Tools` |

身份句只在 part 开头匹配，正文里引用同句式不改；标题只在命中身份句的 part 内改写。
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
