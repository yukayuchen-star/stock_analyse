"""
清理过期文件 — 保证日常运行不积累冗余历史

清理范围：
  - cache/universe/<date>.json   保留近 KEEP_DAYS 天
  - output/<date>/                保留近 KEEP_DAYS 天目录
  - cache/market_data.db          删除已过期的 SQLite 行 + VACUUM 回收空间
  - 当日目录中的 .DS_Store        顺手清掉

保护清单（永不删）：
  - pool_history.jsonl            全局变更日志（审计/回放依赖）
  - 最新一份 output/<date>/stock_pool.json   启动加载 dynamic_pool 依赖
  - 当日目录                       今天刚生成的不动
"""
from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

from data.cache import SQLiteCache
from utils.time_utils import today_str


KEEP_DAYS = 7


def _is_date_name(name: str) -> bool:
    """目录名是否形如 YYYY-MM-DD。"""
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


def _too_old(name: str, cutoff: date) -> bool:
    try:
        return date.fromisoformat(name) < cutoff
    except ValueError:
        return False


def cleanup_old_files(keep_days: int = KEEP_DAYS) -> dict:
    """执行清理，返回 {universe_deleted, output_deleted, db_rows_deleted}。"""
    cutoff = date.today() - timedelta(days=keep_days)
    today  = today_str()
    stats  = {"universe_deleted": 0, "output_deleted": 0, "db_rows_deleted": 0}

    # 1. cache/universe/*.json
    udir = Path("cache") / "universe"
    if udir.exists():
        for f in udir.glob("*.json"):
            stem = f.stem  # "2026-05-17"
            if _too_old(stem, cutoff):
                f.unlink()
                stats["universe_deleted"] += 1

    # 2. output/<date>/ — 跳过今天 + 保护最新的 stock_pool.json 所在目录
    out_root = Path("output")
    if out_root.exists():
        date_dirs = sorted(
            [d for d in out_root.iterdir() if d.is_dir() and _is_date_name(d.name)]
        )
        # 找最新带 stock_pool.json 的目录，保护
        latest_with_snap = next(
            (d for d in reversed(date_dirs) if (d / "stock_pool.json").exists()),
            None,
        )
        for d in date_dirs:
            if d.name == today or d == latest_with_snap:
                continue
            if _too_old(d.name, cutoff):
                shutil.rmtree(d)
                stats["output_deleted"] += 1

    # 顺手清 output 根下的 .DS_Store
    ds = out_root / ".DS_Store"
    if ds.exists():
        ds.unlink()

    # 3. SQLite 过期行
    try:
        cache = SQLiteCache()
        stats["db_rows_deleted"] = cache.purge_expired()
    except Exception as e:
        logger.warning(f"[Cleanup] SQLite 清理失败: {e}")

    logger.info(
        f"[Cleanup] 保留 {keep_days} 天 | "
        f"universe -{stats['universe_deleted']} | "
        f"output -{stats['output_deleted']} | "
        f"db rows -{stats['db_rows_deleted']}"
    )
    return stats
