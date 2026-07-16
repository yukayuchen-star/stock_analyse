import time
from typing import Callable, Protocol, TypeVar, runtime_checkable

import pandas as pd
from loguru import logger

T = TypeVar("T")


def with_retry(fn: Callable[[], T], label: str = "",
               retries: int = 2, base_delay: float = 1.0) -> T:
    """
    统一重试薄封装（R3.1）：最多 retries 次重试 + 指数退避（1s/2s），不引重依赖。
    最后一次仍失败则原样抛出，由调用方自己的 except 分支处理。
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= retries:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"[Retry] {label} 第{attempt + 1}次失败: {e} → {delay:.0f}s 后重试")
            time.sleep(delay)
    raise RuntimeError("unreachable")


@runtime_checkable
class DataSource(Protocol):
    """All data sources must satisfy this interface."""

    def is_available(self) -> bool: ...

    def get_price(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Return OHLCV DataFrame with DatetimeIndex."""
        ...

    def get_news(self, ticker: str, days: int = 7) -> pd.DataFrame:
        """Return news DataFrame with columns: datetime, headline, sentiment."""
        ...

    def get_macro(self, series_id: str) -> pd.DataFrame:
        """Return macro series with DatetimeIndex and 'value' column."""
        ...
