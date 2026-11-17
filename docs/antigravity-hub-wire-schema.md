# Hub 2.31.2 生成遥测线格式

协议依据为本地 Hub 2.31.2 描述符及 2026-11-16 已捕获的 source 2747 请求。独立描述符摘录位于 `tests/fixtures/hub_2_31_2_generation_schema.json`，包含字段编号、类型与原始消息名，不含账号或认证数据。此处只实现已确认的生成事件子集。

| 消息 | 字段编号和含义 |
| --- | --- |
| LogRequest | 1 ClientInfo、2 log_source=2747、3 repeated LogEvent、4 request_time_ms |
| ClientInfo | 1 client_type=16 |
| LogEvent | 1 event_time_ms、6 source_extension（bytes，内容是 JetskiTelemetryExtension） |
| JetskiTelemetryExtension | 3 cortex_generator_metadata、16 ide_name、17 ide_version、18 os、19 arch、24 is_redacted、27 product_name |
| CortexGeneratorMetadata | 1 trajectory_id、2 cascade_id、3 generator_metadata、12 plan_model |
| CortexStepGeneratorMetadata | 1 chat_model、2 packed step_indices |
| ChatModelMetadata | 4 usage、19 response_model |
| ModelUsageStats | 2 input_tokens、3 output_tokens、5 cache_read_tokens、9 thinking_output_tokens、10 response_output_tokens、11 response_id |

环境版本、系统及架构从当前账号绑定 UA 提取；轨迹、步骤及请求模型来自实际请求。返回模型和响应 ID 来自实际上游响应。Gemini candidatesTokenCount 映射 response_output_tokens，thoughtsTokenCount 映射 thinking_output_tokens，两者相加得到 output_tokens。totalTokenCount 不冒充 input/output 任一计数。

没有可靠值的字段省略：模型枚举、Hub 执行 ID、planner 配置、完整步骤状态、官方阶段计时等。不能用整个模型耗时冒充 SEND_USER_CASCADE_MESSAGE_LATENCY。网关安装 ID、会话 ID、请求 ID、HTTP 结果和投递信息仅保留本地；不会自动展开任意字典到网络字段中。

编码器以显式字段表限定序列化范围，未知键直接报错。LogEvent 的 source_extension 在原描述符中是 bytes；独立测试按 source 2747 解为 JetskiTelemetryExtension，不修改原描述符来迎合编码器。

## 可复现验证

在仓库根执行：

```bash
python -m pytest tests/test_antigravity_hub_schema.py tests/test_antigravity_telemetry.py -q
```

测试使用恢复的描述符独立解码实际编码字节，核对字段编号、类型、批量信封与面板预览投影；同时加入 implementation、自定义事件名、正文和伪造 _wire 字段，验证这些不会进入网络载荷。合成用量 input=21、response=3、thinking=4、cache=2 的解码结果应为 input_tokens=21、output_tokens=7、response_output_tokens=3、thinking_output_tokens=4、cache_read_tokens=2。

本地 `_wire` 仅用于展示 LogEvent 的语义字段；发送时重新从白名单事实构造。外层 LogRequest 还包含上述来源、客户端类型与发送时间。旧历史如实保留，不声称旧数据按新格式发送。协议形状匹配不代表 Google 已接纳、入库或采用事件。
