"""
股票池持久化 — core_pool 与 dynamic_pool 的加载、保存、变更日志

布局：
  output/<date>/stock_pool.json    当日最终池快照（core + dynamic + buckets + 决策摘要）
  pool_history.jsonl               全局变更日志，每行一条 {date, action, ticker, reason, score}

设计原则：
  - core_pool（=config.stocks.STOCK_POOL）永不被自动移除，仅在快照中标注
  - dynamic_pool 由筛选 + 用户确认逐日演化
  - 加载 dynamic_pool：从最近一份 stock_pool.json 读取，不存在则返回空
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from config.settings import settings


_HISTORY_FILE = Path("pool_history.jsonl")
_OUTPUT_ROOT  = Path(settings.output_dir)


# ── 加载 ────────────────────────────────────────────────────────

def _latest_snapshot_file() -> Optional[Path]:
    if not _OUTPUT_ROOT.exists():
        return None
    snapshots = sorted(_OUTPUT_ROOT.glob("*/stock_pool.json"))
    return snapshots[-1] if snapshots else None


def load_dynamic_pool() -> List[str]:
    """从最近一份 stock_pool.json 读取 dynamic_pool；无快照时返回空列表。"""
    f = _latest_snapshot_file()
    if f is None:
        logger.info("[Pool] 无历史快照，dynamic_pool 初始化为空")
        return []
    try:
        snap = json.loads(f.read_text())
        dyn  = snap.get("dynamic_pool", [])
        logger.info(f"[Pool] 加载 dynamic_pool ({len(dyn)} 只) 来自 {f}")
        return dyn
    except Exception as e:
        logger.warning(f"[Pool] 读取快照失败 {f}: {e}")
        return []


# ── 保存 ────────────────────────────────────────────────────────

def save_pool_snapshot(
    date_str:     str,
    core_pool:    List[str],
    dynamic_pool: List[str],
    buckets:      Dict[str, List[str]],
    decisions:    Optional[Dict[str, dict]] = None,
) -> Path:
    """写入 output/<date>/stock_pool.json。"""
    out_dir = _OUTPUT_ROOT / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "date":          date_str,
        "core_pool":     core_pool,
        "dynamic_pool":  dynamic_pool,
        "final_pool":    sorted(set(core_pool) | set(dynamic_pool)),
        "buckets":       buckets,
        "decisions":     decisions or {},
        "saved_at":      datetime.now().isoformat(timespec="seconds"),
    }
    f = out_dir / "stock_pool.json"
    f.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    logger.info(f"[Pool] 快照已保存: {f}")
    return f


# ── 变更日志 ────────────────────────────────────────────────────

@dataclass
class PoolChange:
    date:    str
    action:  str    # "add" | "remove"
    ticker:  str
    reason:  str
    score:   Optional[float] = None
    source:  str = "user"   # "user" | "auto-screen"


def append_pool_changes(changes: List[PoolChange]) -> None:
    """追加多条变更到 pool_history.jsonl。"""
    if not changes:
        return
    with _HISTORY_FILE.open("a", encoding="utf-8") as f:
        for ch in changes:
            f.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")
    logger.info(f"[Pool] 写入 {len(changes)} 条变更日志 → {_HISTORY_FILE}")
