"""统一 SQLite 连接参数：压低写放大与锁等待。

背景（2026-11-20 生产实测，43.110.150.44）：

- 单库 3.8 GB，``call_records`` 459 万行（30 天保留期）、5 个索引合计 1.18 GB；
- 每个请求收尾要写 5 次独立事务（account_usage / credentials / billing /
  call_records INSERT / call_records UPDATE），默认 ``synchronous=FULL``
  意味着每次 commit 都要 fsync WAL + 同步；
- 单写锁在高并发下串行排队，收尾阶段 p50 17.8s，占端到端总时长 43%。

这里只做**连接级**参数统一，不改任何业务语义：

- ``synchronous=NORMAL``：WAL 模式下 SQLite 官方推荐值。崩溃只会丢掉最后一次
  checkpoint 之后提交的事务，数据库文件不会损坏；换来 commit 不再每次 fsync。
- ``wal_autocheckpoint``：默认 1000 页（~4MB）就 checkpoint 一次，在持续写入下
  制造周期性停顿；调到 20000 页（~78MB）把频率降下来。
- ``cache_size``：保持 8MB。这里的连接几乎都是「一次请求一个短连接」，
  连接关闭后页缓存全部作废，调大只是按并发数放大器内存（185 并发 × 64MB
  就是 11.8GB，而机器只有 7GB）。8MB 足够覆盖单次请求的索引定位。
- ``temp_store=MEMORY``：聚合查询的临时 B-tree 不走磁盘。

``journal_mode=WAL`` 是持久属性（写在库头），仍在初始化时设置一次。
所有 PRAGMA 都是连接级的，因此必须经由本模块的 :func:`connect` 建立连接，
在服务器上手工执行 ``PRAGMA`` 不会对应用生效，容器重启即失效。
"""

from __future__ import annotations

import contextlib
import os

import aiosqlite

BUSY_TIMEOUT_MS = 5000
# ~78 MB 的 WAL 阈值；4 MB 默认值在高写入下会频繁 checkpoint。
WAL_AUTOCHECKPOINT_PAGES = 20000
# 负数表示 KiB，即 8 MB。连接是短命的，缓存无法跨请求复用，
# 上限过高只会按并发数放大内存占用。
CACHE_SIZE_KB = 8192


async def apply_pragmas(db: aiosqlite.Connection) -> None:
    """Apply the connection-level settings shared by every SQLite caller."""
    await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
    await db.execute(f"PRAGMA cache_size=-{CACHE_SIZE_KB}")
    await db.execute("PRAGMA temp_store=MEMORY")


@contextlib.asynccontextmanager
async def connect(path: str, *, row_factory=None, isolation_level=None):
    """Open an aiosqlite connection with the shared pragmas applied.

    Mirrors ``aiosqlite.connect`` semantics closely enough for the existing
    ``async with ... as db`` call sites; only the pragmas are added.
    """
    from src.storage.main_postgres import PostgresDatabase
    if isinstance(path, PostgresDatabase):
        async with path.connect() as db:
            db.row_factory = row_factory
            yield db
        return
    async with aiosqlite.connect(path, isolation_level=isolation_level) as db:
        if row_factory is not None:
            db.row_factory = row_factory
        await apply_pragmas(db)
        yield db


def apply_sync_pragmas(connection) -> None:
    """Same settings for the blocking ``sqlite3`` connections (worker threads)."""
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(f"PRAGMA wal_autocheckpoint={WAL_AUTOCHECKPOINT_PAGES}")
    connection.execute(f"PRAGMA cache_size=-{CACHE_SIZE_KB}")


def connect_timeout_seconds() -> float:
    return BUSY_TIMEOUT_MS / 1000.0


def is_enabled() -> bool:
    """Escape hatch: set SQLITE_PRAGMA_TUNING=0 to fall back to sqlite defaults."""
    return os.getenv("SQLITE_PRAGMA_TUNING", "1").strip().lower() not in {"0", "false", "off", "no"}
