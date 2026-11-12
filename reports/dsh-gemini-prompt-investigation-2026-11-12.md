# DSH / Gemini 提示词接入核查

时间：2026-11-12，UTC+8。按用户授权通过所给 API Key 发起 DSH 测试并只读检查 43.110.150.44；未修改生产配置、部署或重启。密钥未写入本报告。

## 实测

1. 初次误选 `/antigravity/v1/models` 列出的 `gemini-4-argon-disabled`，11:40:16 返回上游 404 NOT_FOUND，traceId `8d8462be4e5eefc3`。此结果不能用于判断提示词兼容性。
2. 用户确认实际可调用 ID 后，使用 `gemini-4-argon-low` 重跑 DSH headless：11:42:12 发出请求，服务端 11:42:13 记录模型映射为原名，11:42:20 访问日志返回 HTTP 200。DSH 退出码 0，返回“连接测试成功。”，usage input=12518、output=8。
3. 成功请求保留 DSH 默认 system（2571 字符）及 25 个工具定义，共四条消息。另含运行上下文和用户级可用技能目录。测试指令禁止调用工具，所以只证明文本响应成功，未验证工具执行或长对话。
4. DSH 在临时工作目录运行，使用独立 DSH_HOME，未加载用户 home 级 MCP patch，未加载本仓库 AGENTS.md。通过本机回环 HTTP 代理捕获请求体并转发到指定服务；捕获文件不含 Authorization 头。未复现用户原会话的 429。

## 线上提示词配置快照

- SQLite `system_prompt_append` 为空，环境中无 SYSTEM_PROMPT_APPEND：当前没有前置文本。
- 自定义 sensitive_word_replace_rules 为空。
- SQLite sensitive_word_replace_enabled=false，但环境 SENSITIVE_WORD_REPLACE_ENABLED=true 优先，因此实际开关启用。
- 环境 SENSITIVE_WORD_FILTER_MODE=rewrite，SENSITIVE_WORD_USE_DEFAULT_KEYWORDS=true。
- compatibility_mode_enabled=false，环境无覆盖。
- call_content_capture_enabled=false，因此历史日志不保留完整请求体，无法从此次普通日志还原用户之前失败请求的完整提示词。

## 提示词发现

成功请求 system 首句是 `You are an AI agent powered by DeepSeek Harness.`，随后是当前模型说明、文件读写约束、工具选用规则、后台任务管理、外部数据处理、goal 和委派规则。

仓库默认名称过滤词库不含 DeepSeek Harness，因此仅启用默认名称替换，不会处理该身份句。但此次携带该身份句仍成功，不能认定这个名称必然触发 429。

前置函数目前直接执行 `text + original_text`，不会自动加分隔换行。若后续配置前置文本，应明确以两个换行结尾，避免与原首句粘连。

## 建议方案（待在原失败请求上验证）

首选 DSH 客户端局部配置，而非全局替换系统提示词：保留全部工具和行为规则，仅关闭固定 harness 身份 opener，设置中性的 personaPrefix。DSH 自带这两个配置项，无需重写整个 system。

```yaml
- id: system-prompt
  config:
    includeHarnessIdentity: false
    personaPrefix: >-
      You are a coding assistant. Follow the task instructions and the
      execution environment's workflow and tool contracts below. Use only
      the tools provided in this session and preserve their names and
      argument schemas. Respect file access and approval requirements.
      Verify results and report them accurately.
```

以上是候选兼容配置，未应用、未证明能修复 429；只关闭固定身份句并更换 persona，不删除后续由插件贡献的规则。已有 personaPrefix 中如包含用户自定义行为要求，应先合并保留，不能直接覆盖。

若必须从网关配置前置文本，可用：

```text
You are a coding assistant operating within the execution environment described below. Follow its task workflow, tool definitions, file access requirements, and approval rules. Use only the tools exposed in this session and keep their names and argument schemas unchanged. Verify results before reporting completion.

```

末尾保留两个换行。前置文本本身不会删除后面的身份句，不能宣称仅靠此前置就能解决原故障。不建议伪造官方客户端身份，或把所有 system 降级为 user。

验收应使用原失败请求：固定模型和请求其余字段，比较原版、仅身份段调整版；记录错误正文和请求标识。若两版均成功，保留默认配置，不把偶发恢复归功于改写；若仅改写版成功，再重复对照，并验证至少一次真实工具调用及结果回传。
