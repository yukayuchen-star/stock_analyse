from typing import Protocol, runtime_checkable
import pandas as pd


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
