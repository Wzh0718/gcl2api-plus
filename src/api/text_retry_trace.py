"""记录一次文本换号的实际发送过程，不保存正文或认证信息。"""

import os
import time
from typing import Any

from starlette.responses import Response


class TextRetryTrace:
    """只在触发换号时创建；生命周期限定在单个网关请求内。"""

    def __init__(
        self, account: str, model: str, response: Response, elapsed: float
    ) -> None:
        self.state = "selection_failed"
        self.attempts = [self._account(account, model)]
        self.started: float | None = None
        self.respond(response.status_code, elapsed, response)

    @staticmethod
    def _account(account: str, model: str) -> dict[str, Any]:
        return {"credential_name": os.path.basename(account), "model": model}

    def selected(self, account: str | None, model: str) -> None:
        """记录候选名；未选到账号时不创建第二次尝试。

        Args:
            account: 备选凭证名，不包含认证数据。
            model: 为该备选账号请求的模型。
        """
        if account is None:
            self.state = "no_candidate"
        else:
            self.state = "not_sent"
            self.attempts.append(self._account(account, model))

    def sending(self, account: str, model: str, *, same_account: bool = False) -> None:
        """只在准入完成、开始调用上游传输时登记实际尝试。

        Args:
            account: 本次发送实际使用的凭证名。
            model: 发往上游的模型名。
            same_account: 是否为容量型 503 的原号补试。
        """
        attempt = self._account(account, model)
        if same_account:
            attempt["kind"] = "same_account"
            self.attempts.append(attempt)
        else:
            self.state = "sent"
            self.attempts[-1] = attempt
        self.started = time.monotonic()

    def respond(
        self, status: int, elapsed: float, response: Response | None = None
    ) -> None:
        """保存上游状态、耗时和可公开的限流信息。

        Args:
            status: 上游 HTTP 状态码，而非网关对外映射的状态码。
            elapsed: 从本次尝试开始到响应的秒数，不含选取备选账号的时间。
            response: 带有限流分类和可选 Retry-After 的响应。
        """
        attempt = self.attempts[-1]
        attempt.update(status_code=status, elapsed_ms=round(elapsed * 1000))
        if response is not None:
            attribution = getattr(response, "rate_limit", None)
            if attribution is not None:
                attempt["rate_limit"] = attribution.as_dict()
            retry_after = response.headers.get("retry-after")
            if retry_after:
                attempt["retry_after"] = retry_after[:128]

    def snapshot(self) -> dict[str, Any]:
        """生成独立的入库摘要。

        Returns:
            换号进度和有序尝试；无 HTTP 响应时省略状态码。
        """
        attempts = [dict(attempt) for attempt in self.attempts]
        if self.started is not None and "elapsed_ms" not in attempts[-1]:
            attempts[-1]["elapsed_ms"] = round((time.monotonic() - self.started) * 1000)
        return {"state": self.state, "attempts": attempts}
