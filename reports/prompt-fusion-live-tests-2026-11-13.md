# 2026-11-13 提示词融合生产验证

用户授权通过 dsh 调用生产网关，随后要求停止重复调用。停止要求之后仅检查已有生产证据、运行本地模拟测试及发布修复，没有再次发起真实模型生成请求。本文不含密钥。

## 已有调用结果

测试任务：通过工具读取 invoice.json，计算 19.5×3 + 8.25×2 − 5，保留客户名和 trace，最终仅输出 JSON。正确金额为 70。

| 调用模型 | 工具环境 | 融合 | 结果 |
| --- | --- | --- | --- |
| claude-sonnet-5-5-medium | 首次成功请求仅有 bash | 成功，命中后续缓存 | 两次均算出 70，但 JSON 前多出计算解释，严格输出格式未通过 |
| gemini-4-hydrogen-medium | 完整 MCP，250 个工具 | 超过旧 128 KiB 输入限额，跳过 | 返回 69，错误；首次 bash 带多余 justification 被 dsh 拒绝，随后自行纠正 |
| gemini-4-argon-medium | 完整 MCP，250 个工具 | 超过旧限额，跳过 | 返回 68.5，错误 |
| gemini-4-hydrogen-medium | 临时关闭无关 MCP | 成功并缓存 | 读取文件后只输出 JSON，金额 70 |
| gemini-4-argon-medium | 临时关闭无关 MCP | 成功并缓存 | 读取文件后只输出 JSON，金额 70 |

这些是小样本且工具环境发生变化，不能据此宣称融合必然提升模型能力，也不能将失败归因于某一个因素。生产记录实际上游版本为 Sonnet 的 claude-sonnet-5-5-medium、Hydrogen 的 gemini-4-hydrogen、Argon 的 gemini-4-argon-trial。

Sonnet 首次优化约 13.28 秒，原客户端指导 9426 字符、优化结果 7362 字符；重复任务只有两次 chat 记录，没有再次 prompt_optimization。两个 Gemini 的优化各约 9 秒，分别有一条优化记录、两条 chat 记录和独立缓存。融合成功后的最终任务 system 含 AGY 基础模块和 Client runtime guidance，未包含完整原 dsh system；优化请求本身包含原指导作为改写素材。

完整 MCP 样本的工具定义规范化后分别为 295842 / 297454 字节，另有约 9.4K 字符客户端指导和 2750 字节基础内容，故旧输入限制会回退原提示。原样发送的最终 system 与客户端 system 一致，没有融合段落。

## 已发布的调用修复

- f1a7603e565fdbe67a5dbe860e1ed37bc31dec54 保留 Claude 5.x 显式档位模型，修复 capture_stream 跨任务复用 ContextVar token 导致的流中断。
- [Actions](https://github.com/Wzh0718/gcl2api-plus/actions/runs/43354157273)：全量 1091 passed、6 skipped；本地全量 1097 passed。
- 生产镜像 sha256:9cf9149712bd0e0d89df4d8a660c3d065fe8d7dbe326d3f22a2dba31a61a015a，10:50:28（UTC+8）启动，running/healthy、0 重启、keepalive 200。
- 回滚元数据 /opt/gcli2api/releases/20261113-f1a7603，无数据库备份。

## 300 KiB 与压缩修复

按用户要求，将完整优化输入的上限提高到 300 KiB（307200 字节），输出上限独立保留 128 KiB。输入使用紧凑 JSON；超限时对重复工具说明建立字符串引用表，保持可无损还原，只改变优化器看到的表示，实际工具定义不变。若压缩后的输入仍超限，保留原请求并记录大小告警。

优化指令要求合并重复表述、不复制工具 schema，保留权限、审批、工具协议、路径和字面约束，在不丢失要求的前提下以原客户端指导 60% 以内为目标。此目标不是强制截断，也不是语义等价保证。规则版本更新使旧缓存自然失配，不删除账号数据。

本地全量 1101 passed。新增测试覆盖 UTF-8 的 300 KiB 精确边界、边界外拒绝、超过 128 KiB 的输入、重复工具说明的无损还原、实际工具与历史不变，以及独立的输出大小限制。全部使用模拟模型，没有真实生成调用。

300 KiB 修复已发布：构建提交 74d7002b7a5900d63d23793f6cd45a71d2525b48，[Actions 成功](https://github.com/Wzh0718/gcl2api-plus/actions/runs/43356125872)，全量 1095 passed、6 skipped。镜像 sha256:48cb41e4e72f1a76749b318966dbf3c3d790060055128a52708438eea4ed9295。生产 11:08:17（UTC+8）启动，running/healthy、keepalive 200；Compose 仅镜像变更，容器环境和挂载等配置保持一致，启动日志检查通过。只读验证确认 MAX_INPUT_BYTES=307200、MAX_OUTPUT_BYTES=131072、VERSION=2，生产提示词融合代码哈希与提交一致。没有发起真实模型复测。回滚元数据 /opt/gcli2api/releases/20261113-74d7002，无数据库备份。
