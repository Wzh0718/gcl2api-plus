# AGY system 注入与客户端指导

两个独立开关默认关闭，仅作用于 Antigravity 通道。面板“AGY 系统提示与客户端指导”可保存，下一请求生效；非空环境变量优先于 SQLite 并锁定面板控件。

| 配置键 | 环境变量 | 行为 |
| --- | --- | --- |
| `antigravity_system_prompt_enabled` | `ANTIGRAVITY_SYSTEM_PROMPT_ENABLED` | 将内置 AGY 基础提示词作为 system 注入；客户端无 system 时也注入 |
| `antigravity_system_as_user_enabled` | `ANTIGRAVITY_SYSTEM_AS_USER_ENABLED` | 将转换后的客户端 system 各 part 放入会话开头的一条 user 指导消息，删除原 system |

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

以 Go text/template 渲染：`generic-system-prompt=false`、`IsAutonomous=false`、`SupportsLatexRendering=false`；产物为 `src/prompts/antigravity-2.31.2.txt`。这三个固定基础模块**不是完整运行时 system**。完整内容还依赖模型、实验、会话、技能、工作区、工具和其他代码生成模块，未冒充或重建这些客户端状态。不在线自动拉取更新，升级时重新核查并随代码发布。

## 与其他配置的关系

- 在协议转换后执行；OpenAI Chat/Responses、Anthropic Messages、Gemini 的生成请求共享规范化入口，流式与非流式使用相同转换。
- 原有敏感词过滤先处理客户端 system，再执行本功能。角色转换后的指导内容因此继承现有过滤结果。
- 原有全局 `system_prompt_append` 在本功能后执行：仍前置到最终 system 第一个文本 part；不随客户端 system 转移。仅角色转换、最终无 system 时，旧前置功能仍不主动创建 system。
- 原有全局兼容模式可能在协议转换阶段已将 system 改为 user；本功能不会再将这些 user 消息挪动或重复包装。需要本功能的明确指导包装时，关闭全局兼容模式。
- 角色转换不改工具名称、参数和调用结果顺序；原有协议规范化仍执行。
- 这是提示词兼容选项，没有证据表明能解决 429 或账号停用。默认关闭，也未在生产上开启或部署。
