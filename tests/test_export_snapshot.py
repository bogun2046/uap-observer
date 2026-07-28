from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.database import Database
from uap_observer.export_snapshot import export_snapshot
from uap_observer.models import FactStatus, News, NewsCategory
from uap_observer.repositories import Repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ExportSnapshotTests(unittest.TestCase):
    def test_export_contains_all_core_tables_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db", PROJECT_ROOT / "migrations")
            database.initialize()
            Repository(database).add_news(
                News(
                    title="Snapshot",
                    original_title="Snapshot",
                    source="Test",
                    source_url="https://example.test/snapshot",
                    category=NewsCategory.OTHER,
                    credibility=2,
                    fact_status=FactStatus.UNVERIFIED,
                )
            )
            output = Path(directory) / "nested" / "snapshot.json"

            rows = export_snapshot(database, output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(rows, 1)
        self.assertEqual(payload["schema_version"], "005_organizations.sql")
        self.assertEqual(payload["tables"]["news"][0]["title"], "Snapshot")
        self.assertEqual(payload["tables"]["relationships"], [])


if __name__ == "__main__":
    unittest.main()
