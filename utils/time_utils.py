from datetime import date, timedelta


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def prev_trading_day(reference: date | None = None) -> str:
    """Most recent completed trading day (skips weekends, no holiday calendar)."""
    d = (reference or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def earnings_available_date(datadate: str) -> str:
    """2-month delay rule: Q-end → earliest date earnings are public.

    Q1 03-31 → 06-01 | Q2 06-30 → 09-01 | Q3 09-30 → 12-01 | Q4 12-31 → next 03-01
    """
    from pandas import Timestamp
    d = Timestamp(datadate)
    mapping = {3: ("06-01", 0), 6: ("09-01", 0), 9: ("12-01", 0), 12: ("03-01", 1)}
    suffix, year_offset = mapping[d.month]
    return f"{d.year + year_offset}-{suffix}"
