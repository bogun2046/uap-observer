"""Export a deterministic SQLite JSON snapshot for reviewed migration."""

from __future__ import annotations

import json
from pathlib import Path

from uap_observer.database import CORE_TABLES, Database


def export_snapshot(database: Database, output: Path) -> int:
    database.initialize()
    with database.connect() as connection:
        tables = list(dict.fromkeys(("sources", *CORE_TABLES)))
        snapshot = {
            "schema_version": database.status().schema_version,
            "tables": {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in tables
            },
        }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return sum(len(rows) for rows in snapshot["tables"].values())
