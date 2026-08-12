"""SQLite-only storage adapter used by the billing/account-proxy features."""

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Protocol

from log import log


class StorageBackend(Protocol):
    """存储后端协议"""

    async def initialize(self) -> None:
        """初始化存储后端"""
        ...

    async def close(self) -> None:
        """关闭存储后端"""
        ...

    # 凭证管理
    async def store_credential(self, filename: str, credential_data: Dict[str, Any], mode: str = "geminicli") -> bool:
        """存储凭证数据"""
        ...

    async def get_credential(self, filename: str, mode: str = "geminicli") -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        ...

    async def list_credentials(self, mode: str = "geminicli") -> List[str]:
        """列出所有凭证文件名"""
        ...

    async def delete_credential(self, filename: str, mode: str = "geminicli") -> bool:
        """删除凭证"""
        ...

    # 状态管理
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], mode: str = "geminicli") -> bool:
        """更新凭证状态"""
        ...

    async def get_credential_state(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        """获取凭证状态"""
        ...

    async def get_all_credential_states(self, mode: str = "geminicli") -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        ...

    async def mark_credential_selected(
        self,
        filename: str,
        *,
        mode: str = "antigravity",
        expected_call_count: Optional[int] = None,
    ) -> bool:
        """原子推进凭证公平调度计数。"""
        ...

    # 配置管理
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项"""
        ...

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        ...

    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        ...

    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        ...

    async def create_proxy_group(self, **kwargs) -> Dict[str, Any]: ...
    async def get_proxy_group(self, group_id: int) -> Dict[str, Any]: ...
    async def list_proxy_groups(self) -> List[Dict[str, Any]]: ...
    async def update_proxy_group(self, group_id: int, **updates) -> Dict[str, Any]: ...
    async def delete_proxy_group(self, group_id: int) -> bool: ...
    async def import_proxy_group_nodes(self, group_id: int, nodes: List[Dict[str, str]], replace: bool = False) -> Dict[str, Any]: ...
    async def select_proxy_group_node(self, group_id: int, advance: bool = True) -> Optional[Dict[str, Any]]: ...
    async def resolve_credential_network(self, filename: str, mode: str = "antigravity") -> Dict[str, Any]: ...
    async def batch_update_credential_network(self, filenames: List[str], **kwargs) -> Dict[str, Any]: ...
    async def get_antigravity_account_health(self, filename: str) -> Dict[str, Any]: ...
    async def update_antigravity_account_health(self, filename: str, **updates) -> Dict[str, Any]: ...
    async def invalidate_antigravity_proxy_binding(self, filename: str, cooldown_seconds: int = 300) -> bool: ...


class StorageAdapter:
    """固定使用 SQLite 的存储适配器。"""

    def __init__(self):
        self._backend: Optional["StorageBackend"] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """初始化存储适配器"""
        async with self._lock:
            if self._initialized:
                return

            # PostgreSQL/MongoDB 代码保留在仓库中，但新功能始终固定使用 SQLite。
            from .storage.sqlite_manager import SQLiteManager

            self._backend = SQLiteManager()
            await self._backend.initialize()
            log.info("Using SQLite storage backend (PostgreSQL/MongoDB settings ignored)")

            self._initialized = True

    async def close(self) -> None:
        """关闭存储适配器"""
        if self._backend:
            await self._backend.close()
            self._backend = None
            self._initialized = False

    def _ensure_initialized(self):
        """确保存储适配器已初始化"""
        if not self._initialized or not self._backend:
            raise RuntimeError("Storage adapter not initialized")

    # ============ 凭证管理 ============

    async def store_credential(self, filename: str, credential_data: Dict[str, Any], mode: str = "geminicli") -> bool:
        """存储凭证数据"""
        self._ensure_initialized()
        return await self._backend.store_credential(filename, credential_data, mode)

    async def get_credential(self, filename: str, mode: str = "geminicli") -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        self._ensure_initialized()
        return await self._backend.get_credential(filename, mode)

    async def list_credentials(self, mode: str = "geminicli") -> List[str]:
        """列出所有凭证文件名"""
        self._ensure_initialized()
        return await self._backend.list_credentials(mode)

    async def delete_credential(self, filename: str, mode: str = "geminicli") -> bool:
        """删除凭证"""
        self._ensure_initialized()
        return await self._backend.delete_credential(filename, mode)

    # ============ 状态管理 ============

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], mode: str = "geminicli") -> bool:
        """更新凭证状态"""
        self._ensure_initialized()
        return await self._backend.update_credential_state(filename, state_updates, mode)

    async def get_credential_state(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        """获取凭证状态"""
        self._ensure_initialized()
        return await self._backend.get_credential_state(filename, mode)

    async def get_all_credential_states(self, mode: str = "geminicli") -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        self._ensure_initialized()
        return await self._backend.get_all_credential_states(mode)

    async def mark_credential_selected(
        self,
        filename: str,
        *,
        mode: str = "antigravity",
        expected_call_count: Optional[int] = None,
    ) -> bool:
        self._ensure_initialized()
        return await self._backend.mark_credential_selected(
            filename,
            mode=mode,
            expected_call_count=expected_call_count,
        )

    async def get_credential_network(self, filename: str, mode: str = "antigravity") -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_credential_network(filename, mode)

    async def update_credential_network(
        self,
        filename: str,
        proxy_mode: str = "inherit",
        proxy_url: Optional[str] = None,
        mode: str = "antigravity",
        proxy_group_id: Optional[int] = None,
    ) -> bool:
        self._ensure_initialized()
        return await self._backend.update_credential_network(
            filename, proxy_mode, proxy_url, mode, proxy_group_id
        )

    async def resolve_credential_network(self, filename: str, mode: str = "antigravity") -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.resolve_credential_network(filename, mode)

    async def batch_update_credential_network(self, filenames: List[str], **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.batch_update_credential_network(filenames, **kwargs)

    async def get_antigravity_account_health(self, filename: str) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_antigravity_account_health(filename)

    async def update_antigravity_account_health(
        self, filename: str, **updates: Any
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.update_antigravity_account_health(filename, **updates)

    async def invalidate_antigravity_proxy_binding(
        self, filename: str, cooldown_seconds: int = 300
    ) -> bool:
        self._ensure_initialized()
        return await self._backend.invalidate_antigravity_proxy_binding(
            filename, cooldown_seconds
        )

    async def create_proxy_group(self, **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.create_proxy_group(**kwargs)

    async def get_proxy_group(self, group_id: int) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_proxy_group(group_id)

    async def list_proxy_groups(self) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.list_proxy_groups()

    async def update_proxy_group(self, group_id: int, **updates) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.update_proxy_group(group_id, **updates)

    async def delete_proxy_group(self, group_id: int) -> bool:
        self._ensure_initialized()
        return await self._backend.delete_proxy_group(group_id)

    async def import_proxy_group_nodes(
        self, group_id: int, nodes: List[Dict[str, str]], replace: bool = False
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.import_proxy_group_nodes(group_id, nodes, replace)

    async def select_proxy_group_node(
        self, group_id: int, advance: bool = True
    ) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.select_proxy_group_node(group_id, advance=advance)

    async def create_api_key_record(self, **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.create_api_key_record(**kwargs)

    async def get_api_key_record(
        self, api_key_id: str, *, include_secret: bool = False
    ) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.get_api_key_record(
            api_key_id, include_secret=include_secret
        )

    async def get_api_key_by_prefix(self, key_prefix: str) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.get_api_key_by_prefix(key_prefix)

    async def list_api_key_records(self, **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.list_api_key_records(**kwargs)

    async def update_api_key_record(self, api_key_id: str, **updates) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.update_api_key_record(api_key_id, **updates)

    async def revoke_api_key(self, api_key_id: str) -> bool:
        self._ensure_initialized()
        return await self._backend.revoke_api_key(api_key_id)

    async def rollover_api_key_quota(self, api_key_id: str, **kwargs) -> bool:
        self._ensure_initialized()
        return await self._backend.rollover_api_key_quota(api_key_id, **kwargs)

    async def rollover_all_api_key_quotas(self, **kwargs) -> int:
        self._ensure_initialized()
        return await self._backend.rollover_all_api_key_quotas(**kwargs)

    async def reset_api_key_quota(self, api_key_id: str, **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.reset_api_key_quota(api_key_id, **kwargs)

    async def set_api_key_quota_used(self, api_key_id: str, amount: str) -> None:
        self._ensure_initialized()
        await self._backend.set_api_key_quota_used(api_key_id, amount)

    async def record_billing_usage(self, **kwargs) -> bool:
        self._ensure_initialized()
        return await self._backend.record_billing_usage(**kwargs)

    async def upsert_billing_price(self, **kwargs) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.upsert_billing_price(**kwargs)

    async def list_billing_prices(self) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.list_billing_prices()

    async def delete_billing_price(self, model: str) -> bool:
        self._ensure_initialized()
        return await self._backend.delete_billing_price(model)

    async def get_billing_summary(
        self, range_name: str = "today", api_key_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_billing_summary(range_name, api_key_id=api_key_id)

    async def get_billing_accounts(
        self, range_name: str = "today", page: int = 1, page_size: int = 50,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_billing_accounts(
            range_name, page, page_size, api_key_id=api_key_id
        )

    async def get_billing_models(
        self, range_name: str = "today", page: int = 1, page_size: int = 50,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_billing_models(
            range_name, page, page_size, api_key_id=api_key_id
        )

    async def get_billing_keys(
        self, range_name: str = "today", page: int = 1, page_size: int = 50
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._backend.get_billing_keys(range_name, page, page_size)

    async def list_billing_daily(
        self, days: int = 45, page: int = 1, page_size: int = 500
    ) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.list_billing_daily(days, page, page_size)

    async def list_billing_daily_for_reprice(
        self, model: Optional[str], page: int = 1, page_size: int = 500
    ) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        return await self._backend.list_billing_daily_for_reprice(model, page, page_size)

    async def update_billing_daily_costs(self, **kwargs) -> bool:
        self._ensure_initialized()
        return await self._backend.update_billing_daily_costs(**kwargs)

    async def rebuild_api_key_quotas(self) -> int:
        self._ensure_initialized()
        return await self._backend.rebuild_api_key_quotas()

    # ============ 配置管理 ============

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项"""
        self._ensure_initialized()
        return await self._backend.set_config(key, value)

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        self._ensure_initialized()
        return await self._backend.get_config(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        self._ensure_initialized()
        return await self._backend.get_all_config()

    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        self._ensure_initialized()
        return await self._backend.delete_config(key)

    # ============ 工具方法 ============

    async def export_credential_to_json(self, filename: str, output_path: str = None) -> bool:
        """将凭证导出为JSON文件"""
        self._ensure_initialized()
        if hasattr(self._backend, "export_credential_to_json"):
            return await self._backend.export_credential_to_json(filename, output_path)
        # MongoDB后端的fallback实现
        credential_data = await self.get_credential(filename)
        if credential_data is None:
            return False

        if output_path is None:
            output_path = f"{filename}.json"

        import aiofiles

        try:
            async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(credential_data, indent=2, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def import_credential_from_json(self, json_path: str, filename: str = None) -> bool:
        """从JSON文件导入凭证"""
        self._ensure_initialized()
        if hasattr(self._backend, "import_credential_from_json"):
            return await self._backend.import_credential_from_json(json_path, filename)
        # MongoDB后端的fallback实现
        try:
            import aiofiles

            async with aiofiles.open(json_path, "r", encoding="utf-8") as f:
                content = await f.read()

            credential_data = json.loads(content)

            if filename is None:
                filename = os.path.basename(json_path)

            return await self.store_credential(filename, credential_data)
        except Exception:
            return False

    def get_backend_type(self) -> str:
        """获取当前存储后端类型"""
        if not self._backend:
            return "none"

        # 检查后端类型
        backend_class_name = self._backend.__class__.__name__
        if "SQLite" in backend_class_name or "sqlite" in backend_class_name.lower():
            return "sqlite"
        elif "MongoDB" in backend_class_name or "mongo" in backend_class_name.lower():
            return "mongodb"
        elif "PSQL" in backend_class_name or "Postgres" in backend_class_name or "psql" in backend_class_name.lower():
            return "postgresql"
        else:
            return "unknown"

    async def get_backend_info(self) -> Dict[str, Any]:
        """获取存储后端信息"""
        self._ensure_initialized()

        backend_type = self.get_backend_type()
        info = {"backend_type": backend_type, "initialized": self._initialized}

        # 获取底层存储信息
        if hasattr(self._backend, "get_database_info"):
            try:
                db_info = await self._backend.get_database_info()
                info.update(db_info)
            except Exception as e:
                info["database_error"] = str(e)
        else:
            backend_type = self.get_backend_type()
            if backend_type == "sqlite":
                info.update(
                    {
                        "database_path": getattr(self._backend, "_db_path", None),
                        "credentials_dir": getattr(self._backend, "_credentials_dir", None),
                    }
                )
            elif backend_type == "mongodb":
                info.update(
                    {
                        "database_name": getattr(self._backend, "_db", {}).name if hasattr(self._backend, "_db") else None,
                    }
                )
            elif backend_type == "postgresql":
                info.update(
                    {
                        "dsn": getattr(self._backend, "_dsn", None),
                    }
                )

        return info


# 全局存储适配器实例
_storage_adapter: Optional[StorageAdapter] = None


async def get_storage_adapter() -> StorageAdapter:
    """获取全局存储适配器实例"""
    global _storage_adapter

    if _storage_adapter is None:
        _storage_adapter = StorageAdapter()
        await _storage_adapter.initialize()

    return _storage_adapter


async def close_storage_adapter():
    """关闭全局存储适配器"""
    global _storage_adapter

    if _storage_adapter:
        await _storage_adapter.close()
        _storage_adapter = None
