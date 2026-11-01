"""宽宇宙前向记录入口（每周一次，单次约 15 分钟）：`python forward_wide.py`

口径见 `PREREG-wide-forward.md`。先评估已成熟的旧记录，再（距上次 ≥5 天时）记录新快照，最后写进度报告。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from backtest.factor_forward_wide import evaluate_wide_pending, log_wide_events, write_wide_report
from data.pipeline import DataPipeline


def main() -> None:
    today = date.today().isoformat()
    pl = DataPipeline()
    evaluate_wide_pending(pl)
    log_wide_events(today, pl)
    write_wide_report(today, Path("output") / today)


if __name__ == "__main__":
    main()
