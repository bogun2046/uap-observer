"""Generate reviewable PostgreSQL INSERT SQL from an SQLite JSON snapshot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TABLE_ORDER = ("sources", "events", "persons", "news", "relationships")
JSONB_FIELDS = {
    "include_keywords",
    "exclude_keywords",
    "key_facts",
    "viewpoints",
    "analysis_json",
    "risk_flags",
}


def snapshot_to_sql(snapshot_path: Path, output_path: Path) -> int:
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise TypeError("Snapshot must contain a tables object")
    statements = [
        "-- REVIEW BEFORE EXECUTION: target Supabase database must be empty or isolated.",
        "-- No destructive statements are generated.",
        "BEGIN;",
    ]
    inserted = 0
    for table in TABLE_ORDER:
        rows = tables.get(table, [])
        if not isinstance(rows, list):
            raise TypeError(f"Snapshot table {table!r} must be a list")
        for row in rows:
            if not isinstance(row, dict) or not row:
                raise TypeError(f"Snapshot row in {table!r} must be a non-empty object")
            columns = list(row)
            values = [_sql_value(column, row[column]) for column in columns]
            statements.append(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"OVERRIDING SYSTEM VALUE VALUES ({', '.join(values)});"
            )
            inserted += 1
    for table in TABLE_ORDER:
        statements.append(
            "SELECT setval(pg_get_serial_sequence("
            f"'{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1), true);"
        )
    statements.append("COMMIT;")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(statements) + "\n", encoding="utf-8")
    return inserted


def _sql_value(column: str, value: Any) -> str:
    if value is None:
        return "NULL"
    if column in JSONB_FIELDS:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return f"{_quote(encoded)}::jsonb"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return _quote(str(value))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
