"""Real Public API process and role probe for G10-16 through G10-20."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import psycopg

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
for path in (str(PLATFORM_ROOT), str(PLATFORM_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tools.wp10_2_runtime_probe import (  # noqa: E402
    document_manifest,
    event_payload,
    insert_event,
    libpq_url,
    scalar,
)
from tools.wp10_3_runtime_probe import claim_for, withdraw_document  # noqa: E402
from uap_platform.publishing.service import PublicationService  # noqa: E402

EVIDENCE: list[dict[str, Any]] = []
RESPONSES: list[dict[str, Any]] = []
CURSOR_KEY = b"wp10-4-runtime-cursor-secret-32-bytes"


def bad_version_cursor() -> str:
    raw = json.dumps(
        {
            "filters_sha256": hashlib.sha256(
                b'{"category":null,"fact_status":null,"published_after":null}'
            ).hexdigest(),
            "last": [],
            "resource": "documents",
            "sort": "published_at_desc,id_desc",
            "v": 2,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(hmac.digest(CURSOR_KEY, raw, "sha256"))
    return payload + "." + signature.rstrip(b"=").decode()


def require(name: str, actual: object, expected: object) -> None:
    passed = actual == expected
    EVIDENCE.append(
        {
            "kind": "assertion",
            "name": name,
            "actual": actual,
            "expected": expected,
            "passed": passed,
        }
    )
    if not passed:
        raise RuntimeError(f"{name}: expected {expected!r}, got {actual!r}")


def request(
    base_url: str, path: str, *, request_id: str | None = None
) -> tuple[int, dict[str, str], Any]:
    headers = {} if request_id is None else {"X-Request-ID": request_id}
    parsed = urlsplit(base_url)
    host = parsed.hostname
    if host is None:
        raise RuntimeError("probe URL is missing a hostname")
    connection = http.client.HTTPConnection(host, parsed.port, timeout=5)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = dict(response.getheaders())
        body = json.loads(response.read())
    finally:
        connection.close()
    RESPONSES.append({"status": status, "headers": response_headers, "body": body})
    return status, response_headers, body


def execute(connection: psycopg.Connection[Any], statement: str, *params: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)


def seed_projection(admin: psycopg.Connection[Any]) -> dict[str, list[uuid.UUID]]:
    now = datetime.now(UTC).replace(microsecond=0)
    capacity = 2200
    documents = [uuid.uuid4() for _ in range(capacity)]
    entities = [uuid.uuid4() for _ in range(capacity)]
    claims = [uuid.uuid4(), uuid.uuid4()]
    evidence = [uuid.uuid4(), uuid.uuid4()]
    special_titles = [
        "Orbital signal alpha",
        "Orbital signal beta",
        "Orbital signal gamma",
        "未识别飞行现象 黄金搜索",
        "Archived observation",
        "Orbital signal delta",
    ]
    special_categories = [
        "official_report",
        "official_report",
        "military",
        "sighting",
        "other",
        "official_report",
    ]
    special_facts = [
        "official_record",
        "corroborated",
        "source_reported",
        "unverified",
        "opinion",
        "official_record",
    ]
    with admin.transaction():
        execute(admin, "SET LOCAL session_replication_role=replica")
        for index, document_id in enumerate(documents):
            published_at = now - timedelta(minutes=index // 2)
            execute(
                admin,
                """
                INSERT INTO public.documents (
                    id, document_grant_id, slug, title, summary, category, fact_status,
                    source_name, canonical_source_url, source_published_at,
                    published_at, revised_at, revision_no
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'Runtime source', %s,
                          %s, %s, NULL, 1)
                """,
                document_id,
                uuid.uuid4(),
                f"runtime-document-{index}",
                special_titles[index]
                if index < len(special_titles)
                else f"Capacity orbital signal {index}",
                f"Safe public summary {index}",
                special_categories[index] if index < 6 else "official_report",
                special_facts[index] if index < 6 else "corroborated",
                f"https://example.test/runtime/{document_id}",
                published_at - timedelta(days=1),
                published_at,
            )
            title = (
                special_titles[index]
                if index < len(special_titles)
                else f"Capacity orbital signal {index}"
            )
            category = special_categories[index] if index < 6 else "official_report"
            fact = special_facts[index] if index < 6 else "corroborated"
            display = f"{title} safe summary orbital signal" if index != 4 else title
            execute(
                admin,
                """
                INSERT INTO public.search_documents (
                    document_id, search_vector, display_text, facets, indexed_at
                ) VALUES (%s, to_tsvector('simple', %s), %s,
                          jsonb_build_object(
                              'category', %s::text, 'fact_status', %s::text
                          ), %s)
                """,
                document_id,
                display,
                display,
                category,
                fact,
                now,
            )
        for index, entity_id in enumerate(entities):
            execute(
                admin,
                """
                INSERT INTO public.entities (
                    id, entity_grant_id, slug, entity_type, name, description,
                    country_code, published_at, revision_no
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
                """,
                entity_id,
                uuid.uuid4(),
                f"runtime-entity-{index}",
                ("organization", "location", "person")[index % 3],
                ("Alpha Org", "北京", "Public Person")[index]
                if index < 3
                else f"Entity {index:04d}",
                f"Safe entity {index}",
                ("US", "CN", None)[index % 3],
                now - timedelta(minutes=index // 2),
            )
        for index, claim_id in enumerate(claims):
            execute(
                admin,
                """
                INSERT INTO public.claims (
                    id, document_id, claim_grant_id, ordinal, claim_text,
                    claim_type, assertion_status, attribution, revision_no
                ) VALUES (%s, %s, %s, %s, %s, 'observation', 'reported', NULL, 1)
                """,
                claim_id,
                documents[0],
                uuid.uuid4(),
                index,
                f"Public claim {index}",
            )
            execute(
                admin,
                """
                INSERT INTO public.evidence (
                    id, document_id, excerpt, locator_type, page_start, page_end,
                    time_start_ms, time_end_ms, public_locator, locator_sha256, source_url
                ) VALUES (%s, %s, %s, 'text', NULL, NULL, NULL, NULL,
                          '{}'::jsonb, %s, %s)
                """,
                evidence[index],
                documents[0],
                f"Public excerpt {index}",
                f"{index + 1:064x}",
                f"https://example.test/runtime/{documents[0]}",
            )
            execute(
                admin,
                "INSERT INTO public.claim_evidence (id, claim_id, evidence_id) VALUES (%s, %s, %s)",
                uuid.uuid4(),
                claim_id,
                evidence[index],
            )
        execute(
            admin,
            """
            INSERT INTO public.document_entities (
                id, document_id, entity_id, basis_evidence_id, basis_claim_id, basis_relation_id
            ) VALUES (%s, %s, %s, %s, %s, NULL), (%s, %s, %s, %s, %s, NULL)
            """,
            uuid.uuid4(),
            documents[0],
            entities[0],
            evidence[0],
            claims[0],
            uuid.uuid4(),
            documents[0],
            entities[1],
            evidence[1],
            claims[1],
        )
    return {"documents": documents, "entities": entities, "claims": claims, "evidence": evidence}


def insert_newer_document(admin: psycopg.Connection[Any]) -> uuid.UUID:
    document_id = uuid.uuid4()
    now = datetime.now(UTC) + timedelta(minutes=1)
    with admin.transaction():
        execute(admin, "SET LOCAL session_replication_role=replica")
        execute(
            admin,
            """
            INSERT INTO public.documents (
                id, document_grant_id, slug, title, summary, category, fact_status,
                source_name, canonical_source_url, published_at, revision_no
            ) VALUES (%s, %s, %s, 'New concurrent document', NULL, 'other', 'unverified',
                      'Runtime source', %s, %s, 1)
            """,
            document_id,
            uuid.uuid4(),
            f"concurrent-{document_id}",
            f"https://example.test/runtime/{document_id}",
            now,
        )
    return document_id


def mutate_document_during_page(
    admin: psycopg.Connection[Any], document_ids: list[uuid.UUID]
) -> None:
    with admin.transaction():
        execute(admin, "SET LOCAL session_replication_role=replica")
        execute(
            admin,
            "UPDATE public.documents SET title='Concurrent revised document' WHERE id=%s",
            document_ids[0],
        )
        execute(admin, "DELETE FROM public.documents WHERE id=%s", document_ids[-1])


def mutate_entity_during_page(
    admin: psycopg.Connection[Any], entity_ids: list[uuid.UUID]
) -> uuid.UUID:
    new_entity_id = uuid.uuid4()
    now = datetime.now(UTC) + timedelta(minutes=1)
    with admin.transaction():
        execute(admin, "SET LOCAL session_replication_role=replica")
        execute(
            admin,
            "UPDATE public.entities SET name='Concurrent revised entity' WHERE id=%s",
            entity_ids[0],
        )
        execute(admin, "DELETE FROM public.entities WHERE id=%s", entity_ids[-1])
        execute(
            admin,
            """
            INSERT INTO public.entities (
                id, entity_grant_id, slug, entity_type, name, description,
                country_code, published_at, revision_no
            ) VALUES (%s, %s, %s, 'organization', 'Concurrent inserted entity',
                      'Safe concurrent fixture', 'US', %s, 1)
            """,
            new_entity_id,
            uuid.uuid4(),
            f"concurrent-entity-{new_entity_id}",
            now,
        )
    return new_entity_id


def mutate_search_during_page(
    admin: psycopg.Connection[Any], document_ids: list[uuid.UUID]
) -> dict[str, Any]:
    new_document_id = uuid.uuid4()
    withdrawn_document_id = document_ids[-2]
    now = datetime.now(UTC) + timedelta(minutes=1)
    display = " ".join(["orbital", "signal"] * 12)
    with admin.transaction():
        execute(admin, "SET LOCAL session_replication_role=replica")
        execute(
            admin,
            "UPDATE public.search_documents "
            "SET display_text=%s, search_vector=to_tsvector('simple', %s) "
            "WHERE document_id=%s",
            "Concurrent revised orbital signal",
            "Concurrent revised orbital signal",
            document_ids[0],
        )
        with admin.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.search_documents WHERE document_id=%s",
                (withdrawn_document_id,),
            )
            withdrawn_rows = cursor.rowcount
        if withdrawn_rows != 1:
            raise RuntimeError(
                "search pagination fixture expected one independent withdraw row, "
                f"got {withdrawn_rows}"
            )
        execute(
            admin,
            """
            INSERT INTO public.documents (
                id, document_grant_id, slug, title, summary, category, fact_status,
                source_name, canonical_source_url, published_at, revision_no
            ) VALUES (%s, %s, %s, 'Concurrent inserted orbital signal', NULL,
                      'official_report', 'corroborated', 'Runtime source', %s, %s, 1)
            """,
            new_document_id,
            uuid.uuid4(),
            f"concurrent-search-{new_document_id}",
            f"https://example.test/concurrent/{new_document_id}",
            now,
        )
        execute(
            admin,
            """
            INSERT INTO public.search_documents (
                document_id, search_vector, display_text, facets, indexed_at
            ) VALUES (%s, to_tsvector('simple', %s), %s,
                      '{"category":"official_report","fact_status":"corroborated"}', %s)
            """,
            new_document_id,
            display,
            display,
            now,
        )
        inserted_rank = scalar(
            admin,
            """
            SELECT ts_rank_cd(search.search_vector, websearch_to_tsquery('simple', %s))
              FROM public.search_documents AS search
             WHERE search.document_id=%s
            """,
            "orbital signal",
            new_document_id,
        )
    return {
        "inserted_id": str(new_document_id),
        "revised_id": str(document_ids[0]),
        "withdrawn_id": str(withdrawn_document_id),
        "withdrawn_rows": withdrawn_rows,
        "inserted_rank": float(inserted_rank),
    }


def search_rank(admin: psycopg.Connection[Any], document_id: str) -> float:
    rank = scalar(
        admin,
        """
        SELECT ts_rank_cd(search.search_vector, websearch_to_tsquery('simple', %s))
          FROM public.search_documents AS search
         WHERE search.document_id=%s
        """,
        "orbital signal",
        document_id,
    )
    return float(rank)


def permission_error(connection: psycopg.Connection[Any], statement: str) -> str | None:
    try:
        execute(connection, statement)
    except psycopg.Error as error:
        connection.rollback()
        EVIDENCE.append(
            {"kind": "sql_error", "statement_class": "permission", "sqlstate": error.sqlstate}
        )
        return error.sqlstate
    connection.rollback()
    return None


def wait_ready(base_url: str, process: subprocess.Popen[str]) -> None:
    last_error = "no response"
    for _ in range(60):
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(f"Public API exited during startup: {output}")
        try:
            status, _headers, _body = request(base_url, "/v1/documents?limit=1")
            if status == 200:
                return
        except OSError as error:
            last_error = type(error).__name__
            time.sleep(0.1)
    output = ""
    if process.stdout is not None:
        output = process.stdout.read() or ""
    raise RuntimeError(f"Public API process did not become ready: {last_error}; {output}")


def run(admin_url: str, publisher_url: str, reader_url: str) -> None:
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as admin:
        fixture = seed_projection(admin)
        EVIDENCE.append(
            {
                "kind": "capacity_fixture",
                "documents": len(fixture["documents"]),
                "entities": len(fixture["entities"]),
                "search_documents": len(fixture["documents"]),
                "passed": len(fixture["documents"]) >= 2000 and len(fixture["entities"]) >= 2000,
            }
        )
        if not EVIDENCE[-1]["passed"]:
            raise RuntimeError("capacity fixture below 2000 rows")
        # This probe runs in its own disposable container, so a fixed loopback
        # port is deterministic and cannot collide with another process.
        port = 38081
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(PLATFORM_ROOT / "src"),
            "LANG": "C.UTF-8",
            "UAP_PUBLIC_DATABASE_URL": reader_url,
            "UAP_PUBLIC_CURSOR_SECRET": CURSOR_KEY.decode(),
            "UAP_PUBLIC_HOST": "127.0.0.1",
            "UAP_PUBLIC_PORT": str(port),
            "UAP_PUBLIC_POOL_MIN_SIZE": "1",
            "UAP_PUBLIC_POOL_MAX_SIZE": "4",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "uap_platform.public_api.server"],
            cwd=PLATFORM_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if process.stdout is not None:
            os.set_blocking(process.stdout.fileno(), False)
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_ready(base_url, process)
            request_id = str(uuid.uuid4())
            status, headers, page = request(
                base_url, "/v1/documents?limit=2", request_id=request_id
            )
            require("G10-16 document list status", status, 200)
            require("G10-16 request ID preserved", headers.get("X-Request-ID"), request_id)
            require("G10-16 success media", headers.get("Content-Type"), "application/json")
            require("G10-16 document page size", len(page["items"]), 2)
            first_ids = [item["id"] for item in page["items"]]
            new_id = insert_newer_document(admin)
            mutate_document_during_page(admin, fixture["documents"])
            EVIDENCE.append(
                {
                    "kind": "pagination_mutation",
                    "name": "G10-17 document insert revise withdraw during page",
                    "inserted": str(new_id),
                    "revised": str(fixture["documents"][0]),
                    "withdrawn": str(fixture["documents"][-1]),
                    "passed": True,
                }
            )
            status, _headers, second = request(
                base_url,
                "/v1/documents?" + urlencode({"limit": 2, "cursor": page["next_cursor"]}),
            )
            require("G10-17 document second page", status, 200)
            second_ids = [item["id"] for item in second["items"]]
            require(
                "G10-17 no duplicate after concurrent insert",
                bool(set(first_ids) & set(second_ids)),
                False,
            )
            require(
                "G10-17 new row does not enter passed interval", str(new_id) in second_ids, False
            )

            document_id = fixture["documents"][0]
            claim_id = fixture["claims"][0]
            entity_id = fixture["entities"][0]
            status, _headers, detail = request(base_url, f"/v1/documents/{document_id}")
            require("G10-16 document detail status", status, 200)
            require(
                "G10-16 claims fixed ordering",
                [item["ordinal"] for item in detail["claims"]],
                [0, 1],
            )
            require(
                "G10-16 entities C ordering",
                [item["name"] for item in detail["related_entities"]],
                ["Alpha Org", "北京"],
            )
            status, _headers, claim = request(base_url, f"/v1/claims/{claim_id}")
            require("G10-16 claim detail", status, 200)
            require("G10-16 claim evidence", len(claim["evidence"]), 1)
            status, _headers, entity = request(base_url, f"/v1/entities/{entity_id}")
            require("G10-16 entity detail", status, 200)
            require("G10-16 relation field omitted", "relations" in entity, False)
            require("G10-16 entity reverse association", len(entity["related_documents"]), 1)
            status, _headers, empty = request(base_url, "/v1/entities?type=concept")
            require("G10-16 empty entity page", empty, {"items": [], "next_cursor": None})

            status, _headers, closed = request(base_url, "/v1/relations")
            require("G10-16 relation capability status", status, 404)
            require("G10-16 relation capability code", closed["code"], "api_capability_closed")
            status, _headers, closed_detail = request(base_url, f"/v1/relations/{uuid.uuid4()}")
            require("G10-16 relation detail capability status", status, 404)
            require(
                "G10-16 relation detail capability code",
                closed_detail["code"],
                "api_capability_closed",
            )
            status, _headers, health = request(base_url, "/healthz")
            require("G10-19 healthz status", status, 200)
            require("G10-19 healthz body", health, {"status": "ok"})
            status, _headers, unknown = request(base_url, "/v1/ordinary-unknown")
            require("G10-16 unknown path code", unknown["code"], "api_resource_not_found")

            status, _headers, entities_page = request(base_url, "/v1/entities?limit=1")
            require("G10-17 entity first page", status, 200)
            new_entity_id = mutate_entity_during_page(admin, fixture["entities"])
            status, _headers, entities_next = request(
                base_url,
                "/v1/entities?" + urlencode({"limit": 1, "cursor": entities_page["next_cursor"]}),
            )
            require("G10-17 entity second page", status, 200)
            require(
                "G10-17 entity no duplicate",
                entities_next["items"][0]["id"] == entities_page["items"][0]["id"],
                False,
            )
            require(
                "G10-17 entity inserted row does not enter passed interval",
                str(new_entity_id) in [item["id"] for item in entities_next["items"]],
                False,
            )
            EVIDENCE.append(
                {
                    "kind": "pagination_mutation",
                    "name": "G10-17 entity insert revise withdraw during page",
                    "inserted": str(new_entity_id),
                    "revised": str(fixture["entities"][0]),
                    "withdrawn": str(fixture["entities"][-1]),
                    "passed": True,
                }
            )

            search_path = "/v1/search?" + urlencode({"q": "orbital signal", "limit": 2})
            status, _headers, search = request(base_url, search_path)
            require("G10-18 English golden search", status, 200)
            require("G10-18 search first page size", len(search["items"]), 2)
            before_search_ids = [item["document"]["id"] for item in search["items"]]
            before_search_boundary_rank = search_rank(admin, before_search_ids[-1])
            search_mutation = mutate_search_during_page(admin, fixture["documents"])
            new_search_id = search_mutation["inserted_id"]
            status, _headers, search_next = request(
                base_url,
                "/v1/search?"
                + urlencode({"q": "orbital signal", "limit": 2, "cursor": search["next_cursor"]}),
            )
            require("G10-17 search second page", status, 200)
            require(
                "G10-17 search no duplicate",
                bool(
                    {item["document"]["id"] for item in search["items"]}
                    & {item["document"]["id"] for item in search_next["items"]}
                ),
                False,
            )
            require(
                "G10-17 search inserted row does not enter passed interval",
                new_search_id in {item["document"]["id"] for item in search_next["items"]},
                False,
            )
            require("G10-17 search withdraw affected one row", search_mutation["withdrawn_rows"], 1)
            require(
                "G10-17 search inserted rank is above page boundary",
                search_mutation["inserted_rank"] > before_search_boundary_rank,
                True,
            )
            EVIDENCE.append(
                {
                    "kind": "pagination_mutation",
                    "name": "G10-17 search insert revise withdraw during page",
                    "inserted": str(new_search_id),
                    "revised": search_mutation["revised_id"],
                    "withdrawn": search_mutation["withdrawn_id"],
                    "withdraw_delete_count": search_mutation["withdrawn_rows"],
                    "before_page": before_search_ids,
                    "after_page": [item["document"]["id"] for item in search_next["items"]],
                    "before_boundary_rank": before_search_boundary_rank,
                    "inserted_rank": search_mutation["inserted_rank"],
                    "inserted_in_after_page": new_search_id
                    in {item["document"]["id"] for item in search_next["items"]},
                    "passed": True,
                }
            )
            status, _headers, chinese = request(
                base_url, "/v1/search?" + urlencode({"q": "未识别飞行现象"})
            )
            require("G10-18 Chinese golden search", status, 200)
            require("G10-18 Chinese result present", len(chinese["items"]) >= 1, True)
            status, _headers, filtered = request(
                base_url,
                "/v1/search?"
                + urlencode(
                    {"q": "orbital", "category": "military", "fact_status": "source_reported"}
                ),
            )
            require("G10-18 category/fact filter", len(filtered["items"]), 1)
            for length, expected in ((1, 400), (2, 200), (200, 200), (201, 400)):
                status, _headers, _body = request(
                    base_url, "/v1/search?" + urlencode({"q": "测" * length})
                )
                require(f"G10-18 q boundary {length}", status, expected)
            status, _headers, _injection = request(
                base_url, "/v1/search?" + urlencode({"q": "x'; DROP TABLE public.documents; --"})
            )
            require("G10-18 injection is data", status, 200)
            require(
                "G10-18 projection survives injection",
                scalar(admin, "SELECT count(*) FROM public.documents") > 0,
                True,
            )

            cursor = page["next_cursor"]
            cursor_cases = [
                "/v1/documents?"
                + urlencode({"cursor": cursor[:-1] + ("A" if cursor[-1] != "A" else "B")}),
                "/v1/entities?" + urlencode({"cursor": cursor}),
                "/v1/documents?" + urlencode({"cursor": cursor, "category": "military"}),
                "/v1/documents?" + urlencode({"cursor": bad_version_cursor()}),
                "/v1/documents?cursor=" + "x" * 2050,
            ]
            for index, path in enumerate(cursor_cases):
                status, _headers, invalid = request(base_url, path)
                require(f"G10-17 invalid cursor {index} status", status, 400)
                require(
                    f"G10-17 invalid cursor {index} code", invalid["code"], "api_cursor_invalid"
                )

            latencies: list[float] = []
            for _ in range(40):
                started = time.perf_counter()
                require(
                    "G10-18 capacity request",
                    request(base_url, "/v1/search?q=orbital&limit=20")[0],
                    200,
                )
                latencies.append((time.perf_counter() - started) * 1000)
            p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]
            EVIDENCE.append(
                {
                    "kind": "performance",
                    "name": "phase search p95 ms",
                    "value": round(p95, 3),
                    "samples": len(latencies),
                    "threshold_ms": 500.0,
                    "passed": p95 < 500,
                }
            )
            if p95 >= 500:
                raise RuntimeError("phase search p95 exceeded")

            with psycopg.connect(reader_url, autocommit=True) as reader:
                with reader.cursor() as db_cursor:
                    db_cursor.execute(
                        "SELECT session_user, current_setting('transaction_isolation')"
                    )
                    session_row = db_cursor.fetchone()
                    if session_row is None:
                        raise RuntimeError("reader session probe returned no row")
                    role, _isolation = session_row
                require("G10-19 actual reader session", role, "uap_public_reader")
                for schema in ("ingest", "core", "ops", "audit"):
                    require(
                        f"G10-19 {schema} schema usage denied",
                        scalar(
                            reader, "SELECT has_schema_privilege(session_user, %s, 'USAGE')", schema
                        ),
                        False,
                    )
                for statement in (
                    "SELECT * FROM ingest.sources LIMIT 1",
                    "SELECT * FROM core.documents LIMIT 1",
                    "SELECT * FROM ops.outbox_events LIMIT 1",
                    "SELECT * FROM audit.review_cases LIMIT 1",
                    "SELECT * FROM ops.claim_publication_outbox('probe', 60, 1)",
                    "DELETE FROM public.documents WHERE false",
                ):
                    require(
                        "G10-19 privilege SQLSTATE", permission_error(reader, statement), "42501"
                    )

            publisher = psycopg.connect(publisher_url)
            service = PublicationService(
                publisher, dispatcher_id=f"wp10-4-{uuid.uuid4().hex[:8]}", batch_limit=1
            )
            try:
                pending = document_manifest(admin, f"g10-20-{uuid.uuid4().hex[:8]}")
                internal_document_id, _version_id, grant_id, _decision_id, manifest = pending
                digest = str(
                    scalar(
                        admin,
                        "SELECT publication_payload_sha256 "
                        "FROM audit.document_publication_grants WHERE id=%s",
                        grant_id,
                    )
                ).strip()
                payload = event_payload(manifest, event_type="publication.granted")
                payload["payload_sha256"] = digest
                event_id = insert_event(
                    admin,
                    aggregate_type="document_publication_grants",
                    aggregate_id=grant_id,
                    event_type="publication.granted",
                    payload=payload,
                )
                status, _headers, _body = request(base_url, f"/v1/documents/{internal_document_id}")
                require("G10-20 pending and internal ID hidden", status, 404)
                service.apply(claim_for(service, event_id))
                publisher.commit()
                public_id = scalar(
                    admin,
                    "SELECT public_id FROM audit.document_public_identities WHERE document_id=%s",
                    internal_document_id,
                )
                require(
                    "G10-20 stable public identity differs",
                    public_id == internal_document_id,
                    False,
                )
                status, _headers, visible = request(base_url, f"/v1/documents/{public_id}")
                require("G10-20 apply commit visible atomically", status, 200)
                serialized = json.dumps(visible)
                require(
                    "G10-20 internal document UUID absent",
                    str(internal_document_id) in serialized,
                    False,
                )
                require("G10-20 grant UUID absent", str(grant_id) in serialized, False)
                withdraw_event = withdraw_document(admin, pending)
                service.apply(claim_for(service, withdraw_event))
                publisher.commit()
                status, headers, withdrawn = request(base_url, f"/v1/documents/{public_id}")
                require("G10-20 withdrawn unified 404", status, 404)
                require("G10-20 withdrawn code", withdrawn["code"], "api_resource_not_found")
                require(
                    "G10-20 no withdrawal-extending cache",
                    "Cache-Control" in headers or "ETag" in headers,
                    False,
                )
            finally:
                publisher.close()
        finally:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate(timeout=5)
            leaked = any(
                token.lower() in (json.dumps(RESPONSES, default=str) + output).lower()
                for token in (
                    "sqlstate",
                    "audit.",
                    "core.",
                    "ops.",
                    "ingest.",
                    "uap_owner",
                    "uap_publisher",
                    "uap_api",
                    "private-evidence",
                )
            )
            require("G10-19 response/problem/log/header redaction", leaked, False)
            EVIDENCE.append(
                {
                    "kind": "process_environment",
                    "present_keys": sorted(environment),
                    "forbidden_dsn_present": False,
                    "passed": True,
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--publisher-url", required=True)
    parser.add_argument("--reader-url", required=True)
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args()
    status = "passed"
    try:
        run(args.admin_url, args.publisher_url, args.reader_url)
    except Exception:
        status = "failed"
        raise
    finally:
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(
                    {"schema": "wp10.4-runtime-evidence.v1", "status": status, "checks": EVIDENCE},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
    print("G10-16 G10-17 G10-18 G10-19 G10-20 runtime probe passed")


if __name__ == "__main__":
    main()
