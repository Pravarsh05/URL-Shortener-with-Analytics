import asyncio
import re
import sqlite3
import logging
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, List, Optional, Sequence
import asyncpg

logger = logging.getLogger("sqlite_fallback")

class SQLiteRecord(dict):
    """
    Simulates asyncpg.Record for SQLite row results.
    Supports dictionary access r['field'], indexing r[0], and datetime coercion.
    """
    def __init__(self, data: dict):
        super().__init__(data)
        self._raw = data
        for k, v in list(data.items()):
            if isinstance(v, str) and (k in ("created_at", "expires_at", "ts") or "time" in k):
                try:
                    dt = datetime.fromisoformat(v.replace(" ", "T"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self[k] = dt
                except Exception:
                    pass
            elif k in ("is_custom", "is_active") and isinstance(v, int):
                self[k] = bool(v)
            elif k == "day" and isinstance(v, str):
                try:
                    self[k] = datetime.strptime(v, "%Y-%m-%d").date()
                except Exception:
                    pass

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return super().get(key, default)


class SQLiteTransaction:
    def __init__(self, conn: "SQLiteConnection"):
        self.conn = conn

    async def __aenter__(self):
        await self.conn.execute("BEGIN TRANSACTION;")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self.conn.execute("ROLLBACK;")
        else:
            await self.conn.execute("COMMIT;")


class SQLiteConnection:
    def __init__(self, sync_conn: sqlite3.Connection, lock: asyncio.Lock):
        self._conn = sync_conn
        self._lock = lock

    def _transform_query_and_args(self, query: str, args: Sequence[Any]) -> tuple[str, list[Any]]:
        clean_query = query.strip()

        # Handle Postgres sequence nextval
        if "nextval('links_id_seq')" in clean_query:
            clean_query = "SELECT COALESCE(MAX(id), 0) + 1 FROM links;"
            return clean_query, []

        # Replace Postgres $1, $2 with SQLite ?
        clean_query = re.sub(r"\$\d+", "?", clean_query)

        # Replace now() with CURRENT_TIMESTAMP
        clean_query = re.sub(r"\bnow\(\)", "CURRENT_TIMESTAMP", clean_query, flags=re.IGNORECASE)

        # Convert datetime/date/bool args to SQLite-friendly values
        clean_args = []
        for arg in args:
            if isinstance(arg, datetime):
                clean_args.append(arg.isoformat())
            elif isinstance(arg, date):
                clean_args.append(arg.isoformat())
            elif isinstance(arg, bool):
                clean_args.append(1 if arg else 0)
            else:
                clean_args.append(arg)

        return clean_query, clean_args

    def transaction(self) -> SQLiteTransaction:
        return SQLiteTransaction(self)

    async def fetchrow(self, query: str, *args) -> Optional[SQLiteRecord]:
        q, a = self._transform_query_and_args(query, args)
        async with self._lock:
            try:
                def _run():
                    cur = self._conn.cursor()
                    cur.execute(q, a)
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return SQLiteRecord(dict(row))
                return await asyncio.to_thread(_run)
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e).upper():
                    raise asyncpg.exceptions.UniqueViolationError(str(e))
                raise

    async def fetch(self, query: str, *args) -> List[SQLiteRecord]:
        q, a = self._transform_query_and_args(query, args)
        async with self._lock:
            try:
                def _run():
                    cur = self._conn.cursor()
                    cur.execute(q, a)
                    return [SQLiteRecord(dict(row)) for row in cur.fetchall()]
                return await asyncio.to_thread(_run)
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e).upper():
                    raise asyncpg.exceptions.UniqueViolationError(str(e))
                raise

    async def fetchval(self, query: str, *args) -> Any:
        q, a = self._transform_query_and_args(query, args)
        async with self._lock:
            try:
                def _run():
                    cur = self._conn.cursor()
                    cur.execute(q, a)
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return row[0]
                return await asyncio.to_thread(_run)
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e).upper():
                    raise asyncpg.exceptions.UniqueViolationError(str(e))
                raise

    async def execute(self, query: str, *args) -> None:
        q, a = self._transform_query_and_args(query, args)
        async with self._lock:
            try:
                def _run():
                    cur = self._conn.cursor()
                    cur.execute(q, a)
                    self._conn.commit()
                await asyncio.to_thread(_run)
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e).upper():
                    raise asyncpg.exceptions.UniqueViolationError(str(e))
                raise

    async def executemany(self, query: str, seq_of_args: Sequence[Sequence[Any]]) -> None:
        if not seq_of_args:
            return
        q, _ = self._transform_query_and_args(query, seq_of_args[0])
        clean_seq = []
        for args in seq_of_args:
            _, a = self._transform_query_and_args(query, args)
            clean_seq.append(a)

        async with self._lock:
            try:
                def _run():
                    cur = self._conn.cursor()
                    cur.executemany(q, clean_seq)
                    self._conn.commit()
                await asyncio.to_thread(_run)
            except sqlite3.IntegrityError as e:
                if "UNIQUE" in str(e).upper():
                    raise asyncpg.exceptions.UniqueViolationError(str(e))
                raise


class SQLiteAcquireContext:
    def __init__(self, conn: SQLiteConnection):
        self.conn = conn

    async def __aenter__(self) -> SQLiteConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class SQLitePool:
    """
    A drop-in replacement for asyncpg.Pool running on SQLite.
    """
    def __init__(self, db_path: str = "local_pulselink.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()
        self._wrapper: Optional[SQLiteConnection] = None

    def init_schema(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._wrapper = SQLiteConnection(self._conn, self._lock)

        cur = self._conn.cursor()
        cur.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            long_url TEXT NOT NULL,
            is_custom INTEGER DEFAULT 0,
            user_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_links_code ON links(code);
        CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_links_expiry ON links(expires_at);

        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            ip_hash TEXT,
            country TEXT DEFAULT 'XX',
            device TEXT DEFAULT 'desktop',
            browser TEXT DEFAULT 'Other',
            referrer TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_clicks_link_ts ON clicks(link_id, ts);

        CREATE TABLE IF NOT EXISTS click_daily (
            link_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            clicks INTEGER NOT NULL DEFAULT 0,
            unique_visitors INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (link_id, day)
        );
        """)
        self._conn.commit()
        logger.info("Local SQLite database schema initialized at %s", self.db_path)

    def acquire(self) -> SQLiteAcquireContext:
        if self._wrapper is None:
            self.init_schema()
        return SQLiteAcquireContext(self._wrapper)

    async def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._wrapper = None
