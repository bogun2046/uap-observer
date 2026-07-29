"""Application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = Path.cwd() / "data" / "uap.db"
DEFAULT_MIGRATIONS_PATH = PACKAGE_ROOT / "migrations"
DEFAULT_SOURCES_PATH = PACKAGE_ROOT / "resources" / "sources.json"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_REASONING_EFFORT = "low"


@dataclass(frozen=True)
class Settings:
    """Runtime settings resolved from environment variables."""

    database_path: Path = DEFAULT_DATABASE_PATH
    migrations_path: Path = DEFAULT_MIGRATIONS_PATH
    sources_path: Path = DEFAULT_SOURCES_PATH
    openai_model: str = DEFAULT_OPENAI_MODEL
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    ai_provider: str = "openai"
    reasoning_effort: str = DEFAULT_REASONING_EFFORT

    @classmethod
    def from_environment(cls) -> Settings:
        database_value = os.getenv("UAP_DB_PATH")
        database_path = Path(database_value).expanduser() if database_value else DEFAULT_DATABASE_PATH
        reasoning_effort = os.getenv(
            "OPENAI_REASONING_EFFORT",
            DEFAULT_REASONING_EFFORT,
        )
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise ValueError(
                "OPENAI_REASONING_EFFORT must be one of: none, low, medium, high, xhigh"
            )
        ai_provider = os.getenv("AI_PROVIDER", "openai").lower()
        if ai_provider not in {"openai", "deepseek"}:
            raise ValueError("AI_PROVIDER must be one of: openai, deepseek")
        return cls(
            database_path=database_path,
            openai_model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
            ai_provider=ai_provider,
            reasoning_effort=reasoning_effort,
        )
