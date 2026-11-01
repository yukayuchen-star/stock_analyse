"""资本开支与现金流前向记录入口（每周一次）：`python forward_capex.py`

口径见 `PREREG-capex-forward.md`。先评估已成熟的旧记录，再（距上次 ≥5 天时）记录新快照，最后写进度报告。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from backtest.factor_forward_capex import evaluate_capex_pending, log_capex_events, write_capex_report
from data.pipeline import DataPipeline


def main() -> None:
    today = date.today().isoformat()
    pl = DataPipeline()
    evaluate_capex_pending(pl)
    log_capex_events(today, pl)
    write_capex_report(today, Path("output") / today)


if __name__ == "__main__":
    main()
