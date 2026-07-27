"""SQLite connection management and forward-only migration runner."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


CORE_TABLES = ("sources", "news", "events", "persons", "relationships")


@dataclass(frozen=True)
class DatabaseStatus:
    schema_version: str | None
    row_counts: dict[str, int]


class Database:
    """Own the SQLite file and apply ordered SQL migrations."""

    def __init__(self, path: Path, migrations_path: Path) -> None:
        self.path = Path(path)
        self.migrations_path = Path(migrations_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> list[str]:
        migration_files = sorted(self.migrations_path.glob("*.sql"))
        if not migration_files:
            raise RuntimeError(f"No SQL migrations found in {self.migrations_path}")

        applied_now: list[str] = []
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                )
                """
            )
            applied = {
                row["name"]
                for row in connection.execute("SELECT name FROM schema_migrations").fetchall()
            }

            for migration_file in migration_files:
                if migration_file.name in applied:
                    continue
                connection.executescript(migration_file.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES (?)",
                    (migration_file.name,),
                )
                applied_now.append(migration_file.name)

        return applied_now

    def status(self) -> DatabaseStatus:
        with self.connect() as connection:
            version_row = connection.execute(
                "SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1"
            ).fetchone()
            row_counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CORE_TABLES
            }
        return DatabaseStatus(
            schema_version=version_row["name"] if version_row else None,
            row_counts=row_counts,
        )
