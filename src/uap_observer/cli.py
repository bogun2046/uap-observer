"""Command-line interface for local development and automation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from uap_observer.config import Settings
from uap_observer.database import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uap-observer")
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path (defaults to data/uap.db or UAP_DB_PATH).",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or migrate the SQLite database.")
    subparsers.add_parser("db-status", help="Show migration and row-count status.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_environment()
    database = Database(args.database or settings.database_path, settings.migrations_path)

    if args.command == "init-db":
        applied = database.initialize()
        if applied:
            print(f"Initialized {database.path}; applied: {', '.join(applied)}")
        else:
            print(f"Database is current: {database.path}")
        return 0

    database.initialize()
    status = database.status()
    print(f"Database: {database.path}")
    print(f"Schema version: {status.schema_version}")
    for table, count in status.row_counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
