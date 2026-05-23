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
    # 数据拉取窗口（天）
    price_history_days: int = 365

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
