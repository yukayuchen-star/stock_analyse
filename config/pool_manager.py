"""
股票池持久化 — core_pool 与 dynamic_pool 的加载、保存、变更日志

布局：
  output/<date>/stock_pool.json    当日最终池快照（core + dynamic + buckets + 决策摘要）
  pool_history.jsonl               全局变更日志，每行一条 {date, action, ticker, reason, score}

设计原则：
  - core_pool（=config.stocks.STOCK_POOL）永不被自动移除，仅在快照中标注
  - dynamic_pool 由筛选 + 用户确认逐日演化
  - 加载 dynamic_pool：从最近一份 stock_pool.json 读取，不存在则返回空
  - forced_held（R9.10）：仅为风控被强制留池的 paper 持仓票，快照中**单列一栏**，
    不混进 core_pool——它们是被扫描池刷下来的票，不是战略核心池成员
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from config.settings import settings


_HISTORY_FILE      = Path("pool_history.jsonl")
_OUTPUT_ROOT       = Path(settings.output_dir)
_WATCHLIST_US_FILE = Path("watchlist_us.txt")


def load_us_watchlist(path: Path = _WATCHLIST_US_FILE) -> List[str]:
    """
    读美股人工强制关注列表（R2.1，平移 A 股 watchlist.txt 先例）。

    格式：# 开头为注释；ticker 逗号或空白分隔。返回去重大写列表。
    无效条目（^指数、纯数字、非字母数字）跳过并告警；文件不存在返回空。
    """
    if not path.exists():
        return []
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        for raw in line.replace(",", " ").split():
            t = raw.strip().upper()
            clean = t.replace(".", "").replace("-", "")
            if t.startswith("^") or not clean.isalnum() or clean.isdigit():
                logger.warning(f"[Pool] watchlist_us 无效条目跳过: {t}")
                continue
            if t not in out:
                out.append(t)
    if out:
        logger.info(f"[Pool] watchlist_us.txt 读取 {len(out)} 只: {', '.join(out)}")
    return out


# ── 加载 ────────────────────────────────────────────────────────

def _latest_snapshot_file() -> Optional[Path]:
    if not _OUTPUT_ROOT.exists():
        return None
    snapshots = sorted(_OUTPUT_ROOT.glob("*/stock_pool.json"))
    return snapshots[-1] if snapshots else None


def load_forced_held() -> List[str]:
    """从最近一份 stock_pool.json 读取上一轮的 forced_held；无快照/旧格式返回空。

    用途只有一个：判断「持仓票强制留池」是不是**状态翻转**。强制入池每轮重新推导
    （core_pool 不持久化），若无条件写变更日志，同一只票会天天写一条 add 且永远
    没有配对的 remove，`pool_history.jsonl` 就再也回放不出池状态。
    """
    f = _latest_snapshot_file()
    if f is None:
        return []
    try:
        return json.loads(f.read_text()).get("forced_held", [])
    except Exception as e:
        logger.warning(f"[Pool] 读取 forced_held 失败 {f}: {e}")
        return []


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
    forced_held:  Optional[List[str]] = None,
) -> Path:
    """写入 output/<date>/stock_pool.json。

    `forced_held`：仅为风控被强制留池的 paper 持仓票（R9.9/R9.10）。运行期它们被塞进
    `core_pool` 列表以借用「永不被自动移除」这条性质，但**它们不是战略核心池成员**
    ——恰恰相反，它们是被扫描池刷下来的票。故落盘时从 core_pool 中扣掉、单列一栏，
    否则读这份快照的人（或程序）会把一只弱势持仓票当成 STOCK_POOL 成员。
    `final_pool` 仍然包含它们（它们确实在池里，当日确实出了信号）。
    """
    out_dir = _OUTPUT_ROOT / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    forced = sorted(set(forced_held or ()))
    core   = [t for t in core_pool if t not in set(forced)]

    snapshot = {
        "date":          date_str,
        "core_pool":     core,
        "dynamic_pool":  dynamic_pool,
        "forced_held":   forced,
        "final_pool":    sorted(set(core) | set(dynamic_pool) | set(forced)),
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
    source:  str = "user"   # "user" | "auto-screen" | "watchlist"


def append_pool_changes(changes: List[PoolChange]) -> None:
    """追加多条变更到 pool_history.jsonl。"""
    if not changes:
        return
    with _HISTORY_FILE.open("a", encoding="utf-8") as f:
        for ch in changes:
            f.write(json.dumps(asdict(ch), ensure_ascii=False) + "\n")
    logger.info(f"[Pool] 写入 {len(changes)} 条变更日志 → {_HISTORY_FILE}")
