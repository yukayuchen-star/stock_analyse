from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlphaVantageSettings(BaseSettings):
    key: SecretStr = SecretStr("")
    model_config = SettingsConfigDict(env_prefix="ALPHA_VANTAGE_", env_file=".env", extra="ignore")


class FinnhubSettings(BaseSettings):
    key: SecretStr = SecretStr("")
    model_config = SettingsConfigDict(env_prefix="FINNHUB_", env_file=".env", extra="ignore")


class FREDSettings(BaseSettings):
    key: SecretStr = SecretStr("")
    model_config = SettingsConfigDict(env_prefix="FRED_", env_file=".env", extra="ignore")


class Settings(BaseSettings):
    output_dir: str = "output"
    cache_dir: str = "cache"
    log_dir: str = "logs"

    # 日线价格拉取窗口（calendar days）
    # 800d ≈ 550 TD → processed bars ≈ 413（缠论日K 400根目标）
    # SMA200 在 200TD 后可用，前 252TD 作 EWM 预热期
    price_history_days: int = 800

    # P7 回测专用窗口（calendar days，与信号层分离）
    backtest_history_days: int = 1825  # ~5年，2年预热+3年回测

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def alpha_vantage(self) -> AlphaVantageSettings:
        return AlphaVantageSettings()

    @property
    def finnhub(self) -> FinnhubSettings:
        return FinnhubSettings()

    @property
    def fred(self) -> FREDSettings:
        return FREDSettings()


settings = Settings()
