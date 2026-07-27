"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "uap.db"
DEFAULT_MIGRATIONS_PATH = PROJECT_ROOT / "migrations"
DEFAULT_SOURCES_PATH = PROJECT_ROOT / "config" / "sources.json"


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    database_path: Path = DEFAULT_DATABASE_PATH
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH
    sources_path: Path = DEFAULT_SOURCES_PATH

    @classmethod
    def from_environment(cls) -> "Settings":
        database_value = os.getenv("UAP_DB_PATH")
        database_path = Path(database_value).expanduser() if database_value else DEFAULT_DATABASE_PATH
        return cls(database_path=database_path)
