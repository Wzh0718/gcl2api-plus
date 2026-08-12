"""SQLite-backed downstream API-key lifecycle and quota validation."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken


SHANGHAI = ZoneInfo("Asia/Shanghai")
MONEY_QUANTUM = Decimal("0.00000001")


class ApiKeyAuthError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ApiKeyPrincipal:
    api_key_id: str
    name: str
    kind: str
    key_prefix: str


def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ApiKeyCipher:
    """Encrypt retrievable API keys with a local, persistent master key."""

    KEY_FILENAME = "api_keys.fernet.key"

    def __init__(self, credentials_dir: Optional[str] = None):
        directory = Path(credentials_dir or os.getenv("CREDENTIALS_DIR", "./creds"))
        directory.mkdir(parents=True, exist_ok=True)
        self.key_path = directory / self.KEY_FILENAME
        self._fernet = Fernet(self._load_or_create_key(directory))

    def _read_key(self) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.key_path, flags)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise RuntimeError("密钥加密主密钥文件类型无效")
            key = os.read(fd, 256).strip()
        finally:
            os.close(fd)
        if not key:
            raise RuntimeError("密钥加密主密钥文件为空")
        try:
            os.chmod(self.key_path, 0o600, follow_symlinks=False)
        except (NotImplementedError, OSError):
            pass
        return key

    def _load_or_create_key(self, directory: Path) -> bytes:
        try:
            return self._read_key()
        except FileNotFoundError:
            pass

        generated = Fernet.generate_key()
        temp_path = directory / f".{self.KEY_FILENAME}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, generated + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            try:
                os.link(temp_path, self.key_path, follow_symlinks=False)
            except FileExistsError:
                pass
        finally:
            temp_path.unlink(missing_ok=True)
        return self._read_key()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("密钥密文无法解密，请重新创建密钥") from exc


def _now(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI)


def _month_start(value: datetime) -> str:
    return value.date().replace(day=1).isoformat()


def _next_month_start(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return value.replace(month=value.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _money(value: Any, *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("额度必须是有效金额") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("额度必须是非负有限金额")
    return f"{amount.quantize(MONEY_QUANTUM):.8f}"


def _expires_at(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("过期时间必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.timestamp()


class ApiKeyService:
    def __init__(self, storage: Any):
        self.storage = storage
        self._cipher: Optional[ApiKeyCipher] = None

    @property
    def cipher(self) -> ApiKeyCipher:
        # Authentication and list operations only need hashes/availability.
        # Load the master key only for create or explicit secret retrieval.
        if self._cipher is None:
            self._cipher = ApiKeyCipher()
        return self._cipher

    @property
    def currency(self) -> str:
        return os.getenv("BILLING_CURRENCY", "CNY") or "CNY"

    def _public(self, record: dict[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
        current = _now(now)
        result = dict(record)
        ciphertext = result.pop("secret_ciphertext", None)
        result["secret_available"] = bool(
            result.get("secret_available", ciphertext)
        )
        result.pop("secret_hash", None)
        quota_amount = _money(result.get("quota_amount"), allow_none=True)
        quota_used = _money(result.get("quota_used")) or "0.00000000"
        result["quota_amount"] = quota_amount
        result["quota_used"] = quota_used
        result["quota_tokens_used"] = max(
            int(result.get("quota_tokens_used") or 0), 0
        )
        if quota_amount is None:
            result["quota_remaining"] = None
        else:
            remaining = max(Decimal(quota_amount) - Decimal(quota_used), Decimal("0"))
            result["quota_remaining"] = f"{remaining.quantize(MONEY_QUANTUM):.8f}"
        expires_at = result.get("expires_at")
        if result.get("status") == "revoked":
            effective_status = "revoked"
        elif result.get("status") == "disabled":
            effective_status = "disabled"
        elif expires_at is not None and float(expires_at) <= current.timestamp():
            effective_status = "expired"
        elif quota_amount is not None and Decimal(quota_used) >= Decimal(quota_amount):
            effective_status = "quota_exhausted"
        else:
            effective_status = "active"
        result["effective_status"] = effective_status
        result["next_reset_at"] = (
            _next_month_start(current).isoformat()
            if result.get("kind") == "managed" and result.get("reset_mode") == "monthly"
            else None
        )
        return result

    async def create_key(
        self,
        *,
        name: str,
        quota_amount: Any = None,
        reset_mode: str = "manual",
        expires_at: Any = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name or len(clean_name) > 100:
            raise ValueError("密钥名称长度必须为 1-100 个字符")
        if reset_mode not in {"manual", "monthly"}:
            raise ValueError("reset_mode 必须是 manual 或 monthly")
        current = _now(now)
        normalized_quota = _money(quota_amount, allow_none=True)
        normalized_expiry = _expires_at(expires_at)
        for _ in range(5):
            key_prefix = secrets.token_hex(4)
            api_key = f"gcli_{key_prefix}_{secrets.token_urlsafe(32)}"
            try:
                record = await self.storage.create_api_key_record(
                    id=uuid.uuid4().hex,
                    name=clean_name,
                    key_prefix=key_prefix,
                    secret_hash=hash_api_key(api_key),
                    secret_ciphertext=self.cipher.encrypt(api_key),
                    quota_amount=normalized_quota,
                    currency=self.currency,
                    reset_mode=reset_mode,
                    quota_period_start=_month_start(current) if reset_mode == "monthly" else current.date().isoformat(),
                    expires_at=normalized_expiry,
                    now=current.timestamp(),
                )
                return {"item": self._public(record, current), "api_key": api_key}
            except sqlite3.IntegrityError as exc:
                if "key_prefix" in str(exc) or "secret_hash" in str(exc):
                    continue
                if "name" in str(exc):
                    raise ValueError("密钥名称已存在") from exc
                raise
        raise RuntimeError("无法生成唯一密钥前缀")

    async def get_secret(self, api_key_id: str) -> str:
        record = await self.storage.get_api_key_record(
            api_key_id, include_secret=True
        )
        if record is None:
            raise KeyError(api_key_id)
        if record.get("kind") != "managed":
            raise ValueError("环境配置密钥不由密钥分发管理保存")
        if record.get("status") == "revoked":
            raise ValueError("已吊销密钥不能复制")
        ciphertext = record.get("secret_ciphertext")
        if not ciphertext:
            raise ValueError("旧密钥没有可恢复明文，请重新创建密钥")
        api_key = self.cipher.decrypt(str(ciphertext))
        if not hmac.compare_digest(
            str(record.get("secret_hash") or ""), hash_api_key(api_key)
        ):
            raise ValueError("密钥密文校验失败，请重新创建密钥")
        return api_key

    async def list_keys(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        current = _now(now)
        await self.storage.rollover_all_api_key_quotas(
            month_start=_month_start(current), now=current.timestamp()
        )
        result = await self.storage.list_api_key_records(
            page=page, page_size=page_size, status=status
        )
        result["items"] = [self._public(item, current) for item in result["items"]]
        return result

    async def get_key(self, api_key_id: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
        current = _now(now)
        await self.storage.rollover_api_key_quota(
            api_key_id, month_start=_month_start(current), now=current.timestamp()
        )
        record = await self.storage.get_api_key_record(api_key_id)
        if record is None:
            raise KeyError(api_key_id)
        return self._public(record, current)

    async def update_key(
        self, api_key_id: str, *, now: Optional[datetime] = None, **updates: Any
    ) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        current = _now(now)
        if "name" in updates:
            name = str(updates["name"] or "").strip()
            if not name or len(name) > 100:
                raise ValueError("密钥名称长度必须为 1-100 个字符")
            normalized["name"] = name
        if "status" in updates:
            if updates["status"] not in {"active", "disabled"}:
                raise ValueError("status 只能是 active 或 disabled")
            normalized["status"] = updates["status"]
        if "quota_amount" in updates:
            normalized["quota_amount"] = _money(updates["quota_amount"], allow_none=True)
            normalized["currency"] = self.currency
        if "reset_mode" in updates:
            if updates["reset_mode"] not in {"manual", "monthly"}:
                raise ValueError("reset_mode 必须是 manual 或 monthly")
            normalized["reset_mode"] = updates["reset_mode"]
            normalized["quota_period_start"] = (
                _month_start(current)
                if updates["reset_mode"] == "monthly"
                else current.date().isoformat()
            )
        if "expires_at" in updates:
            normalized["expires_at"] = _expires_at(updates["expires_at"])
        try:
            record = await self.storage.update_api_key_record(api_key_id, **normalized)
        except sqlite3.IntegrityError as exc:
            raise ValueError("密钥名称已存在") from exc
        return self._public(record, current)

    async def revoke_key(self, api_key_id: str) -> bool:
        return await self.storage.revoke_api_key(api_key_id)

    async def reset_quota(
        self, api_key_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        current = _now(now)
        record = await self.storage.reset_api_key_quota(
            api_key_id,
            period_start=current.date().isoformat(),
            now=current.timestamp(),
        )
        return self._public(record, current)

    async def authenticate(
        self, api_key: str, *, now: Optional[datetime] = None
    ) -> ApiKeyPrincipal:
        current = _now(now)
        parts = str(api_key or "").split("_", 2)
        if len(parts) != 3 or parts[0] != "gcli" or len(parts[1]) != 8 or not parts[2]:
            raise ApiKeyAuthError(403, "密钥错误")
        record = await self.storage.get_api_key_by_prefix(parts[1])
        if record is None or not hmac.compare_digest(
            str(record.get("secret_hash") or ""), hash_api_key(api_key)
        ):
            raise ApiKeyAuthError(403, "密钥错误")
        await self.storage.rollover_api_key_quota(
            record["id"], month_start=_month_start(current), now=current.timestamp()
        )
        record = await self.storage.get_api_key_record(record["id"])
        if record is None:
            raise ApiKeyAuthError(403, "密钥错误")
        if record["status"] in {"disabled", "revoked"}:
            raise ApiKeyAuthError(403, "密钥已停用或吊销")
        if record.get("expires_at") is not None and float(record["expires_at"]) <= current.timestamp():
            raise ApiKeyAuthError(403, "密钥已过期")
        quota_amount = _money(record.get("quota_amount"), allow_none=True)
        quota_used = _money(record.get("quota_used")) or "0.00000000"
        if quota_amount is not None and Decimal(quota_used) >= Decimal(quota_amount):
            raise ApiKeyAuthError(429, "密钥额度已耗尽")
        return ApiKeyPrincipal(
            api_key_id=record["id"],
            name=record["name"],
            kind=record["kind"],
            key_prefix=record["key_prefix"],
        )
