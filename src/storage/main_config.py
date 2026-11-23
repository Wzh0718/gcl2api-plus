"""Main store selection shared by the server and synchronous workers."""
import os


def main_backend():
    explicit = os.getenv("MAIN_STORAGE_BACKEND", "").strip().lower()
    dsns = {
        "postgresql": os.getenv("MAIN_POSTGRES_DSN", "").strip(),
        "mysql": os.getenv("MAIN_MYSQL_DSN", "").strip(),
    }
    if explicit == "postgres":
        explicit = "postgresql"
    if explicit == "sqlite":
        return explicit
    if explicit:
        if explicit not in dsns:
            raise RuntimeError("MAIN_STORAGE_BACKEND 仅支持 sqlite、postgresql 或 mysql")
        if not dsns[explicit]:
            setting = "MAIN_POSTGRES_DSN" if explicit == "postgresql" else "MAIN_MYSQL_DSN"
            raise RuntimeError(f"选择 {explicit} 主库时必须配置 {setting}")
        return explicit
    configured = [name for name, dsn in dsns.items() if dsn]
    if len(configured) > 1:
        raise RuntimeError("配置了多个主库 DSN，请显式选择 MAIN_STORAGE_BACKEND")
    return configured[0] if configured else "sqlite"


def external_connection():
    if main_backend() == "mysql":
        from src.storage.main_mysql import sync_connection
    else:
        from src.storage.main_postgres import sync_connection
    return sync_connection()
