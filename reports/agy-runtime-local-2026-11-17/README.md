# 账号运行记录与上传统计：本地验证版

2026-11-17。修改位于 `/tmp/gcli-hub-telemetry`，分支 `codex/hub-telemetry-schema`。本次未 push、未触发 Actions、未部署生产、未访问生产凭据或账号，未向 Google 发送请求。

已实现：

- `_runtime.binding` 将账号、主体摘要、安装标识、环境摘要、策略版本和模式绑定；相同的请求轨迹在不同账号中具有不同本地关联。
- 请求开始、首响应块、结束和终止状态来自真实观察；不记录提示词与回答正文。
- `_delivery.uploads` 记录每批实际编码字节数、尝试事件数、HTTP 状态、耗时和结果。批次 ID 在同批事件间共享，汇总去重；重试按新尝试计算。
- 预览、丢弃和编码失败不计为已发送；连接错误、取消与超时没有 HTTP 状态时使用 null，修复了继承上次状态的问题。
- `src/antigravity_telemetry_runtime.py:summarize` 可以对历史事件作本地汇总。统计仅覆盖提供的记录，不是全量服务计数器；HTTP 200 不证明服务端完成摄取。

查看 `events.json` 中的 events 与 statistics；`result.json` 是验证结果。所有账号、token、模型和 HTTP 响应均为合成测试数据，发送器在初始化后立即替换成本地函数。结果中的 sent、503、200 和成功事件数都来自该替身，没有提交上游。

本轮脚本生成三个账号、四条尝试：alpha 两条共享批次、beta 一条、preview 一条。第一批模拟 503，随后成功；三个发送事件最终收到模拟 200。preview 零上传。各账号的安装标识独立，输入 trajectory_id 故意相同。统计字节数与替身接收到的实际编码体长度逐批一致；导出不含合成 token、代理地址或提示词。

本轮不增加对外 327、2992、轨迹 ACL 或完整 Hub 轨迹事件。生成器编码仍使用原允许字段，新增本地诊断不进入对外包体。安装标识在本地关联，未塞进官方 schema 不存在的字段。真实 Hub 内部执行器/工具状态未复现。

相关测试 67 项通过（5 项原有 Pydantic 弃用警告）；覆盖批量去重、账号隔离、预览不发送、环境/策略分代、token 续期保持关联、HTTP 后连接异常、发送中取消、序列化失败及对外字段隔离。

复现：

```bash
/home/libre/project/gcli2api/.venv/bin/python \
 /tmp/gcli-hub-telemetry/scripts/validate_telemetry_runtime.py \
 --output /tmp/telemetry-local-check
```

临时 SQLite 与预览目录在脚本结束后删除。已有生产数据未修改；上轮指定账号的 Google 偏好保持当时状态，本轮未联网复查。
