# NewAPI / opencodex 中转兼容排查

## 本次证据边界

用户反馈直连 NewAPI → gcli 有报错，增加 opencodex 后可正常使用；模型可能是 Gemini 4。按用户要求，本次未发送真实模型或生成探测请求，仅检查已有日志、源码，并用本地模拟上游验证。

现有服务器日志未定位到这次 Gemini 4 故障的确切请求及正文。历史日志包含模型 404、上游 502 等不同问题，不能仅凭增加中转层后恢复，就把根因归于 NewAPI。也不能保证本次两项修复就是该原始请求的根因。

源码对照固定在 opencodex 提交 `5f12824288d20d8624c63b8dc409fcf9b609b512`。其 [developer-role.ts](https://github.com/lidge-jun/opencodex/blob/5f12824288d20d8624c63b8dc409fcf9b609b512/src/adapters/openai-chat/developer-role.ts) 在转换路径中将未特别配置的 developer 指导映射到 system；还有工具、消息、响应等独立适配，因此增加它可能改变多个协议细节。

## 已复现并修复的兼容缺口

### developer 指导泄漏成 Gemini 消息角色

OpenAI Chat Completions 请求可能包含：

```json
{"messages":[{"role":"developer","content":"Use the supplied tool schemas."},{"role":"user","content":"Task"}]}
```

修复前只识别 system，转换结果中出现 `contents[].role="developer"`，不符合 Gemini 的 user/model 会话角色。

现在 developer 与现有 system 指导走同一转换流程：连续的开头指导合并到 systemInstruction；启用旧兼容模式时转换为 user；对话中后置指导延用现有 system 的 user 转换行为。指导文本保留，输入对象不被原地修改；兼容模式下的敏感词过滤也覆盖 developer，避免角色转换绕过既有过滤策略。

这是 OpenAI 入口的兼容修复，不改变直接提交 Gemini 原生 contents 的消息角色。与 AGY system 注入选项配合时，仍在统一协议转换之后执行注入和角色处理。

### 独立 usage 尾块被丢弃

对于已结束候选、随后单独返回用量的 Gemini SSE：

```text
data: {"candidates":[{"content":{"role":"model","parts":[{"text":"ok"}]},"finishReason":"STOP"}]}

data: {"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":1,"totalTokenCount":3}}
```

修复前第二块没有 finish_reason，usage 被省略。现在转换为 OpenAI 标准的 `choices: []` 加 `usage` 尾块，供中转网关和客户端读取统计；支持直接 Gemini 对象及 response 信封两种形态。原有有候选的中间块处理、token 分类与计费计算方式不变。

## 验证

全量离线回归 **1009 项通过**（6 条现有依赖警告），Python 编译及差异检查通过。

新增 `tests/test_gateway_openai_compat.py`，覆盖指导角色、旧兼容模式、过滤策略、独立 usage 尾块和 GeminiCLI / Antigravity 的流式及非流式路由。路由测试使用模拟上游，真实模型传输若被调用即失败。

不增加配置项；这两项作为标准协议兼容修复生效。未部署、未修改生产配置。由于禁止额外真实请求，尚未实测原 NewAPI → gcli 故障链路；需要后续自然发生的原始错误正文和请求特征才能继续归因。
