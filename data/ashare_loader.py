"""
A 股本地 CSV 装载器

从 processed_stocks_selected/ 读取手动落地的日线数据，转换为缠论引擎
所需的格式：首字母大写 OHLCV + DatetimeIndex，并保留 CSV 中已预计算好的
技术指标列（MACD/KDJ/RSI/BOLL/CCI）原样透传，供 A 股缠论确认层使用。

CSV 列：symbol,date,open,high,low,close,volume,amount,
       macd_dif,macd_dea,macd,kdj_k,kdj_d,kdj_j,
       rsi_6,rsi_12,rsi_24,boll_upper,boll_mid,boll_lower,cci
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

import pandas as pd
from loguru import logger

# 缠论结构识别最少 K 线数（process_bars→笔→中枢需足够长度）
MIN_BARS = 250

# OHLCV 小写 → 缠论引擎要求的首字母大写
_RENAME = {
    "open": "Open", "high": "High", "low": "Low",
    "close": "Close", "volume": "Volume",
}

# 预计算指标列（确认层使用，原名透传）
INDICATOR_COLS = [
    "macd_dif", "macd_dea", "macd",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi_6", "rsi_12", "rsi_24",
    "boll_upper", "boll_mid", "boll_lower", "cci",
]


def classify_board(code: str) -> str:
    """按代码前缀判定板块（决定涨跌停幅度）。"""
    if code.startswith(("300", "301")):
        return "chinext"      # 创业板 ±20%
    if code.startswith(("688", "689")):
        return "star"         # 科创板 ±20%
    if code.startswith(("8", "4")):
        return "bse"          # 北交所 ±30%
    return "main"             # 沪深主板 ±10%


def board_limit(board: str) -> float:
    """板块对应的单日涨跌停幅度（小数）。"""
    return {"chinext": 0.20, "star": 0.20, "bse": 0.30}.get(board, 0.10)


def load_one_csv(path: Path) -> pd.DataFrame | None:
    """读单个 CSV → 标准化 DataFrame（含指标列、DatetimeIndex）。失败返回 None。"""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as exc:
        logger.warning(f"[AShareLoader] 读取失败 {path.name}: {exc}")
        return None

    if "date" not in df.columns or "close" not in df.columns:
        logger.warning(f"[AShareLoader] {path.name} 缺 date/close 列，跳过")
        return None

    df = df.rename(columns=_RENAME)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # OHLCV 转数值
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # 指标列转数值（缺失列忽略）
    for col in INDICATOR_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df


def load_ashare_prices(
    folder: str = "processed_stocks_selected",
    min_bars: int = MIN_BARS,
) -> Dict[str, pd.DataFrame]:
    """
    批量装载文件夹内所有 CSV。

    返回 {code: DataFrame}，code 取文件名（如 "000938"）。
    每个 DataFrame 带属性 df.attrs["board"] 标记板块。
    数据不足 min_bars 的个股跳过并告警。
    """
    base = Path(folder)
    if not base.exists():
        raise FileNotFoundError(f"未找到 A 股数据文件夹: {folder}")

    files = sorted(base.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"文件夹内无 CSV: {folder}")

    out: Dict[str, pd.DataFrame] = {}
    skipped = 0
    for f in files:
        code = f.stem
        df = load_one_csv(f)
        if df is None:
            skipped += 1
            continue
        if len(df) < min_bars:
            logger.warning(f"[AShareLoader] {code} 仅 {len(df)} 根(<{min_bars})，跳过")
            skipped += 1
            continue
        df.attrs["board"] = classify_board(code)
        out[code] = df

    logger.info(f"[AShareLoader] 装载 {len(out)} 支 A 股（跳过 {skipped}），来源 {folder}")
    return out
