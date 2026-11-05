# 400 分类修复与账号复核结果

日期：2026-11-04，Asia/Singapore。

## 修复

提交：[af259ee](https://github.com/Wzh0718/gcl2api-plus/commit/af259ee)，分支 `codex/agy-400-classification`。基于线上 `3c1a23a` 独立修改，不包含原生传输或本地代理组改动。

- AGY 文本流式、非流式和图片生成的 400 不再触发整号自动停用或跨账号重试；客户端仍收到原始 400。
- 生成错误区分参数、模型、地区、账号验证、限制证据和未知类型。400 分类仅用于诊断，不据此声称永久封禁；已有 403/TOS 处置保持不变。
- `error_messages['400']` 保存结构化 `source/category/reason/message`，包括无正文的 unknown_400。诊断和 400 日志使用固定摘要，不保存上游原文中的 token、邮箱或请求内容。
- OAuth 异常保留 HTTP status 和最终 client 的结构化 error；不会用之前某个 client 的失败文字覆盖最终原因。client 配置错误、裸 400/401/403 不再等同于授权永久失效；明确 invalid_grant 等授权失效证据仍保持停用。
- 修复旧 OAuth 刷新失败只留下 `[400]` 和空错误正文的问题。

验证：62 项新增专项回归；全量 **757 passed**；compileall、JavaScript 语法及 diff 检查通过。

## 线上应用

应用前确认四个原文件哈希与部署基线完全一致。备份目录：服务器 `/opt/gcli2api/hotfixes/agy400-af259ee`，包含原源码包、账号状态快照和补丁说明。

已把五个已提交源码文件作为容器源码补丁应用并重启服务；面板 HTTP 200，容器 running/healthy。部署后五个文件的 SHA256 与提交工作树逐项相同。

**正式 GHCR 镜像尚未更新。** 当前补丁可保留至普通容器重启，但重建/替换容器会丢失；后续镜像发布必须包含 af259ee。原生传输未在本次启用。

## 原先 19 个无明确封禁原因的 400 停用账号

最终范围按原先条件复核：disabled、error_codes=[400]，且错误及历史健康原因中没有 TOS、待验证或订阅限制证据；不依赖旧 eligible 缓存来认定资格。

| 当前核查结果 | 数量 | 决定 |
|---|---:|---|
| OAuth 刷新返回 invalid_grant | 19 | 保持停用，恢复 0 个 |

每个账号均逐个尝试刷新授权；这些账号在授权阶段即未通过，因此未继续发送生成消息或消耗生成配额。最终逐条读取数据库确认：19 个全部 disabled，且均保存 `source=oauth / category=oauth_invalid_grant`。

初次按 eligible 缓存筛选时混入一个已有历史 TOS 记录的账号，并遗漏一个 unchecked 账号。交叉核对后已纠正名单：有 TOS 证据的账号未发送探测、未恢复；unchecked 账号已补查，结果同为 invalid_grant。已核查账号没有重复探测。

这证明上述账号**当前授权不可用**，不证明整个 Google 账号被封，也不能由此确定失效是否由指纹识别引起。需要核对授权 client，并在重新登录取得有效授权后再核查资格；单纯把 disabled 改为 false 不能解决问题。

逐账号脱敏结果：[JSON](antigravity-400-qualification-2026-11-04.json)。完整名单和执行审计保存在服务器凭证目录内，没有上传 GitHub；本地原有 Kimi 工作区保留。
