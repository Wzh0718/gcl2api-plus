"""Antigravity 失败的统一枚举与处置表。

背景（2026-11-28 复盘）：同一份故障曾被四条链路各自判定、各写各的字段 ——
文本流式、文本非流式、图像、面板「消息测试」，再加上 ``handle_error_with_retry``
的兜底分支。结果是同一个 503 可能同时被写成「账号冷藏 12h」「模型冷却 40s」
「轮转降权」，三份结论互相矛盾；``antigravity_transient_hold_seconds`` 也被常量
覆盖，改了不起作用。

根因不是缺少一个入口，而是每个场景都单独加了一条特判。这里改成一张表：

    状态码 + 上游正文  →  FailureKind（枚举）
    FailureKind        →  FailureAction（动作表，唯一真相）

``400 / 401 / 403 / 429 / 500 / 502 / 503`` 全部先归一成同一个枚举，链路只按
动作表执行：换号、原号重试、冷藏多久、是否冷模型、是否降权、是否停用、是否写
面板错误码。判定复用的仍是既有分类器（400/403/503），不重复解析正文。

只有一处在决定「冷藏多久」：``HOLD_FROM_CONFIG`` 表示读配置
``antigravity_transient_hold_seconds``（默认 12 小时），``0`` 表示不冷藏，
其余数值即固定窗口（侧车合成 502 只压一跳 60 秒）。
"""

import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from log import log
from src.antigravity_400_classifier import classify_antigravity_400
from src.antigravity_error_classifier import (
    CATEGORY_MODEL_CAPACITY,
    CATEGORY_MODEL_OVERLOADED,
    CATEGORY_QUOTA,
    CATEGORY_SERVICE_UNAVAILABLE,
    CATEGORY_UNKNOWN,
    classify_antigravity_503,
)

# 账号级 5xx 的冷藏档位。2026-11-21 事故后定为半天：5xx 往往是账号级处置的
# 前兆（503 连击 → 短恢复 → 403/TOS），窗口太短会让同一个账号在十分钟内被反复
# 放回轮转，重复制造同样的失败模式。到期后由 warmup 组件低并发放量。
DEFAULT_ACCOUNT_HOLD_SECONDS = 43200.0
# 侧车合成 502 表示上游一个字节都没回：同一个出口前后一秒仍可能返回 200，
# 只压一跳短窗口，既不给账号记账，也不吞噬 12h 档。
RELAY_HOLD_SECONDS = 60.0
# 动作表哨兵：冷藏时长取配置值（账号级 5xx）。
HOLD_FROM_CONFIG = -1.0
# 动作表哨兵：冷藏时长取传输层冷却窗口（面板可配，默认 60s）。
HOLD_RELAY = -2.0

# 与 503 正文分类共用的类别取值；FailureKind 的 value 与之一一对应。
CATEGORY_RELAY_TRANSPORT = "relay_transport"
CATEGORY_REQUEST_INVALID = "request_invalid"
CATEGORY_AUTH_INVALID = "auth_invalid"
CATEGORY_ACCOUNT_FORBIDDEN = "account_forbidden"
CATEGORY_GEO_BLOCKED = "geo_blocked"
CATEGORY_PROXY_BINDING = "proxy_binding"
CATEGORY_VALIDATION_REQUIRED = "validation_required"
CATEGORY_MODEL_FORBIDDEN = "model_forbidden"
CATEGORY_RATE_LIMITED = "rate_limited"
CATEGORY_UPSTREAM_INTERNAL = "upstream_internal"
CATEGORY_UPSTREAM_BAD_GATEWAY = "upstream_bad_gateway"


class FailureKind(StrEnum):
    """一次上游失败归一后的枚举值；跨链路唯一。"""

    RELAY_TRANSPORT = CATEGORY_RELAY_TRANSPORT
    REQUEST_INVALID = CATEGORY_REQUEST_INVALID
    AUTH_INVALID = CATEGORY_AUTH_INVALID
    ACCOUNT_FORBIDDEN = CATEGORY_ACCOUNT_FORBIDDEN
    GEO_BLOCKED = CATEGORY_GEO_BLOCKED
    PROXY_BINDING = CATEGORY_PROXY_BINDING
    VALIDATION_REQUIRED = CATEGORY_VALIDATION_REQUIRED
    MODEL_FORBIDDEN = CATEGORY_MODEL_FORBIDDEN
    RATE_LIMITED = CATEGORY_RATE_LIMITED
    MODEL_CAPACITY = CATEGORY_MODEL_CAPACITY
    QUOTA_EXHAUSTED = CATEGORY_QUOTA
    MODEL_OVERLOADED = CATEGORY_MODEL_OVERLOADED
    SERVICE_UNAVAILABLE = CATEGORY_SERVICE_UNAVAILABLE
    UPSTREAM_INTERNAL = CATEGORY_UPSTREAM_INTERNAL
    UPSTREAM_BAD_GATEWAY = CATEGORY_UPSTREAM_BAD_GATEWAY
    UNKNOWN = CATEGORY_UNKNOWN


@dataclass(frozen=True)
class FailureAction:
    """一个枚举值对应的动作；这是「该做什么」的唯一真相。"""

    kind: FailureKind
    #: 是否换号重投。
    retry_other_account: bool = False
    #: 是否允许同一个号在容量窗口里再试一次。
    retry_same_account: bool = False
    #: 账号冷藏时长：``HOLD_FROM_CONFIG`` 读配置，``0`` 不冷藏，其余为固定秒数。
    hold_seconds: float = 0.0
    #: 是否按上游 Retry-After / 配额重置解析模型级冷却。
    model_cooldown: bool = False
    #: 容量型：把账号在轮转队列里往后挪，仍可被选中。
    demote: bool = False
    #: 是否停用账号（凭证失效 / TOS）。
    disable_account: bool = False
    #: 是否把状态码写进面板错误码（传输层失败不写）。
    record_error_state: bool = True


def _action(kind: FailureKind, **overrides) -> FailureAction:
    return FailureAction(kind=kind, **overrides)


# 动作表。判断口径按「上游是否应答过」与「是否归咎账号」两条线划分，与代码里
# 既有注释保持一致；新增状态码只在这里加一行。
ACTION_TABLE: dict[FailureKind, FailureAction] = {
    # 传输层：上游没应答，不归咎账号，也不污染错误画像。
    FailureKind.RELAY_TRANSPORT: _action(
        FailureKind.RELAY_TRANSPORT,
        retry_other_account=True,
        hold_seconds=HOLD_RELAY,
        record_error_state=False,
    ),
    # 请求本身不合法：换号也没用，不罚账号（AGY 400 曾把整池号打封）。
    FailureKind.REQUEST_INVALID: _action(FailureKind.REQUEST_INVALID),
    # 凭证失效：停用并换号。
    FailureKind.AUTH_INVALID: _action(
        FailureKind.AUTH_INVALID, retry_other_account=True, disable_account=True,
    ),
    # TOS/账号级封禁：停用并换号。
    FailureKind.ACCOUNT_FORBIDDEN: _action(
        FailureKind.ACCOUNT_FORBIDDEN, retry_other_account=True, disable_account=True,
    ),
    # 地区拒绝：不罚账号，换号；绑定修复由 403 处理器另行处理。
    FailureKind.GEO_BLOCKED: _action(
        FailureKind.GEO_BLOCKED, retry_other_account=True,
    ),
    # 绑定漂移：不罚账号，换出口后重试。
    FailureKind.PROXY_BINDING: _action(
        FailureKind.PROXY_BINDING, retry_other_account=True,
    ),
    # 需二次验证 / 模型不可用：只冷该模型并换号。
    FailureKind.VALIDATION_REQUIRED: _action(
        FailureKind.VALIDATION_REQUIRED,
        retry_other_account=True,
        model_cooldown=True,
    ),
    FailureKind.MODEL_FORBIDDEN: _action(
        FailureKind.MODEL_FORBIDDEN,
        retry_other_account=True,
        model_cooldown=True,
    ),
    # 429：按重置时间冷该模型并换号，不冷藏账号。
    FailureKind.RATE_LIMITED: _action(
        FailureKind.RATE_LIMITED,
        retry_other_account=True,
        model_cooldown=True,
    ),
    # 容量耗尽：上游全局窗口，只降权 + 一次原号重试，不冷藏、不写冷却。
    FailureKind.MODEL_CAPACITY: _action(
        FailureKind.MODEL_CAPACITY,
        retry_other_account=True,
        retry_same_account=True,
        demote=True,
        model_cooldown=False,
    ),
    # 配额型 503：只冷该模型并换号。
    FailureKind.QUOTA_EXHAUSTED: _action(
        FailureKind.QUOTA_EXHAUSTED,
        retry_other_account=True,
        model_cooldown=True,
    ),
    # 过载 / 服务级 / 500 / 502：账号级故障，冷藏配置时长并换号。
    FailureKind.MODEL_OVERLOADED: _action(
        FailureKind.MODEL_OVERLOADED,
        retry_other_account=True,
        hold_seconds=HOLD_FROM_CONFIG,
        model_cooldown=True,
    ),
    FailureKind.SERVICE_UNAVAILABLE: _action(
        FailureKind.SERVICE_UNAVAILABLE,
        retry_other_account=True,
        hold_seconds=HOLD_FROM_CONFIG,
        model_cooldown=True,
    ),
    FailureKind.UPSTREAM_INTERNAL: _action(
        FailureKind.UPSTREAM_INTERNAL,
        retry_other_account=True,
        hold_seconds=HOLD_FROM_CONFIG,
    ),
    FailureKind.UPSTREAM_BAD_GATEWAY: _action(
        FailureKind.UPSTREAM_BAD_GATEWAY,
        retry_other_account=True,
        hold_seconds=HOLD_FROM_CONFIG,
    ),
    # 未知状态码：由 auto-ban 配置与调用方决定，表里不产生账号级处置。
    FailureKind.UNKNOWN: _action(FailureKind.UNKNOWN),
}


def action_for(kind: FailureKind) -> FailureAction:
    return ACTION_TABLE.get(kind, ACTION_TABLE[FailureKind.UNKNOWN])


@dataclass(frozen=True)
class FailureDisposition:
    """一次失败该触发什么动作；由动作表实例化，只描述意图，不写存储。"""

    status_code: int
    kind: FailureKind
    reason: str = ""
    #: >0 表示把账号放进就地冷却窗口，值为窗口秒数。
    account_hold_seconds: float = 0.0
    #: 是否按上游 Retry-After / 配额重置解析模型级冷却。
    model_cooldown: bool = False
    #: 容量型：只把这个号在轮转队列里往后挪，仍可被选中。
    demote_round_robin: bool = False
    #: 容量型：允许同一个号在同一窗口里再试一次。
    retry_same_account: bool = False
    #: 是否换号重投。
    retry_other_account: bool = False
    #: 是否停用账号。
    disable_account: bool = False
    #: 是否把状态码写进面板错误码（传输层失败不写）。
    record_error_state: bool = True
    #: 503 正文里的 RetryInfo.retryDelay（秒），容量窗口与 Retry-After 用。
    upstream_delay_seconds: Optional[float] = None
    #: 503 正文里 metadata.model；容量窗口需要同时压制该模型的入口。
    upstream_model: str = ""

    @property
    def category(self) -> str:
        """兼容旧字段：类别字符串即枚举值。"""
        return self.kind.value

    @property
    def holds_account(self) -> bool:
        return self.account_hold_seconds > 0

    def describe(self) -> str:
        """一行排障文本，进日志用。"""
        parts = [f"kind={self.kind}", f"status={self.status_code}"]
        if self.reason:
            parts.append(f"reason={self.reason}")
        parts.append(f"hold={self.account_hold_seconds:g}s")
        if self.model_cooldown:
            parts.append("model_cooldown")
        if self.demote_round_robin:
            parts.append("demote")
        if self.disable_account:
            parts.append("disable")
        if not self.record_error_state:
            parts.append("no_error_state")
        return ", ".join(parts)

    # 旧调用方按 category 比较；这里把枚举值映射回常量字符串。
    def is_kind(self, kind: FailureKind) -> bool:
        return self.kind is kind


_FORBIDDEN_KIND_MAP = {
    "account_forbidden": FailureKind.ACCOUNT_FORBIDDEN,
    "geo_blocked": FailureKind.GEO_BLOCKED,
    "proxy_drift": FailureKind.PROXY_BINDING,
    "proxy_failed": FailureKind.PROXY_BINDING,
    "validation_required": FailureKind.VALIDATION_REQUIRED,
    "model_forbidden": FailureKind.MODEL_FORBIDDEN,
}


def kind_for_forbidden(category: str) -> FailureKind:
    """把 403 分类器的 category 映射到统一枚举；未知按账号级处理。"""
    return _FORBIDDEN_KIND_MAP.get(str(category or ""), FailureKind.UNKNOWN)


def _kind_and_detail(
    status_code: int,
    error_text: Optional[str],
    *,
    relay_failure: bool,
    forbidden_category: str,
):
    """归一的同时带回 503 正文细节（容量窗口需要 retryDelay/upstream model）。"""
    if relay_failure:
        return FailureKind.RELAY_TRANSPORT, None
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 400:
        # 400 的正文分类器只用于确定「是否要停用」，请求级仍归入 REQUEST_INVALID。
        decision = classify_antigravity_400(error_text)
        kind = (
            FailureKind.AUTH_INVALID
            if decision.disable_account
            else FailureKind.REQUEST_INVALID
        )
        return kind, None
    if code == 401:
        return FailureKind.AUTH_INVALID, None
    if code == 403:
        return kind_for_forbidden(forbidden_category), None
    if code == 429:
        return FailureKind.RATE_LIMITED, None
    if code == 500:
        return FailureKind.UPSTREAM_INTERNAL, None
    if code == 502:
        return FailureKind.UPSTREAM_BAD_GATEWAY, None
    if code == 503:
        classified = classify_antigravity_503(code, error_text)
        # 503 只有「容量 / 配额 / 过载」三种例外；其余（含正文为空、无法解析）
        # 一律按服务级账号故障处理，与 2026-11-21 事故后的既定口径一致。
        kind = {
            CATEGORY_MODEL_CAPACITY: FailureKind.MODEL_CAPACITY,
            CATEGORY_QUOTA: FailureKind.QUOTA_EXHAUSTED,
            CATEGORY_MODEL_OVERLOADED: FailureKind.MODEL_OVERLOADED,
        }.get(classified.category, FailureKind.SERVICE_UNAVAILABLE)
        return kind, classified
    return FailureKind.UNKNOWN, None


def classify_failure_kind(
    status_code: int,
    error_text: Optional[str] = "",
    *,
    relay_failure: bool = False,
    forbidden_category: str = "",
) -> FailureKind:
    """把一次失败归一成枚举值；纯函数，便于单测。

    403 的细分依赖账号健康上下文，由调用方先跑 ``classify_antigravity_403``
    再把 ``category`` 传进来；这里只做映射，不重复判定。
    """
    return _kind_and_detail(
        status_code,
        error_text,
        relay_failure=relay_failure,
        forbidden_category=forbidden_category,
    )[0]


def _resolve_hold(
    action: FailureAction,
    configured: Optional[float],
    relay_configured: Optional[float],
) -> float:
    if action.hold_seconds == HOLD_FROM_CONFIG:
        return (
            DEFAULT_ACCOUNT_HOLD_SECONDS
            if configured is None
            else max(float(configured), 0.0)
        )
    if action.hold_seconds == HOLD_RELAY:
        return (
            RELAY_HOLD_SECONDS
            if relay_configured is None
            else max(float(relay_configured), 0.0)
        )
    return max(float(action.hold_seconds), 0.0)


def decide(
    status_code: int,
    error_text: Optional[str] = "",
    *,
    relay_failure: bool = False,
    forbidden_category: str = "",
    account_hold_seconds: Optional[float] = None,
    relay_hold_seconds: Optional[float] = None,
) -> FailureDisposition:
    """归因并查表，产出一份处置；纯函数。

    Args:
        status_code: 上游/侧车返回的状态码。
        error_text: 响应正文，503/400 分类依赖它。
        relay_failure: 侧车合成的传输层失败（上游未应答）。
        forbidden_category: 403 分类器的 ``category``，由调用方先算好。
        account_hold_seconds: 账号级 5xx 的冷藏窗口；``None`` 取默认 12h。
        relay_hold_seconds: 传输层失败的冷却窗口；``None`` 取默认 60s。
    """
    kind, classified_503 = _kind_and_detail(
        status_code,
        error_text,
        relay_failure=relay_failure,
        forbidden_category=forbidden_category,
    )
    action = action_for(kind)
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    return FailureDisposition(
        status_code=code,
        kind=kind,
        account_hold_seconds=_resolve_hold(
            action, account_hold_seconds, relay_hold_seconds
        ),
        model_cooldown=action.model_cooldown,
        demote_round_robin=action.demote,
        retry_same_account=action.retry_same_account,
        retry_other_account=action.retry_other_account,
        disable_account=action.disable_account,
        record_error_state=action.record_error_state,
        upstream_delay_seconds=getattr(classified_503, "upstream_delay_seconds", None),
        upstream_model=str(getattr(classified_503, "upstream_model", "") or ""),
    )


async def resolve_account_hold_seconds() -> float:
    """读取账号级 5xx 的冷藏时长；配置不可用时退回默认 12h。"""
    try:
        from config import get_antigravity_transient_hold_seconds

        value = float(await get_antigravity_transient_hold_seconds())
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不能影响错误处理
        log.warning(
            f"[ANTIGRAVITY FAILURE] 读取冷藏时长失败，使用默认 "
            f"{DEFAULT_ACCOUNT_HOLD_SECONDS:g}s: {type(exc).__name__}: {exc}"
        )
        return DEFAULT_ACCOUNT_HOLD_SECONDS
    if value <= 0:
        return DEFAULT_ACCOUNT_HOLD_SECONDS
    return value


async def resolve_relay_hold_seconds() -> float:
    """传输层失败的冷却窗口；面板「凭证与重试 → 传输抖动冷却」可调。"""
    try:
        from config import get_antigravity_relay_hold_seconds

        value = float(await get_antigravity_relay_hold_seconds())
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不能影响错误处理
        log.warning(
            f"[ANTIGRAVITY FAILURE] 读取传输层冷却失败，使用默认 "
            f"{RELAY_HOLD_SECONDS:g}s: {type(exc).__name__}: {exc}"
        )
        return RELAY_HOLD_SECONDS
    return max(value, 0.0)


async def relay_hold_seconds() -> float:
    """兼容别名：与 ``resolve_relay_hold_seconds`` 同义。"""
    return await resolve_relay_hold_seconds()


async def decide_failure(
    status_code: int,
    error_text: Optional[str] = "",
    *,
    relay_failure: bool = False,
    forbidden_category: str = "",
) -> FailureDisposition:
    """``decide`` 的异步版：先读两处配置窗口，再归因。"""
    return decide(
        status_code,
        error_text,
        relay_failure=relay_failure,
        forbidden_category=forbidden_category,
        account_hold_seconds=await resolve_account_hold_seconds(),
        relay_hold_seconds=await resolve_relay_hold_seconds(),
    )


async def apply_hold(
    credential_manager,
    credential_name: str,
    disposition: FailureDisposition,
) -> float:
    """把 disposition 的账号级处置写入存储；这是唯一的冷藏写入点。

    不抛异常：一次失败的留痕不能把可重试的 5xx 变成请求失败。
    返回写入后的冷却截止时间，未冷藏返回 0。
    """
    if not disposition.holds_account or not credential_name:
        return 0.0
    hold = getattr(credential_manager, "hold_transient", None)
    if hold is None:
        # 存储管理器（SQLiteManager/PostgresManager）直接暴露的是
        # ``hold_antigravity_transient``；两者参数名一致，可共用同一调用。
        hold = getattr(credential_manager, "hold_antigravity_transient", None)
    if hold is None:
        return 0.0
    kwargs = {
        "status_code": disposition.status_code,
        "mode": "antigravity",
        "seconds": disposition.account_hold_seconds,
        "relay_failure": disposition.kind is FailureKind.RELAY_TRANSPORT,
    }
    # 按目标实现的签名裁剪关键字：CredentialManager 接受全部四项，存储管理器
    # （SQLiteManager/PostgresManager）只接受 status_code/seconds/relay_failure，
    # 测试替身可能只接受旧签名。无法取得签名时退回最小集合，避免把可重试的
    # 5xx 变成请求失败。
    try:
        params = inspect.signature(hold).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_extra = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
    )
    if accepts_extra:
        selected = kwargs
    elif params:
        selected = {key: value for key, value in kwargs.items() if key in params}
    else:
        selected = {
            "status_code": disposition.status_code,
            "mode": "antigravity",
        }
    try:
        return float(await hold(credential_name, **selected) or 0.0)
    except Exception as exc:  # noqa: BLE001 - retry path must stay intact
        log.warning(
            f"[ANTIGRAVITY FAILURE] hold failed for {credential_name}: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0.0


async def apply_action(
    credential_manager,
    credential_name: str,
    disposition: FailureDisposition,
    *,
    disable_account: Optional[bool] = None,
) -> float:
    """统一执行一次处置：停用 + 冷藏；这是唯一的账号级写入点。

    停用意图默认取动作表，调用方可用 ``disable_account`` 覆盖：403 处理需要
    账号健康与 ``AUTO_BAN_ERROR_CODES`` 配置上下文，只有它能判定「未知 403
    是否算账号级拒绝」，但写入仍然集中在这里。

    不抛异常：一次失败的留痕不能把可重试的 5xx 变成请求失败。
    返回冷藏截止时间，未冷藏返回 0。
    """
    if not credential_name:
        return 0.0
    should_disable = (
        disposition.disable_account if disable_account is None else disable_account
    )
    if should_disable:
        set_disabled = getattr(credential_manager, "set_cred_disabled", None)
        if set_disabled is not None:
            try:
                await set_disabled(credential_name, True, mode="antigravity")
            except Exception as exc:  # noqa: BLE001 - 停用失败不得打断换号
                log.warning(
                    f"[ANTIGRAVITY FAILURE] disable failed for {credential_name}: "
                    f"{type(exc).__name__}: {exc}"
                )
    return await apply_hold(credential_manager, credential_name, disposition)


__all__ = [
    "ACTION_TABLE",
    "CATEGORY_ACCOUNT_FORBIDDEN",
    "CATEGORY_AUTH_INVALID",
    "CATEGORY_GEO_BLOCKED",
    "CATEGORY_MODEL_CAPACITY",
    "CATEGORY_MODEL_FORBIDDEN",
    "CATEGORY_MODEL_OVERLOADED",
    "CATEGORY_PROXY_BINDING",
    "CATEGORY_QUOTA",
    "CATEGORY_RATE_LIMITED",
    "CATEGORY_RELAY_TRANSPORT",
    "CATEGORY_REQUEST_INVALID",
    "CATEGORY_SERVICE_UNAVAILABLE",
    "CATEGORY_UNKNOWN",
    "CATEGORY_UPSTREAM_BAD_GATEWAY",
    "CATEGORY_UPSTREAM_INTERNAL",
    "CATEGORY_VALIDATION_REQUIRED",
    "DEFAULT_ACCOUNT_HOLD_SECONDS",
    "FailureAction",
    "FailureDisposition",
    "FailureKind",
    "HOLD_FROM_CONFIG",
    "HOLD_RELAY",
    "RELAY_HOLD_SECONDS",
    "action_for",
    "apply_action",
    "apply_hold",
    "classify_failure_kind",
    "decide",
    "decide_failure",
    "kind_for_forbidden",
    "relay_hold_seconds",
    "resolve_relay_hold_seconds",
    "resolve_account_hold_seconds",
]
