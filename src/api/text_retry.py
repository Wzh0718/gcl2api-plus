"""文本上游错误的单次换号策略，与图片重试和账号封禁策略独立。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.credential_manager import CredentialManager

DEFAULT_STATUS_CODES = (429, 502)


def parse_status_codes(value: object) -> list[int]:
    """校验并去重面板配置；显式空列表表示停用。

    Args:
        value: 状态码列表或逗号分隔的字符串。

    Returns:
        按输入顺序排列的 HTTP 错误码。

    Raises:
        TypeError: 输入不是列表或字符串。
        ValueError: 输入包含无效或受保护的错误码。
    """
    if isinstance(value, str):
        value = value.split(",") if value.strip() else []
    if not isinstance(value, (list, tuple)):
        raise TypeError("请输入逗号分隔的 HTTP 错误码")
    result = []
    for item in value:
        token = str(item).strip()
        numeric = len(token) == 3 and token.isascii() and token.isdecimal()
        if not numeric or not 400 <= int(token) <= 599:
            raise ValueError("HTTP 错误码必须是 400–599 的整数")
        code = int(token)
        if code in (400, 401, 403):
            raise ValueError("400、401、403 保留原校验策略，不支持换号")
        if code not in result:
            result.append(code)
    return result


def is_image_request(
    model_name: str,
    request_payload: dict[str, Any] | None = None,
) -> bool:
    """按模型及期望输出识别图片生成，不把带图输入的文本问答误判成生图。

    Args:
        model_name: 当前模型名。
        request_payload: Gemini 请求体。

    Returns:
        请求是否包含图片生成输出。
    """
    if "image" in (model_name or "").lower():
        return True
    payload = request_payload or {}
    generation_config = payload.get("generationConfig") or {}
    if not isinstance(generation_config, dict):
        return False
    modalities = generation_config.get("responseModalities") or generation_config.get(
        "response_modalities"
    )
    if isinstance(modalities, list) and any(
        str(modality).upper() == "IMAGE" for modality in modalities
    ):
        return True
    response_format = generation_config.get("responseFormat") or {}
    return bool(
        generation_config.get("imageConfig")
        or generation_config.get("image_config")
        or (isinstance(response_format, dict) and response_format.get("image"))
    )


def text_retry_decision(
    settings: dict[str, Any],
    status: int,
    *,
    image: bool,
) -> bool | None:
    """区分换号、直接返回和交由原策略处理三种结果。

    Args:
        settings: 本次请求开始时取得的配置快照。
        status: 上游实际返回的 HTTP 状态码。
        image: 是否为图片生成请求。

    Returns:
        True 表示允许尝试换号，False 表示直接返回，None 沿用原策略。
    """
    codes = settings.get("text_codes")
    if image or codes is None:
        return None
    if status in codes:
        return bool(settings["retry_enabled"])
    return False if status in DEFAULT_STATUS_CODES else None


def _account_email(data: dict[str, Any]) -> str:
    return str(data.get("user_email") or data.get("email") or "").strip().casefold()


async def alternate_account(
    manager: CredentialManager,
    mode: str,
    model: str,
    current: str,
    credential: dict[str, Any],
    delay: float,
) -> tuple[str, dict[str, Any]] | None:
    """从同一模型的可用池选择不同账号；无备选时保留原上游错误。

    Args:
        manager: 当前通道的凭证管理器。
        mode: 当前账号池。
        model: 本次发送的模型。
        current: 失败的凭证标识。
        credential: 失败账号的信息，用于排除同一邮箱的别名。
        delay: 选中备选账号后的重试间隔（秒）。

    Returns:
        可用备选凭证，无备选时返回 None。
    """
    excluded = {current}
    email = _account_email(credential)
    token = credential.get("access_token") or credential.get("token")
    while True:
        candidate = await manager.get_valid_credential(
            mode=mode, model_name=model, exclude_filenames=excluded.copy()
        )
        if not candidate or candidate[0] in excluded:
            return None
        name, data = candidate
        other = _account_email(data)
        same_identity = any(
            credential.get(key) and credential[key] == data.get(key)
            for key in ("refresh_token", "account_id")
        )
        if (
            (email and email == other)
            or same_identity
            or (token and token == (data.get("access_token") or data.get("token")))
        ):
            excluded.add(name)
            continue
        await asyncio.sleep(delay)
        return candidate
