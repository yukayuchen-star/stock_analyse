import hashlib
import sqlite3
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
from loguru import logger


class SQLiteCache:
    """DataFrame 缓存，存入 SQLite，支持 TTL 过期。"""

    def __init__(self, cache_dir: str = "cache") -> None:
        Path(cache_dir).mkdir(exist_ok=True)
        self.db_path = str(Path(cache_dir) / "market_data.db")
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key       TEXT PRIMARY KEY,
                    data      TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    ttl_hours INTEGER NOT NULL
                )
            """)

    # ── 读 ────────────────────────────────────────────────

    def get(self, key: str) -> pd.DataFrame | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT data, cached_at, ttl_hours FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        data, cached_at, ttl_hours = row
        expires_at = datetime.fromisoformat(cached_at) + timedelta(hours=ttl_hours)
        if datetime.now() > expires_at:
            logger.debug(f"Cache expired: {key[:16]}…")
            return None
        try:
            return pd.read_json(StringIO(data), orient="table")
        except Exception:
            return pd.read_json(StringIO(data))

    # ── 写 ────────────────────────────────────────────────

    def set(self, key: str, df: pd.DataFrame, ttl_hours: int = 24) -> None:
        if df.empty:
            return  # 不缓存空结果，避免掩盖 API 错误
        try:
            data = df.to_json(orient="table", date_format="iso")
        except Exception:
            data = df.to_json()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, data, cached_at, ttl_hours) VALUES (?,?,?,?)",
                (key, data, datetime.now().isoformat(), ttl_hours),
            )

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def make_key(*parts: object) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.md5(raw.encode()).hexdigest()

    def invalidate(self, key: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cache WHERE key = ?", (key,))

    def purge_expired(self) -> int:
        """删除已过期的缓存行并 VACUUM 回收磁盘空间，返回删除条数。"""
        now_iso = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM cache "
                "WHERE datetime(cached_at, '+' || ttl_hours || ' hours') < ?",
                (now_iso,),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.execute("VACUUM")
        return deleted
