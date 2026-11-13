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

## 11:16—11:18 用户再次授权的完整工具复测

用户重新要求测试当前效果后，分别通过 dsh 对 Hydrogen/Argon medium 各执行一个任务（每个任务自然包含读文件和最终回答两个模型步骤），不额外重跑。两者的客户端请求均含完整 250 个工具，工具 JSON 为 563501 字节，未关闭 MCP。

- 两者均完成 read 工具调用，金额 70，最终为严格 JSON，客户名和 trace 正确；dsh 均退出 0。
- 生产记录确认实际模型分别为 gemini-4-hydrogen / gemini-4-argon-trial。
- 两者均成功融合：最终 system 以 AGY 基础内容开头，包含 Client runtime guidance，未包含完整原 dsh system。新规则 VERSION=2、输入限额 307200 字节，缓存均已写入。
- 第一模型步骤：Hydrogen 客户端指导 9426 → 5486 字符（减少约 42%），Argon 9423 → 4480 字符（减少约 52%）。这是客户端指导的字符数变化，不代表整条请求或 Tokens 同比减少；完整工具定义仍单独发送。
- 首次优化耗时 Hydrogen 24.50 秒、Argon 18.40 秒。dsh 整个任务约 95.14 / 104.91 秒，包含客户端启动、两步主请求和额外优化，不能将全程耗时视为纯生成时间。
- 缓存限制仍存在：第二模型步骤各再次产生一次优化，合计 4 条 prompt_optimization、4 条 chat。两步原系统指导、基础模块、gateway policy、tool_config、工具字符串表摘要相同，优化输入的工具部分仍有变化，导致缓存键不同。现有原文证据受采集截断，未定位变化的具体工具字段；不应声称两步已复用缓存。
- 身份压缩尚不彻底：Argon 第一份压缩指导仍有一句 “You are a coding agent powered by DeepSeek Harness ...”，即使 system 开头已是 AGY，内部仍残留客户端身份描述。不能把此次功能通过解读为提示词融合质量已完全达标。

以上为小样本功能检查，没有在相同工具环境下新增关闭融合的对照组，不能据此证明模型能力提升。

## 11:20—11:22 Claude 5.5 同步复测

按用户要求，使用同一 dsh 配置和任务，对 claude-opus-5-5-medium、claude-sonnet-5-5-medium 各执行一个任务，没有额外重跑。

| 模型 | 客户端指导 → 融合指导（字符） | 结果 | 首次优化 / dsh 全程 |
| --- | --- | --- | --- |
| Opus 5.5 medium | 9424 → 3229（减少约 66%） | 金额 70、保留客户名/trace、最终严格 JSON | 20.42 / 61.32 秒 |
| Sonnet 5.5 medium | 9426 → 3746（减少约 60%） | 金额 70，但 JSON 前附加解释，格式未通过 | 9.18 / 47.75 秒 |

两者的实际上游版本与请求模型一致；最终 system 均以 AGY 基础内容开头，融合指导仍描述 DeepSeek Harness 宿主环境。各有 1 条 prompt_optimization 和 2 条 chat；第二步与第一步最终指导及缓存键完全相同，未再次优化，证明本任务内缓存复用成功。以上“压缩”仅指客户端指导字符数，不是总输入 Tokens 降幅。

发现必须单列的缺陷：客户端发来 250 个工具（563501 字节），但最终上游工具列表都只剩 bash。源代码 `_ensure_empty_tool_schema_for_claude` 在处理聚合 functionDeclarations 时仅提取 declarations[0]，丢弃其它声明。这不是模型自行忘记工具，也不是压缩器主动删除工具；优化器看到的工具输入此时已只剩 bash，因此压缩产物写“only bash”。这次仅验证简单 bash 读文件任务可运行，完整 250 工具兼容性不通过。此次按测试请求仅定位并记录，没有修改该转换逻辑或追加模型调用。
