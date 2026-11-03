# Antigravity 动态养号功能规格

## Objective

为同一出口 IP 下的多个 Antigravity 账号提供由 UI 驱动的渐进放量能力。新账号先以低权重、低速率承接真实业务流量，在持续成功的窗口内逐步提高账号与 IP 组流量；异常时自动降速、暂停或退出。

目标不是增加额外“养号请求”，而是控制现有生产流量进入新账号的速度，降低新号突发流量和单 IP 多账号同时放量的风险。该功能不能保证避免 TOS，也不用于伪造官方客户端行为。

### 用户故事与验收目标

- 管理员可在账号卡片上单独开启养号，设置初始参数并查看进度。
- 管理员可批量开启、批量取消养号；取消立即生效且操作幂等。
- 新账号只承接真实业务请求，系统不得为了养号额外生成上游请求。
- 账号按绑定出口 IP 分组，同组账号共享渐进流量预算，避免“每账号低并发、同 IP 总流量仍过高”。
- 多 Worker、多副本使用同一持久化状态，不改变现有 Worker 和部署参数。
- TOS/account_forbidden 立即永久退出号池，不参与自动恢复或周期复检。

## Tech Stack

- Python 3.13、FastAPI、asyncio、aiosqlite。
- SQLite 为账号状态、养号状态和调度预算的事实来源。
- 现有 `src/credential_manager.py`、`src/storage/sqlite_manager.py`、`src/antigravity_pacing.py` 与账号管理 UI。
- 不新增第三方依赖，不修改 Worker 数量、容器副本或现有启动命令。

## Commands

```bash
# 专项测试（实现后按实际文件补充）
.venv/bin/python -m pytest \
  tests/test_antigravity_dynamic_warmup.py \
  tests/test_antigravity_pacing_slots.py \
  tests/test_antigravity_403_recheck.py \
  tests/test_proxy_groups.py -q

# 全量回归
.venv/bin/python -m pytest tests/ -q

# 静态检查
.venv/bin/python -m compileall -q config.py log.py web.py src tests
node --check front/common.js

# 发布：只通过 GitHub Actions
# 推送 custom 后，手动运行 Release to GHCR workflow。
```

## Project Structure

```text
src/
  antigravity_dynamic_warmup.py   # 状态机、成功/失败反馈与预算计算
  credential_manager.py           # 账号选择与养号状态接入
  antigravity_pacing.py           # 真实在飞槽位与发起节奏
  storage/sqlite_manager.py       # 持久化、原子推进和 IP 组预算
  panel/                         # 单账号/批量 UI API
front/
  common.js                       # 进度、配置和批量操作
  control_panel.html              # 桌面 UI
  control_panel_mobile.html       # 移动 UI
tests/
  test_antigravity_dynamic_warmup.py
  test_antigravity_pacing_slots.py
  test_antigravity_403_recheck.py
  test_proxy_groups.py
docs/
  ANTIGRAVITY_DYNAMIC_WARMUP_SPEC.md
  CUSTOMIZATION.md
```

## Code Style

状态迁移集中在一个模块，API、存储和调度不得各自推断另一套状态：

```python
@dataclass(frozen=True)
class WarmupDecision:
    eligible: bool
    account_limit: int
    ip_group_limit: int
    reason: str = ""


def decision_for(account: WarmupState, ip_group: IpGroupBudget) -> WarmupDecision:
    if account.status != "warming":
        return WarmupDecision(False, 0, ip_group.limit, account.status)
    return WarmupDecision(True, account.current_limit, ip_group.current_limit)
```

- 配置和状态使用显式名称，不使用含义模糊的 `level`、`stage` 混称。
- 状态变更必须原子化并持久化；内存只作缓存，不作为跨 Worker 事实来源。
- 默认不开启养号，已有账号行为保持不变。
- UI 配置校验与后端校验一致，非法参数不得进入数据库。

## Testing Strategy

- 单元测试：状态机、预算计算、成功窗口、失败回退、取消/恢复、参数边界。
- SQLite 集成测试：并发选择、跨批次持久化、同 IP 多账号预算、重启恢复。
- API 测试：单账号开启/取消、批量开启/取消、非法输入、重复操作幂等。
- 调度测试：只使用真实请求推进进度，忙号避让，取消后不再被选中，TOS 永久排除。
- UI 验证：桌面和移动端均可完成配置、查看进度和批量取消；无 JavaScript 控制台错误。
- 回归：现有完整 pytest 全过，不通过删除或放宽旧测试掩盖行为回归。

## Boundaries

- Always：真实业务流量驱动；账号固定出口；TOS 永久退出；测试通过后提交；发布只走 GitHub Actions。
- Ask first：新增数据库表/字段、改变默认调度行为、增加依赖、修改生产 Worker 或副本、改变批量账号状态。
- Never：额外生成养号请求；伪造真人或官方客户端行为；自动复活 TOS 账号；记录 Token、完整邮箱或请求正文；本地执行特权 binfmt。

## Functional Specification

### 状态与操作

状态：`off / warming / paused / active / account_forbidden`。

- 开启养号：`off/paused -> warming`，从初始预算开始。
- 取消养号：`warming/paused -> off`，立即停止渐进放量并将账号排除出候选池；不删除凭证、不清除历史进度。
- 恢复养号：`off -> warming`，可从保留进度恢复；UI 另提供“重新开始”以清空进度。
- 提前完成：`warming/paused -> active`，立即按普通账号使用，明确二次确认。
- TOS：任意状态 `-> account_forbidden`，禁用账号且不可自动恢复。
- 单账号和批量操作使用同一状态转换函数，重复取消返回当前状态并报告 `already_inactive`，不报错。

### 渐进预算

- 账号维度：初始在飞上限、初始发起间隔、当前上限、目标上限。
- IP 组维度：同出口 IP 的账号共享初始/当前/目标预算，组内账号数增加不得线性放大总流量。
- 只有在“连续真实业务成功数量 + 最短观察时间”同时满足后推进一档；健康检查、额度查询和自动复检不计入成功。
- 429：暂停推进并退避；连续 429 降低一档。
- validation/geo/proxy 异常：暂停对应账号或出口组，不按成功推进。
- unknown_403：保持当前档位，不继续放量。
- TOS/account_forbidden：永久退出。
- 预算调整后原子写入 SQLite，所有 Worker 在下一账号选择周期内生效。

### UI 配置

- 单账号：开关、当前状态、当前档位、成功进度、下一档条件、取消、恢复、提前完成。
- 批量：仅对已勾选账号执行相同操作，返回成功、已处于目标状态、失败三项明细。
- IP 组：显示绑定出口、账号数、组预算和组内异常；不得展示代理密码。
- 全局默认参数可在基础配置修改，账号/IP 组覆盖值优先；不得覆盖现有每账号并发 UI 配置。

## Success Criteria

1. 未开启养号的账号与现有行为完全一致，不产生额外数据库写入外的上游请求。
2. 养号账号仅由真实业务成功推进，等待时间和成功数缺一不可。
3. 同一 IP 下 1 个账号与多个账号的组总在飞/发起预算均不超过 UI 配置。
4. 两个 Worker 同时选择账号时不会突破同一持久化预算，重启后进度不丢失。
5. 单账号/批量取消后，账号在下一选择周期即不可被选中；重复操作幂等。
6. 429、可恢复 403、TOS 分别执行退避/暂停/永久退出，TOS 不被自动复检重新启用。
7. 全部新旧测试通过，生产发布不改变 Worker、副本和现有启动参数。

## Open Questions

以下默认值在实现评审时确认，不先写死到生产：

- 初始账号并发建议为 1，目标值沿用现有 UI 的每账号并发上限。
- 初始 IP 组并发建议为 1，目标值由管理员显式设置。
- 首档观察建议至少 10 次真实成功且满 30 分钟；后续档位逐级增加观察窗口。
- 取消默认保留进度以便恢复，“重新开始”才清空进度。
