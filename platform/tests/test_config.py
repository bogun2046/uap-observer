from __future__ import annotations

from pydantic import SecretStr

from uap_platform.config import Settings


def make_settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql://user:database-secret@postgres/db"),
        s3_endpoint="object-store:8333",
        s3_access_key=SecretStr("access-secret"),
        s3_secret_key=SecretStr("object-secret"),
    )


def test_settings_hide_secrets_and_normalize_buckets() -> None:
    settings = make_settings()

    rendered = repr(settings)
    summary = repr(settings.safe_summary())

    assert "database-secret" not in rendered
    assert "access-secret" not in rendered
    assert "object-secret" not in rendered
    assert "database-secret" not in summary
    assert settings.bucket_names == ("raw", "derived", "model-io", "public-assets")
