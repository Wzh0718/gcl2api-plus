"""记录一次逻辑请求内「网关把请求改投到别的模型」的每一跳原因。

调用监控只看 ``request_model`` / ``model_name`` / ``upstream_model`` 三个字段时，
两种完全不同的跳转会长得一样：

- 网关按降级链改投（容量 503、入口容量窗口、无可用凭证、首字超时）；
- 上游把请求路由到另一个物理模型（``modelVersion`` 与请求模型不同）。

前端只能靠字段先后猜。这里把网关侧的每次改投连同原因记进
``request_features.model_fallback``，监控据此给每一跳打「网关 / 上游」标签。

只保存模型名与原因，不保存请求正文或认证信息。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 改投原因：与破障路径一一对应，新增原因时同步前端 _cmModelHopLabel。
REASON_CAPACITY_WINDOW = "capacity_window"
REASON_NO_CREDENTIAL = "no_credential"
REASON_TTFT_TIMEOUT = "ttft_timeout"
REASON_CAPACITY_503 = "capacity_503"

REASON_LABELS = {
    REASON_CAPACITY_WINDOW: "容量窗口",
    REASON_NO_CREDENTIAL: "无可用账号",
    REASON_TTFT_TIMEOUT: "首字超时",
    REASON_CAPACITY_503: "容量 503",
}


class ModelFallbackTrace:
    """一次请求内按发生顺序累积网关改投；不改投时快照为 None。"""

    def __init__(self) -> None:
        self._events: List[Dict[str, str]] = []

    def record(self, reason: str, source: str, target: str) -> None:
        """记录一跳改投；起点/终点相同或缺失时忽略。"""
        source = (source or "").strip()
        target = (target or "").strip()
        if not source or not target or source == target:
            return
        self._events.append({"reason": reason, "from": source, "to": target})

    @property
    def events(self) -> List[Dict[str, str]]:
        return [dict(event) for event in self._events]

    def snapshot(self) -> Optional[Dict[str, Any]]:
        """生成入库摘要；没有改投时返回 None，避免给普通请求加字段。"""
        if not self._events:
            return None
        events = self.events
        return {"events": events, "reason": events[-1]["reason"]}
