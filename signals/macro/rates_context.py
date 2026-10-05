"""
利率水平与实际利率代理 —— **只读暴露，不进任何打分**（2026-10-05 用户批准）。

起因：深挖 Damodaran 时用 FRED 核数，发现 10Y 在 2026-09-30 到 5.29%（2007 年以来最高），
同期 10Y 盈亏平衡通胀几乎不动 ⇒ 上行几乎全是实际利率。但两条管线的报告里都看不到这件事：
战术侧 yield_score 只看 10Y−2Y 利差，核心侧宏观只有 VIX 档。

口径：
- 实际利率代理 = DGS10 − T10YIE，取两者**同一观测日**的值。按 FRED 定义 T10YIE = DGS10 − DFII10，
  所以它等价于 10Y TIPS 收益率，但这里只叫「代理」。
- 变动：取最近 CHANGE_OBS 个共同观测日的首尾差，单位 bp。
- `dgs10_level_last_seen`：排除最近 RECENT_EXCLUDE_OBS 个观测日后，DGS10 ≥ 当前值的最近一天。
  如果从未达到 ⇒ None，即全序列最高。用来回答「多少年没见过这个水平」。

🔴 不进 `macro_score`、不进核心三轴、不是加速器或加码器。要改宏观轴口径须另行预注册
（会作废宏观基线）。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

CHANGE_OBS = 20
RECENT_EXCLUDE_OBS = 60
NOTE = "只读：不进 macro_score / 核心三轴 / 加速器（2026-10-05 用户批准仅作呈现）"


def _clean(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce").dropna()
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def compute_rates_context(dgs10: pd.Series, t10yie: pd.Series) -> Optional[dict]:
    """两条 FRED 日序列 → 只读利率上下文。任一为空返回 None，调用方据此打降级标。"""
    d, b = _clean(dgs10), _clean(t10yie)
    if d.empty or b.empty:
        return None
    both = pd.concat([d.rename("d"), b.rename("b")], axis=1, join="inner")
    if both.empty:
        return None
    real = both["d"] - both["b"]
    win = both.iloc[-(CHANGE_OBS + 1):]
    rwin = real.iloc[-(CHANGE_OBS + 1):]

    cur = float(d.iloc[-1])
    older = d.iloc[:-RECENT_EXCLUDE_OBS] if len(d) > RECENT_EXCLUDE_OBS else d.iloc[:0]
    hit = older[older >= cur]
    return {
        "dgs10": round(cur, 2),
        "dgs10_asof": d.index[-1].strftime("%Y-%m-%d"),
        "t10yie": round(float(b.iloc[-1]), 2),
        "t10yie_asof": b.index[-1].strftime("%Y-%m-%d"),
        "real_proxy": round(float(real.iloc[-1]), 2),
        "real_proxy_asof": real.index[-1].strftime("%Y-%m-%d"),
        "change_window": [win.index[0].strftime("%Y-%m-%d"), win.index[-1].strftime("%Y-%m-%d")],
        "dgs10_chg_bp": int(round((win["d"].iloc[-1] - win["d"].iloc[0]) * 100)),
        "t10yie_chg_bp": int(round((win["b"].iloc[-1] - win["b"].iloc[0]) * 100)),
        "real_proxy_chg_bp": int(round((rwin.iloc[-1] - rwin.iloc[0]) * 100)),
        "dgs10_level_last_seen": hit.index[-1].strftime("%Y-%m-%d") if not hit.empty else None,
        "note": NOTE,
    }


def rates_line(r: Optional[dict]) -> str:
    """报告用的一行描述；两条管线共用，避免两边各写一套措辞。"""
    if not r:
        return "利率上下文不可得（FRED DGS10/T10YIE 缺失）"
    seen = (f"上次达到此水平 {r['dgs10_level_last_seen']}" if r["dgs10_level_last_seen"]
            else "全序列最高")
    return (f"10Y {r['dgs10']:.2f}%（{r['dgs10_asof']}，{seen}；20 个观测日 {r['dgs10_chg_bp']:+d}bp）· "
            f"BE10Y {r['t10yie']:.2f}%（{r['t10yie_chg_bp']:+d}bp）· "
            f"实际利率代理 10Y−BE {r['real_proxy']:.2f}%（{r['real_proxy_asof']}，{r['real_proxy_chg_bp']:+d}bp）")
