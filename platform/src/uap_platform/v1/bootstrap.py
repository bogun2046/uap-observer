"""Idempotently register approved V1 sources and prompt versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import Connection

from .config import SourceSpec, load_v1_config
from .prompts import v1_prompts

BOOTSTRAP_PRINCIPAL_ID = "00000000-0000-7000-8000-000000001101"


def _configuration(source: SourceSpec) -> dict[str, object]:
    return {
        **dict(source.configuration),
        "fetch_url": source.fetch_url,
        "source_type": source.source_type,
        "v1_1_enabled": source.v1_1_enabled,
    }


def bootstrap(connection: Connection[Any], config_path: Path | None = None) -> dict[str, int]:
    config = load_v1_config(config_path)
    source_updates = 0
    prompt_updates = 0
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE uap_owner")
        cursor.execute(
            """
            INSERT INTO audit.principals (
                id, principal_type, service_name, display_name, active
            ) VALUES (
                %s, 'service'::audit.principal_type,
                'uap-v1-local-bootstrap', 'UAP V1 local bootstrap', true
            ) ON CONFLICT (service_name) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                active = true
            RETURNING id
            """,
            (BOOTSTRAP_PRINCIPAL_ID,),
        )
        principal_id = str(cast(tuple[object], cursor.fetchone())[0])
        for source in config.sources:
            feed_url = source.fetch_url if source.source_type == "rss" else None
            cursor.execute(
                """
                INSERT INTO ingest.sources (
                    id, slug, name, source_type, homepage_url, feed_url,
                    country_code, language_code, enabled,
                    minimum_request_interval_seconds
                ) VALUES (%s, %s, %s, %s::ingest.source_type, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    name = EXCLUDED.name,
                    source_type = EXCLUDED.source_type,
                    homepage_url = EXCLUDED.homepage_url,
                    feed_url = EXCLUDED.feed_url,
                    country_code = EXCLUDED.country_code,
                    language_code = EXCLUDED.language_code,
                    enabled = EXCLUDED.enabled,
                    minimum_request_interval_seconds = EXCLUDED.minimum_request_interval_seconds,
                    updated_at = now()
                RETURNING id
                """,
                (
                    source.id,
                    source.slug,
                    source.name,
                    source.source_type,
                    source.homepage_url,
                    feed_url,
                    source.country_code,
                    source.language_code,
                    source.enabled,
                    source.minimum_request_interval_seconds,
                ),
            )
            source_id = str(cast(tuple[object], cursor.fetchone())[0])
            payload = _configuration(source)
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            cursor.execute(
                """
                SELECT id, configuration_sha256
                  FROM ingest.source_config_versions
                 WHERE source_id = %s AND effective_to IS NULL
                 FOR UPDATE
                """,
                (source_id,),
            )
            current = cursor.fetchone()
            if current is None or str(current[1]) != digest:
                cursor.execute(
                    """
                    UPDATE ingest.source_config_versions
                       SET effective_to = now()
                     WHERE source_id = %s AND effective_to IS NULL
                    """,
                    (source_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO ingest.source_config_versions (
                        id, source_id, version_no, configuration, configuration_sha256,
                        effective_from, changed_by, change_reason
                    ) SELECT
                        gen_random_uuid(), %s, coalesce(max(version_no), 0) + 1,
                        %s::jsonb, %s, now(), %s, 'V1 approved source configuration'
                      FROM ingest.source_config_versions WHERE source_id = %s
                    """,
                    (
                        source_id,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        digest,
                        principal_id,
                        source_id,
                    ),
                )
                source_updates += 1

        for prompt in v1_prompts():
            cursor.execute(
                "SELECT id FROM ops.prompt_versions WHERE content_sha256 = %s",
                (prompt.content_sha256,),
            )
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(
                    "UPDATE ops.prompt_versions SET active = false WHERE task_type = %s",
                    (prompt.task_type.value,),
                )
                cursor.execute(
                    """
                    INSERT INTO ops.prompt_versions (
                        id, task_type, version, system_template, user_template,
                        output_schema, content_sha256, active, created_by
                    ) VALUES (
                        %s, %s::ops.model_task_type, %s, %s, %s, %s::jsonb, %s, true, %s
                    )
                    """,
                    (
                        prompt.id,
                        prompt.task_type.value,
                        prompt.version,
                        prompt.system_template,
                        prompt.user_template,
                        json.dumps(dict(prompt.output_schema), sort_keys=True),
                        prompt.content_sha256,
                        principal_id,
                    ),
                )
                prompt_updates += 1
            else:
                cursor.execute(
                    """
                    UPDATE ops.prompt_versions SET active = (id = %s)
                     WHERE task_type = %s
                    """,
                    (existing[0], prompt.task_type.value),
                )
        cursor.execute("RESET ROLE")
    connection.commit()
    return {"source_config_versions_created": source_updates, "prompts_created": prompt_updates}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    database_url = os.environ.get("UAP_V1_BOOTSTRAP_DATABASE_URL")
    if not database_url:
        raise SystemExit("UAP_V1_BOOTSTRAP_DATABASE_URL is required")
    with psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://", 1)) as db:
        result = bootstrap(db, args.config)
    print(json.dumps(result, sort_keys=True))
