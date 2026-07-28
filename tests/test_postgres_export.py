from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uap_observer.postgres_export import snapshot_to_sql


class PostgresExportTests(unittest.TestCase):
    def test_snapshot_to_sql_is_reviewable_and_preserves_jsonb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            output = root / "nested" / "import.sql"
            snapshot.write_text(
                json.dumps(
                    {
                        "tables": {
                            "sources": [],
                            "events": [],
                            "persons": [],
                            "news": [
                                {
                                    "id": 1,
                                    "title": "O'Reilly",
                                    "key_facts": '["公开"]',
                                    "analysis_json": '{"confidence":0.8}',
                                }
                            ],
                            "relationships": [],
                        }
                    }
                ),
                encoding="utf-8",
            )

            rows = snapshot_to_sql(snapshot, output)
            sql = output.read_text(encoding="utf-8")

        self.assertEqual(rows, 1)
        self.assertIn("REVIEW BEFORE EXECUTION", sql)
        self.assertIn("O''Reilly", sql)
        self.assertIn("'[\"公开\"]'::jsonb", sql)
        self.assertIn("OVERRIDING SYSTEM VALUE", sql)
        self.assertNotIn("TRUNCATE", sql)


if __name__ == "__main__":
    unittest.main()
