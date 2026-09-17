"""Environment-only configuration for the isolated OIDC Admin API."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_FORBIDDEN_DSN_NAMES = frozenset(
    {
        "UAP_DATABASE_URL",
        "UAP_PUBLIC_DATABASE_URL",
        "UAP_PUBLISHER_DATABASE_URL",
        "UAP_WORKER_DATABASE_URL",
        "UAP_OWNER_DATABASE_URL",
        "UAP_MIGRATOR_DATABASE_URL",
    }
)
_FORBIDDEN_USERS = frozenset(
    {
        "uap_public_reader",
        "uap_publisher",
        "uap_worker",
        "uap_owner",
        "uap_migrator",
        "postgres",
    }
)


class AdminApiSettings(BaseSettings):
    """The Admin API accepts exactly one least-privilege database identity."""

    model_config = SettingsConfigDict(env_prefix="UAP_ADMIN_", case_sensitive=False, extra="ignore")

    database_url: SecretStr
    cursor_secret: SecretStr
    oidc_issuer: str = Field(min_length=1)
    oidc_audience: str = Field(min_length=1)
    oidc_jwks: str = Field(min_length=1)
    host: str = "127.0.0.1"
    port: int = Field(default=8082, ge=1, le=65535)
    pool_min_size: int = Field(default=1, ge=1, le=20)
    pool_max_size: int = Field(default=8, ge=1, le=50)
    cursor_max_length: int = Field(default=2048, ge=256, le=8192)

    @field_validator("cursor_secret")
    @classmethod
    def _strong_cursor_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("cursor secret must contain at least 32 bytes")
        return value

    @field_validator("oidc_issuer")
    @classmethod
    def _issuer_value(cls, value: str) -> str:
        issuer = value.strip()
        if not issuer.startswith("https://") and not issuer.startswith("http://"):
            raise ValueError("OIDC issuer is invalid")
        return issuer

    @field_validator("oidc_jwks")
    @classmethod
    def _jwks_document(cls, value: str) -> str:
        try:
            document = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("OIDC JWKS is invalid") from error
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError("OIDC JWKS is invalid")
        if not document["keys"]:
            raise ValueError("OIDC JWKS is invalid")
        return value

    @field_validator("database_url")
    @classmethod
    def _api_only_dsn(cls, value: SecretStr) -> SecretStr:
        dsn = value.get_secret_value().replace("postgresql+psycopg://", "postgresql://", 1)
        try:
            user = conninfo_to_dict(dsn).get("user")
        except Exception as error:
            raise ValueError("admin API database URL is invalid") from error
        if user != "uap_api":
            raise ValueError("admin API database role is required")
        if user in _FORBIDDEN_USERS:
            raise ValueError("admin API database role is required")
        return SecretStr(dsn)

    def model_post_init(self, __context: object) -> None:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")

    @property
    def psycopg_database_url(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def cursor_key(self) -> bytes:
        return self.cursor_secret.get_secret_value().encode("utf-8")

    @property
    def jwks_document(self) -> dict[str, object]:
        document = json.loads(self.oidc_jwks)
        if not isinstance(document, dict):
            raise ValueError("OIDC JWKS is invalid")
        return document

    def safe_summary(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "pool_min_size": self.pool_min_size,
            "pool_max_size": self.pool_max_size,
            "cursor_max_length": self.cursor_max_length,
            "oidc_issuer": self.oidc_issuer,
            "oidc_audience": self.oidc_audience,
        }


def load_admin_api_settings(
    environ: Mapping[str, str] | None = None,
) -> AdminApiSettings:
    """Reject privileged DSNs before loading the dedicated admin settings."""

    values = os.environ if environ is None else environ
    forbidden = sorted(
        name
        for name, value in values.items()
        if value
        and name != "UAP_ADMIN_DATABASE_URL"
        and (
            name in _FORBIDDEN_DSN_NAMES
            or name.upper().endswith("_DATABASE_URL")
            or name.upper().endswith("_DSN")
            or name.upper() == "DATABASE_URL"
        )
    )
    if forbidden:
        raise ValueError("privileged database configuration is forbidden in the Admin API")
    if environ is None:
        return AdminApiSettings()  # type: ignore[call-arg]
    return AdminApiSettings.model_validate(
        {
            "database_url": values.get("UAP_ADMIN_DATABASE_URL", ""),
            "cursor_secret": values.get("UAP_ADMIN_CURSOR_SECRET", ""),
            "oidc_issuer": values.get("UAP_ADMIN_OIDC_ISSUER", ""),
            "oidc_audience": values.get("UAP_ADMIN_OIDC_AUDIENCE", ""),
            "oidc_jwks": values.get("UAP_ADMIN_OIDC_JWKS", ""),
            "host": values.get("UAP_ADMIN_HOST", "127.0.0.1"),
            "port": values.get("UAP_ADMIN_PORT", "8082"),
            "pool_min_size": values.get("UAP_ADMIN_POOL_MIN_SIZE", "1"),
            "pool_max_size": values.get("UAP_ADMIN_POOL_MAX_SIZE", "8"),
            "cursor_max_length": values.get("UAP_ADMIN_CURSOR_MAX_LENGTH", "2048"),
        }
    )
