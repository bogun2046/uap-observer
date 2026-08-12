"""Environment-only configuration with secret-safe representations."""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by local, CI, and staging containers."""

    model_config = SettingsConfigDict(env_prefix="UAP_", case_sensitive=False, extra="ignore")

    app_env: Literal["development", "ci", "staging"] = "development"
    log_level: str = "INFO"
    database_url: SecretStr
    s3_endpoint: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_secure: bool = False
    s3_buckets: str = "raw,derived,model-io,public-assets"
    health_port: int = 8080

    @property
    def bucket_names(self) -> tuple[str, ...]:
        """Return normalized non-empty bucket names."""

        return tuple(name.strip() for name in self.s3_buckets.split(",") if name.strip())

    def safe_summary(self) -> dict[str, object]:
        """Return only values that are safe to emit to logs."""

        return {
            "app_env": self.app_env,
            "log_level": self.log_level,
            "s3_endpoint": self.s3_endpoint,
            "s3_secure": self.s3_secure,
            "s3_buckets": self.bucket_names,
            "health_port": self.health_port,
        }


def load_settings() -> Settings:
    """Load settings from the process environment."""

    # The mypy plugin cannot infer that BaseSettings supplies required fields from env.
    return Settings()  # type: ignore[call-arg]
