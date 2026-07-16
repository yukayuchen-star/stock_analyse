from datetime import date, timedelta
from functools import lru_cache


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


# ── NYSE 交易日历（R2.2）───────────────────────────────────────
# 规则法内置常规假日，不引第三方依赖。覆盖：元旦、MLK、总统日、耶稣受难日、
# 阵亡将士日、六月节、独立日、劳动节、感恩节、圣诞节，含 observed 移位
# （周六→前一周五补休；周日→下周一补休；元旦落周六不补休——NYSE 惯例）。
# 不建模临时休市（哀悼日/飓风等）——那类日子数据源当天也无新K线，影响面为零。

def _easter(year: int) -> date:
    """复活节（匿名公历算法）——耶稣受难日 = 复活节前的周五。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """当月第 n 个星期 weekday（Mon=0）。"""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """当月最后一个星期 weekday。"""
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    """联邦假日 observed 移位：周六→前一周五，周日→下周一，工作日原样。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=32)
def nyse_holidays(year: int) -> frozenset[date]:
    """该年 NYSE 全天休市日集合（仅常规假日）。"""
    hs: set[date] = set()

    new_year = date(year, 1, 1)
    if new_year.weekday() == 6:                    # 周日→周一补休
        hs.add(new_year + timedelta(days=1))
    elif new_year.weekday() < 5:                   # 落周六不补休（NYSE 规则）
        hs.add(new_year)

    hs.add(_nth_weekday(year, 1, 0, 3))            # MLK：1月第3个周一
    hs.add(_nth_weekday(year, 2, 0, 3))            # 总统日：2月第3个周一
    hs.add(_easter(year) - timedelta(days=2))      # 耶稣受难日
    hs.add(_last_weekday(year, 5, 0))              # 阵亡将士日：5月最后周一
    if year >= 2022:
        hs.add(_observed(date(year, 6, 19)))       # 六月节（2022 起）
    hs.add(_observed(date(year, 7, 4)))            # 独立日
    hs.add(_nth_weekday(year, 9, 0, 1))            # 劳动节：9月第1个周一
    hs.add(_nth_weekday(year, 11, 3, 4))           # 感恩节：11月第4个周四
    hs.add(_observed(date(year, 12, 25)))          # 圣诞节

    return frozenset(h for h in hs if h.weekday() < 5)


def is_trading_day(d: date) -> bool:
    """是否 NYSE 交易日（非周末且非假日）。"""
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def prev_trading_day(reference: date | None = None) -> str:
    """Most recent completed trading day（跳过周末 + NYSE 假日）。"""
    d = (reference or date.today()) - timedelta(days=1)
    while not is_trading_day(d):
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
