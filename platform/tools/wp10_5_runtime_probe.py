"""Real Admin API process probe for G10-21 through G10-24 and W01-W11."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import make_conninfo

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
for path in (str(PLATFORM_ROOT), str(PLATFORM_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from tools.wp8_1_runtime_probe import (  # noqa: E402
    insert_analysis,
    insert_candidate_with_evidence,
    insert_model_run,
    insert_span,
    seed_document,
    sha256_text,
)
from tools.wp9_2_runtime_probe import GRANTOR_ID, execute, seed_grantor  # noqa: E402
from tools.wp10_2_migration_probe import (  # noqa: E402
    create_database,
    database_url,
    drop_database,
    libpq_url,
    run_alembic,
)


def _probable_prime(value: int) -> bool:
    if value < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31):
        if value == small:
            return True
        if value % small == 0:
            return False
    exponent = value - 1
    zeros = 0
    while exponent % 2 == 0:
        exponent //= 2
        zeros += 1
    for _ in range(8):
        base = secrets.randbelow(value - 3) + 2
        witness = pow(base, exponent, value)
        if witness in (1, value - 1):
            continue
        for _ in range(zeros - 1):
            witness = pow(witness, 2, value)
            if witness == value - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _probable_prime(candidate):
            return candidate


class _RSAPublicNumbers(Protocol):
    n: int
    e: int


class _RSAPrivateNumbers(Protocol):
    d: int
    public_numbers: _RSAPublicNumbers


class _RSAPrivateKey(Protocol):
    def private_numbers(self) -> _RSAPrivateNumbers: ...


class _RSAModule(Protocol):
    def generate_private_key(self, *, public_exponent: int, key_size: int) -> _RSAPrivateKey: ...


def _runtime_rsa() -> tuple[int, int, int]:
    try:
        rsa = cast(
            _RSAModule,
            importlib.import_module("cryptography.hazmat.primitives.asymmetric.rsa"),
        )
    except ImportError:
        public_exponent = 65537
        while True:
            first = _prime(1024)
            second = _prime(1024)
            if first == second:
                continue
            totient = (first - 1) * (second - 1)
            if totient % public_exponent == 0:
                continue
            modulus = first * second
            private_exponent = pow(public_exponent, -1, totient)
            return modulus, public_exponent, private_exponent
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.private_numbers()
    return numbers.public_numbers.n, numbers.public_numbers.e, numbers.d


N, E, D = _runtime_rsa()

ISSUER = "https://issuer.test/wp10.5"
AUDIENCE = "uap-admin"
KID = "wp10-5-runtime"
CURSOR_KEY = "wp10-5-runtime-cursor-secret-32-bytes"
DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
REASON = "runtime probe reason text"
EVIDENCE: list[dict[str, Any]] = []
MATRIX: list[dict[str, Any]] = []
RESPONSES: list[dict[str, Any]] = []
_RESPONSES_LOCK = threading.Lock()
ROLE_PASSWORD_ENV = {
    "uap_api": "UAP_API_PASSWORD",
    "uap_publisher": "UAP_PUBLISHER_PASSWORD",
    "uap_worker": "UAP_WORKER_PASSWORD",
    "uap_public_reader": "UAP_PUBLIC_READER_PASSWORD",
}


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def jwk() -> dict[str, object]:
    def encode_int(value: int) -> str:
        size = (value.bit_length() + 7) // 8
        return _b64(value.to_bytes(size, "big"))

    return {
        "kty": "RSA",
        "kid": KID,
        "alg": "RS256",
        "use": "sig",
        "n": encode_int(N),
        "e": encode_int(E),
    }


def mint(
    subject: str,
    *,
    iss: str = ISSUER,
    aud: object = AUDIENCE,
    exp: int | None = None,
    nbf: int | None = None,
    alg: str = "RS256",
    kid: str | None = KID,
) -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": iss,
        "aud": aud,
        "sub": subject,
        "exp": now + 3600 if exp is None else exp,
    }
    if nbf is not None:
        payload["nbf"] = nbf
    encoded_header = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
    encoded_payload = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    size = (N.bit_length() + 7) // 8
    digest = hashlib.sha256(signing_input).digest()
    pad_length = size - len(DIGEST_INFO) - len(digest) - 3
    encoded = b"\x00\x01" + (b"\xff" * pad_length) + b"\x00" + DIGEST_INFO + digest
    signature = pow(int.from_bytes(encoded, "big"), D, N).to_bytes(size, "big")
    return f"{encoded_header}.{encoded_payload}.{_b64(signature)}"


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


def record_matrix(instance_id: str, **fields: object) -> None:
    MATRIX.append({"id": instance_id, **fields})
    if fields.get("passed") is not True:
        raise RuntimeError(f"{instance_id} failed: {fields}")


def connect_role(url: str, role: str) -> psycopg.Connection[Any]:
    password = os.environ.get(ROLE_PASSWORD_ENV[role])
    if not password:
        raise RuntimeError(f"missing password for {role}")
    connection = psycopg.connect(make_conninfo(libpq_url(url), user=role, password=password))
    connection.autocommit = True
    return connection


def scalar(connection: psycopg.Connection[Any], statement: str, *params: object) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("probe query returned no row")
    return row[0]


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    idempotency_key: str | None = None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    request_id: str | None = None,
    barrier: threading.Barrier | None = None,
    flight: dict[str, list[float]] | None = None,
) -> tuple[int, dict[str, str], Any]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    if extra_headers:
        headers.update(extra_headers)
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    parsed = urlsplit(base_url)
    host = parsed.hostname
    if host is None:
        raise RuntimeError("probe URL is missing a hostname")
    connection = http.client.HTTPConnection(
        host, parsed.port, timeout=60 if barrier is not None else 10
    )
    try:
        if barrier is not None:
            barrier.wait(timeout=15)
            if flight is not None:
                flight.setdefault("starts", []).append(time.monotonic())
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        status = response.status
        response_headers = {name: value for name, value in response.getheaders()}
        raw = response.read()
        parsed_body: Any
        try:
            parsed_body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed_body = raw.decode("utf-8", "replace")
    finally:
        if flight is not None and barrier is not None:
            flight.setdefault("ends", []).append(time.monotonic())
        connection.close()
    with _RESPONSES_LOCK:
        RESPONSES.append({"method": method, "path": path, "status": status, "body": parsed_body})
    leaked = (
        json.dumps(parsed_body, default=str) if not isinstance(parsed_body, str) else parsed_body
    )
    if any(token in leaked for token in ("SQLSTATE", "pg_catalog", "password", "BEGIN RSA")):
        raise RuntimeError("response leaked sensitive content")
    return status, response_headers, parsed_body


def wait_ready(base_url: str, process: subprocess.Popen[str]) -> None:
    for _ in range(50):
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(f"admin API exited early: {output[-500:]}")
        try:
            status, _headers, body = request(base_url, "GET", "/healthz")
            if status == 200 and body == {"status": "ok"}:
                return
        except (TimeoutError, ConnectionRefusedError, OSError):
            time.sleep(0.1)
            continue
        time.sleep(0.1)
    raise RuntimeError("admin API did not become ready")


def insert_principal(
    admin: psycopg.Connection[Any],
    *,
    subject: str,
    role: str | None,
    active: bool = True,
    principal_type: str = "person",
) -> uuid.UUID:
    principal_id = uuid.uuid4()
    if principal_type == "person":
        execute(
            admin,
            """
            INSERT INTO audit.principals (
                id, principal_type, issuer, subject, display_name, active
            ) VALUES (%s, 'person', %s, %s, %s, %s)
            """,
            principal_id,
            ISSUER,
            subject,
            subject,
            active,
        )
    else:
        execute(
            admin,
            """
            INSERT INTO audit.principals (
                id, principal_type, service_name, display_name, active
            ) VALUES (%s, 'service', %s, %s, %s)
            """,
            principal_id,
            subject,
            subject,
            active,
        )
    if role is not None:
        execute(
            admin,
            """
            INSERT INTO audit.role_bindings (
                id, principal_id, role, scope_type, scope_id, reason, granted_by, granted_at
            ) VALUES (
                %s, %s, %s::audit.application_role, 'global', NULL, 'wp10.5 probe',
                %s, clock_timestamp()
            )
            """,
            uuid.uuid4(),
            principal_id,
            role,
            GRANTOR_ID,
        )
    return principal_id


_SNAPSHOT_SQL = {
    "review_cases": "SELECT t::text FROM audit.review_cases AS t",
    "audit_events": "SELECT t::text FROM audit.audit_events AS t",
    "decisions": "SELECT t::text FROM audit.review_decisions AS t",
    "document_grants": "SELECT t::text FROM audit.document_publication_grants AS t",
    "claim_grants": "SELECT t::text FROM audit.claim_publication_grants AS t",
    "entity_grants": "SELECT t::text FROM audit.entity_publication_grants AS t",
    "document_manifests": "SELECT t::text FROM audit.document_publication_manifests AS t",
    "claim_manifests": "SELECT t::text FROM audit.claim_publication_manifests AS t",
    "entity_manifests": "SELECT t::text FROM audit.entity_publication_manifests AS t",
    "outbox": "SELECT t::text FROM ops.outbox_events AS t",
    "entities": "SELECT t::text FROM core.entities AS t",
    "claims": "SELECT t::text FROM core.claims AS t",
    "merge_events": "SELECT t::text FROM core.entity_merge_events AS t",
    "selections": "SELECT t::text FROM core.analysis_selections AS t",
    "candidates": "SELECT t::text FROM core.entity_candidates AS t",
    "candidate_links": "SELECT t::text FROM core.entity_candidate_evidence AS t",
    "evidence": "SELECT t::text FROM core.claim_evidence AS t",
    "quarantine": "SELECT t::text FROM audit.publication_quarantine AS t",
}

_W_OBSERVE = {
    "G10-W01": ("review_cases", "audit_events"),
    "G10-W02": ("review_cases", "audit_events"),
    "G10-W03": ("review_cases", "audit_events"),
    "G10-W04": (
        "decisions",
        "review_cases",
        "document_grants",
        "claim_grants",
        "entity_grants",
        "grants",
        "document_manifests",
        "claim_manifests",
        "entity_manifests",
        "manifests",
        "outbox",
        "audit_events",
    ),
    "G10-W05": ("selections", "audit_events"),
    "G10-W06": ("entities", "candidates", "audit_events"),
    "G10-W07": ("candidates", "candidate_links", "audit_events"),
    "G10-W08": ("entities", "merge_events", "audit_events"),
    "G10-W09": ("entities", "merge_events", "audit_events"),
    "G10-W10": ("claims", "evidence", "audit_events"),
    "G10-W11": ("outbox", "quarantine", "audit_events"),
}

_W_TRIGGER = {
    "G10-W01": ("audit.review_cases", "audit.audit_events"),
    "G10-W02": ("audit.review_cases", "audit.audit_events"),
    "G10-W03": ("audit.review_cases", "audit.audit_events"),
    "G10-W04": (
        "audit.review_decisions",
        "audit.review_cases",
        "audit.document_publication_grants",
        "audit.claim_publication_grants",
        "audit.entity_publication_grants",
        "audit.document_publication_manifests",
        "audit.claim_publication_manifests",
        "audit.entity_publication_manifests",
        "ops.outbox_events",
        "audit.audit_events",
    ),
    "G10-W05": ("core.analysis_selections", "audit.audit_events"),
    "G10-W06": ("core.entities", "core.entity_candidates", "audit.audit_events"),
    "G10-W07": (
        "core.entity_candidates",
        "core.entity_candidate_evidence",
        "audit.audit_events",
    ),
    "G10-W08": ("core.entities", "core.entity_merge_events", "audit.audit_events"),
    "G10-W09": ("core.entities", "core.entity_merge_events", "audit.audit_events"),
    "G10-W10": ("core.claims", "core.claim_evidence", "audit.audit_events"),
    "G10-W11": (
        "ops.outbox_events",
        "audit.publication_quarantine",
        "audit.audit_events",
    ),
}

_TRIGGER_TABLES = {table for tables in _W_TRIGGER.values() for table in tables}


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("snapshot count is not an integer")
    return value


def snapshot(admin: psycopg.Connection[Any]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    with admin.cursor() as cursor:
        for name, statement in _SNAPSHOT_SQL.items():
            cursor.execute(statement)
            rows = sorted(row[0] for row in cursor.fetchall())
            digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
            result[name] = {"count": len(rows), "digest": digest}

    def _combined(*names: str) -> dict[str, object]:
        count = sum(_as_int(result[name]["count"]) for name in names)
        digest = hashlib.sha256(
            "".join(str(result[name]["digest"]) for name in names).encode("ascii")
        ).hexdigest()
        return {"count": count, "digest": digest}

    result["grants"] = _combined("document_grants", "claim_grants", "entity_grants")
    result["manifests"] = _combined("document_manifests", "claim_manifests", "entity_manifests")
    return result


def counts(admin: psycopg.Connection[Any]) -> dict[str, int]:
    return {name: _as_int(item["count"]) for name, item in snapshot(admin).items()}


def _observed(prefix: str, snap: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {name: snap[name] for name in _W_OBSERVE[prefix]}


def _unchanged(before: dict[str, dict[str, object]], after: dict[str, dict[str, object]]) -> bool:
    return before == after


def _request_audits(admin: psycopg.Connection[Any], request_id: str | None) -> int:
    if not request_id:
        return 0
    return int(
        scalar(
            admin,
            "SELECT count(*) FROM audit.audit_events WHERE request_id = %s",
            uuid.UUID(str(request_id)),
        )
    )


def _s_ok(
    prefix: str,
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
    admin: psycopg.Connection[Any],
    request_id: str | None,
) -> bool:
    # Frozen WP9 merge/reverse also insert a core audit without request_id.
    # The HTTP request itself must still create exactly one GUC-scoped audit event.
    if _request_audits(admin, request_id) != 1:
        return False
    if prefix == "G10-W01":
        return (
            _as_int(after["review_cases"]["count"]) - _as_int(before["review_cases"]["count"]) == 1
        )
    if prefix in {"G10-W02", "G10-W03"}:
        return (
            _as_int(after["review_cases"]["count"]) == _as_int(before["review_cases"]["count"])
            and after["review_cases"]["digest"] != before["review_cases"]["digest"]
        )
    if prefix == "G10-W04":
        return (
            _as_int(after["decisions"]["count"]) - _as_int(before["decisions"]["count"]) == 1
            and _as_int(after["grants"]["count"]) - _as_int(before["grants"]["count"]) == 1
            and _as_int(after["manifests"]["count"]) - _as_int(before["manifests"]["count"]) == 1
            and _as_int(after["outbox"]["count"]) - _as_int(before["outbox"]["count"]) == 1
        )
    if prefix == "G10-W05":
        return _as_int(after["selections"]["count"]) - _as_int(before["selections"]["count"]) == 1
    if prefix == "G10-W06":
        return (
            _as_int(after["entities"]["count"]) - _as_int(before["entities"]["count"]) == 1
            and after["candidates"]["digest"] != before["candidates"]["digest"]
        )
    if prefix == "G10-W07":
        return after["candidates"]["digest"] != before["candidates"]["digest"]
    if prefix == "G10-W08":
        return (
            _as_int(after["merge_events"]["count"]) - _as_int(before["merge_events"]["count"]) == 1
            and after["entities"]["digest"] != before["entities"]["digest"]
        )
    if prefix == "G10-W09":
        merge_changed = after["merge_events"] != before["merge_events"]
        return merge_changed and after["entities"]["digest"] != before["entities"]["digest"]
    if prefix == "G10-W10":
        return (
            _as_int(after["claims"]["count"]) - _as_int(before["claims"]["count"]) == 1
            and _as_int(after["evidence"]["count"]) - _as_int(before["evidence"]["count"]) >= 1
        )
    if prefix == "G10-W11":
        return after["outbox"]["digest"] != before["outbox"]["digest"]
    return False


def seed_world(admin: psycopg.Connection[Any]) -> dict[str, Any]:
    seed_grantor(admin)
    reviewer = insert_principal(admin, subject="reviewer-sub", role="reviewer")
    senior = insert_principal(admin, subject="senior-sub", role="senior_reviewer")
    operator = insert_principal(admin, subject="operator-sub", role="data_operator")
    inactive = insert_principal(admin, subject="inactive-sub", role="reviewer", active=False)
    insert_principal(admin, subject="service-bot", role=None, principal_type="service")
    _principal, document_version_id, _source = seed_document(admin, "wp10-5-doc")
    execute(
        admin,
        "UPDATE core.document_versions SET source_published_at = %s WHERE id = %s",
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        document_version_id,
    )
    _p2, claim_document_id, _s2 = seed_document(admin, "wp10-5-claim-doc")
    execute(
        admin,
        "UPDATE core.document_versions SET source_published_at = %s WHERE id = %s",
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        claim_document_id,
    )
    entity_id = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'WP10.5 Entity', 'active')
        """,
        entity_id,
    )
    merge_source = uuid.uuid4()
    merge_target = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'WP10.5 Source', 'active'),
               (%s, 'person', 'WP10.5 Target', 'active')
        """,
        merge_source,
        merge_target,
    )
    span_id = insert_span(admin, document_version_id, sha256_text("wp10-5-span"))
    run = insert_model_run(
        admin,
        document_version_id=document_version_id,
        task_type="entity_extraction",
        input_sha256=sha256_text("wp10-5-run"),
        tag="wp10-5-run",
    )
    analysis_id = insert_analysis(
        admin,
        model_run_id=run,
        document_version_id=document_version_id,
        result_type="entity_extraction",
        result={"entities": [{"name": "WP10.5 Entity"}]},
    )
    candidate_id, _link = insert_candidate_with_evidence(
        admin, analysis_id, document_version_id, 0, "WP10.5 Candidate", span_id
    )
    admin.autocommit = True
    claim_run = insert_model_run(
        admin,
        document_version_id=claim_document_id,
        task_type="claim_extraction",
        input_sha256=sha256_text("wp10-5-claim-run"),
        tag="wp10-5-claim-run",
    )
    claim_analysis = insert_analysis(
        admin,
        model_run_id=claim_run,
        document_version_id=claim_document_id,
        result_type="claim_extraction",
        result={"claims": [{"text": "a claim"}]},
    )
    return {
        "reviewer": reviewer,
        "senior": senior,
        "operator": operator,
        "inactive": inactive,
        "document_version_id": document_version_id,
        "claim_document_id": claim_document_id,
        "entity_id": entity_id,
        "merge_source": merge_source,
        "merge_target": merge_target,
        "analysis_id": analysis_id,
        "claim_analysis": claim_analysis,
        "candidate_id": candidate_id,
        "span_id": span_id,
    }


def g10_21(base_url: str) -> None:
    status, _headers, body = request(base_url, "GET", "/admin/v1/review-cases")
    require("G10-21 missing token", (status, body["code"]), (401, "api_auth_required"))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", extra_headers={"Cookie": "session=1"}
    )
    require("G10-21 cookie not bypass", (status, body["code"]), (401, "api_auth_required"))
    status, _headers, body = request(
        base_url,
        "GET",
        "/admin/v1/review-cases",
        token="not-a-jwt",  # noqa: S106
    )
    require("G10-21 malformed token", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("reviewer-sub", alg="HS256")
    )
    require("G10-21 alg confusion", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url,
        "GET",
        "/admin/v1/review-cases",
        token=mint("reviewer-sub", iss="https://evil.test/issuer"),
    )
    require("G10-21 wrong issuer", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("reviewer-sub", aud="other")
    )
    require("G10-21 wrong audience", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("reviewer-sub", exp=1)
    )
    require("G10-21 expired", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url,
        "GET",
        "/admin/v1/review-cases",
        token=mint("reviewer-sub", nbf=int(time.time()) + 3600),
    )
    require("G10-21 nbf", (status, body["code"]), (401, "api_token_invalid"))
    token = mint("reviewer-sub")
    header, payload, signature = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    raw[0] ^= 0xFF
    mutated = _b64(bytes(raw))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=f"{header}.{payload}.{mutated}"
    )
    require("G10-21 bad signature", (status, body["code"]), (401, "api_token_invalid"))
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("missing-sub")
    )
    require(
        "G10-21 unknown principal",
        (status, body["code"]),
        (403, "api_principal_not_provisioned"),
    )
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("inactive-sub")
    )
    require(
        "G10-21 inactive principal",
        (status, body["code"]),
        (403, "api_principal_not_provisioned"),
    )
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/review-cases", token=mint("service-bot")
    )
    require(
        "G10-21 service principal",
        (status, body["code"]),
        (403, "api_principal_not_provisioned"),
    )


def g10_22(base_url: str, world: dict[str, Any], reviewer: str, senior: str, operator: str) -> None:
    status, _headers, body = request(base_url, "GET", "/admin/v1/review-cases", token=reviewer)
    require("G10-22 reviewer cases", status, 200)
    require("G10-22 no raw model", "result" not in json.dumps(body), True)
    status, _headers, body = request(
        base_url,
        "GET",
        f"/admin/v1/analysis-results?document_version_id={world['document_version_id']}",
        token=reviewer,
    )
    require("G10-22 analysis", status, 200)
    dumped = json.dumps(body)
    require("G10-22 no model I/O field", "payload" not in dumped, True)
    status, _headers, body = request(
        base_url,
        "GET",
        f"/admin/v1/evidence-spans?document_version_id={world['document_version_id']}",
        token=reviewer,
    )
    require("G10-22 evidence spans", status, 200)
    require("G10-22 no evidence_text", "evidence_text" not in json.dumps(body), True)
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/publication-events", token=reviewer
    )
    require("G10-22 reviewer publication denied", status, 403)
    status, _headers, body = request(base_url, "GET", "/admin/v1/publication-events", token=senior)
    require("G10-22 senior publication read", status, 200)
    status, _headers, body = request(
        base_url, "GET", "/admin/v1/publication-events", token=operator
    )
    require("G10-22 operator publication read", status, 200)
    status, _headers, body = request(base_url, "GET", "/admin/v1/review-cases", token=operator)
    require("G10-22 operator cases denied", status, 403)
    grants = request(base_url, "GET", "/admin/v1/grants", token=reviewer)
    require("G10-22 no standalone grant route", grants[2]["code"], "api_capability_closed")


def g10_23(base_url: str, reviewer: str) -> None:
    status, headers, body = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        body={"case_type": "document", "subject_id": str(uuid.uuid4()), "reason": REASON},
    )
    require("G10-23 missing key", (status, body["code"]), (400, "review_request_id_missing"))
    require(
        "G10-23 request id header present",
        "X-Request-ID" in headers or "x-request-id" in {k.lower() for k in headers},
        True,
    )
    status, _headers, body = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        extra_headers={"Cookie": "session=yes"},
        body={"case_type": "document", "subject_id": str(uuid.uuid4()), "reason": REASON},
    )
    require("G10-23 cookie still missing key", body["code"], "review_request_id_missing")
    status, _headers, body = request(
        base_url,
        "GET",
        "/admin/v1/review-cases?case_type=document_version",
        token=reviewer,
    )
    require("G10-23 legacy enum", (status, body["code"]), (422, "api_request_invalid"))
    status, _headers, body = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        idempotency_key="not-a-uuid",
        body={"case_type": "document", "subject_id": str(uuid.uuid4()), "reason": REASON},
    )
    require("G10-23 invalid key", (status, body["code"]), (400, "review_request_id_invalid"))
    status, _headers, body = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        idempotency_key=str(uuid.uuid4()),
        body={"case_type": "relation", "subject_id": str(uuid.uuid4()), "reason": REASON},
    )
    require(
        "G10-23 relation closed",
        (status, body["code"]),
        (404, "api_capability_closed"),
    )
    status, _headers, body = request(
        base_url,
        "GET",
        f"/admin/v1/review-cases/{uuid.uuid4()}",
        token=reviewer,
    )
    require("G10-23 missing case", (status, body["code"]), (404, "api_resource_not_found"))


def g10_24(
    admin: psycopg.Connection[Any],
    api: psycopg.Connection[Any],
    senior: str,
    base_url: str,
    world: dict[str, Any],
) -> None:
    probes: list[tuple[str, str, tuple[object, ...]]] = [
        (
            "core.merge_entities",
            "SELECT core.merge_entities(%s, %s, %s, %s)",
            (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), REASON),
        ),
        (
            "core.reverse_entity_merge",
            "SELECT core.reverse_entity_merge(%s, %s, %s)",
            (uuid.uuid4(), uuid.uuid4(), REASON),
        ),
        (
            "ops.enqueue_publication_outbox",
            "SELECT ops.enqueue_publication_outbox(%s, %s, %s, %s, %s::jsonb)",
            ("publication.document.v2", "k", "document", uuid.uuid4(), "{}"),
        ),
        (
            "audit._apply_publication_grant",
            "SELECT audit._apply_publication_grant("
            "%s::audit.review_case_type, %s, %s, %s, %s::audit.review_decision)",
            ("document", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "approve"),
        ),
        (
            "audit._document_publication_payload",
            "SELECT audit._document_publication_payload(%s, %s, %s, %s, %s::jsonb)",
            (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), 1, "{}"),
        ),
        (
            "ops._apply_publication_event_wp10_2",
            "SELECT ops._apply_publication_event_wp10_2(%s, %s)",
            (uuid.uuid4(), uuid.uuid4()),
        ),
        ("public INSERT", "INSERT INTO public.documents (id) VALUES (%s)", (uuid.uuid4(),)),
        ("public UPDATE", "UPDATE public.documents SET title = %s WHERE false", ("x",)),
        ("public DELETE", "DELETE FROM public.documents WHERE false", ()),
    ]
    for name, statement, params in probes:
        try:
            with api.cursor() as cursor:
                cursor.execute(statement, params)
            require(f"G10-24 {name} denied", False, True)
        except psycopg.Error as error:
            require(f"G10-24 {name}", error.sqlstate, "42501")
    key = str(uuid.uuid4())
    status, _headers, body = request(
        base_url,
        "POST",
        "/admin/v1/entities/merges",
        token=senior,
        idempotency_key=key,
        body={
            "source_entity_id": str(world["merge_source"]),
            "target_entity_id": str(world["merge_target"]),
            "reason": REASON,
        },
    )
    require("G10-24 W08 wrapper", status, 200)
    actor = scalar(
        admin,
        """
        SELECT actor_id FROM audit.audit_events
         WHERE target_id = %s
         ORDER BY occurred_at DESC LIMIT 1
        """,
        uuid.UUID(body["resource_id"]),
    )
    require("G10-24 merge actor is GUC principal", actor, world["senior"])
    reverse = request(
        base_url,
        "POST",
        f"/admin/v1/entities/merge-events/{body['resource_id']}/reverse",
        token=senior,
        idempotency_key=str(uuid.uuid4()),
        body={"reason": REASON},
    )
    require("G10-24 W09 wrapper", reverse[0], 200)


def write_matrix(
    base_url: str,
    admin: psycopg.Connection[Any],
    world: dict[str, Any],
    tokens: dict[str, str],
) -> None:
    reviewer = tokens["reviewer"]
    operator = tokens["operator"]

    def run_case(
        instance_id: str,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        token: str,
        key: str | None,
        expected_status: int,
        expected_code: str | None = None,
        expect_no_change: bool = False,
        match_resource: object | None = None,
    ) -> Any:
        prefix = instance_id.rsplit("-", 1)[0]
        kind = instance_id.rsplit("-", 1)[1]
        before = snapshot(admin)
        status, _headers, response = request(
            base_url,
            method,
            path,
            token=token,
            idempotency_key=key,
            body=body,
        )
        after = snapshot(admin)
        code = response.get("code") if isinstance(response, dict) else None
        resource_id = response.get("resource_id") if isinstance(response, dict) else None
        passed = status == expected_status and (expected_code is None or code == expected_code)
        if kind == "S":
            passed = passed and _s_ok(prefix, before, after, admin, key)
        elif kind == "R":
            passed = passed and _unchanged(before, after) and resource_id == match_resource
        elif expect_no_change or kind in {"P", "M", "C"}:
            passed = passed and _unchanged(before, after)
        record_matrix(
            instance_id,
            status=status,
            code=code,
            resource_id=resource_id,
            before=_observed(prefix, before),
            after=_observed(prefix, after),
            passed=passed,
        )
        return response

    # W01
    open_body = {
        "case_type": "document",
        "subject_id": str(world["document_version_id"]),
        "reason": REASON,
        "priority": 0,
    }
    key_s = str(uuid.uuid4())
    created = run_case(
        "G10-W01-S",
        "POST",
        "/admin/v1/review-cases",
        open_body,
        token=reviewer,
        key=key_s,
        expected_status=200,
    )
    case_id = created["resource_id"]
    run_case(
        "G10-W01-P",
        "POST",
        "/admin/v1/review-cases",
        open_body,
        token=operator,
        key=str(uuid.uuid4()),
        expected_status=403,
        expected_code="review_role_denied",
        expect_no_change=True,
    )
    run_case(
        "G10-W01-M",
        "POST",
        "/admin/v1/review-cases",
        open_body,
        token=reviewer,
        key=None,
        expected_status=400,
        expected_code="review_request_id_missing",
        expect_no_change=True,
    )
    run_case(
        "G10-W01-R",
        "POST",
        "/admin/v1/review-cases",
        open_body,
        token=reviewer,
        key=key_s,
        expected_status=200,
        expect_no_change=True,
        match_resource=case_id,
    )
    conflict = dict(open_body)
    conflict["reason"] = "a different reason text"
    run_case(
        "G10-W01-C",
        "POST",
        "/admin/v1/review-cases",
        conflict,
        token=reviewer,
        key=key_s,
        expected_status=409,
        expected_code="review_idempotency_payload_conflict",
        expect_no_change=True,
    )
    _inject_and_retry(
        "G10-W01-T",
        admin,
        base_url,
        "POST",
        "/admin/v1/review-cases",
        {"case_type": "entity", "subject_id": str(world["entity_id"]), "reason": REASON},
        reviewer,
        "audit.review_cases",
    )

    # W02 assign
    assign_body = {"assignee_id": str(world["senior"])}
    assign_path = f"/admin/v1/review-cases/{case_id}/assignment"
    key_s = str(uuid.uuid4())
    run_case(
        "G10-W02-S", "PUT", assign_path, assign_body, token=reviewer, key=key_s, expected_status=200
    )
    run_case(
        "G10-W02-P",
        "PUT",
        assign_path,
        assign_body,
        token=operator,
        key=str(uuid.uuid4()),
        expected_status=403,
        expect_no_change=True,
    )
    run_case(
        "G10-W02-M",
        "PUT",
        assign_path,
        assign_body,
        token=reviewer,
        key=None,
        expected_status=400,
        expected_code="review_request_id_missing",
        expect_no_change=True,
    )
    run_case(
        "G10-W02-R",
        "PUT",
        assign_path,
        assign_body,
        token=reviewer,
        key=key_s,
        expected_status=200,
        expect_no_change=True,
        match_resource=case_id,
    )
    run_case(
        "G10-W02-C",
        "PUT",
        assign_path,
        {"assignee_id": str(world["reviewer"])},
        token=reviewer,
        key=key_s,
        expected_status=409,
        expected_code="review_idempotency_payload_conflict",
        expect_no_change=True,
    )
    _principal, t_assign_doc, _source = seed_document(admin, f"wp10-5-w02t-{uuid.uuid4().hex[:8]}")
    execute(
        admin,
        "UPDATE core.document_versions SET source_published_at = %s WHERE id = %s",
        datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        t_assign_doc,
    )
    t_assign_open = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        idempotency_key=str(uuid.uuid4()),
        body={
            "case_type": "document",
            "subject_id": str(t_assign_doc),
            "reason": REASON,
        },
    )
    if t_assign_open[0] != 200:
        raise RuntimeError(f"W02-T fixture open failed: {t_assign_open[2]}")
    _inject_and_retry(
        "G10-W02-T",
        admin,
        base_url,
        "PUT",
        f"/admin/v1/review-cases/{t_assign_open[2]['resource_id']}/assignment",
        assign_body,
        reviewer,
        "audit.review_cases",
    )

    # Remaining writes follow the same S/P/M/R/C/T pattern with dedicated fixtures.
    _write_rest(base_url, admin, world, tokens, case_id)


def _inject_and_retry(
    instance_id: str,
    admin: psycopg.Connection[Any],
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any],
    token: str,
    table: str,
) -> None:
    prefix = instance_id.rsplit("-", 1)[0]
    relations = tuple(dict.fromkeys((*_W_TRIGGER[prefix], table)))
    unexpected = [item for item in relations if item not in _TRIGGER_TABLES]
    if unexpected:
        raise RuntimeError(f"refusing trigger on unexpected table {unexpected}")
    fail_key = str(uuid.uuid4())
    trigger = "wp10_5_probe_fail_trigger"
    execute(
        admin,
        """
        CREATE OR REPLACE FUNCTION public.wp10_5_probe_fail() RETURNS trigger
        LANGUAGE plpgsql AS $fail$
        BEGIN
            RAISE EXCEPTION 'probe_injected_failure';
        END
        $fail$;
        """,
    )
    for relation in relations:
        execute(admin, f"DROP TRIGGER IF EXISTS {trigger} ON {relation}")
        execute(
            admin,
            f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {relation} "
            "FOR EACH ROW EXECUTE FUNCTION public.wp10_5_probe_fail()",
        )
    before = snapshot(admin)
    status, _headers, _body = request(
        base_url, method, path, token=token, idempotency_key=fail_key, body=body
    )
    after_fail = snapshot(admin)
    for relation in relations:
        execute(admin, f"DROP TRIGGER IF EXISTS {trigger} ON {relation}")
    unchanged = _unchanged(before, after_fail)
    retry_before = snapshot(admin)
    retry = request(base_url, method, path, token=token, idempotency_key=fail_key, body=body)
    retry_after = snapshot(admin)
    retry_body = retry[2] if isinstance(retry[2], dict) else {}
    record_matrix(
        instance_id,
        status=status,
        retry_status=retry[0],
        retry_code=retry_body.get("code") if isinstance(retry_body, dict) else None,
        unchanged=unchanged,
        inject_tables=list(relations),
        before=_observed(prefix, before),
        after=_observed(prefix, after_fail),
        retry_before=_observed(prefix, retry_before),
        retry_after=_observed(prefix, retry_after),
        passed=(
            status >= 400
            and unchanged
            and retry[0] == 200
            and _s_ok(prefix, retry_before, retry_after, admin, fail_key)
        ),
    )


def _write_rest(
    base_url: str,
    admin: psycopg.Connection[Any],
    world: dict[str, Any],
    tokens: dict[str, str],
    assigned_case_id: str,
) -> None:
    reviewer = tokens["reviewer"]
    senior = tokens["senior"]
    operator = tokens["operator"]

    def six(
        prefix: str,
        method: str,
        path: str,
        body: dict[str, Any],
        *,
        token: str,
        denied_token: str,
        conflict_body: dict[str, Any],
        t_table: str,
        t_path: str | None = None,
        t_body: dict[str, Any] | None = None,
    ) -> Any:
        def _code(payload: object) -> object:
            return payload.get("code") if isinstance(payload, dict) else None

        def _resource(payload: object) -> object:
            return payload.get("resource_id") if isinstance(payload, dict) else None

        key = str(uuid.uuid4())
        before_s = snapshot(admin)
        created = request(base_url, method, path, token=token, idempotency_key=key, body=body)
        after_s = snapshot(admin)
        record_matrix(
            f"{prefix}-S",
            status=created[0],
            code=_code(created[2]),
            resource_id=_resource(created[2]),
            before=_observed(prefix, before_s),
            after=_observed(prefix, after_s),
            passed=created[0] == 200 and _s_ok(prefix, before_s, after_s, admin, key),
        )
        before_p = snapshot(admin)
        denied = request(
            base_url, method, path, token=denied_token, idempotency_key=str(uuid.uuid4()), body=body
        )
        after_p = snapshot(admin)
        record_matrix(
            f"{prefix}-P",
            status=denied[0],
            code=_code(denied[2]),
            before=_observed(prefix, before_p),
            after=_observed(prefix, after_p),
            passed=denied[0] == 403 and _unchanged(before_p, after_p),
        )
        before_m = snapshot(admin)
        missing = request(base_url, method, path, token=token, body=body)
        after_m = snapshot(admin)
        record_matrix(
            f"{prefix}-M",
            status=missing[0],
            code=_code(missing[2]),
            before=_observed(prefix, before_m),
            after=_observed(prefix, after_m),
            passed=(
                missing[0] == 400
                and _code(missing[2]) == "review_request_id_missing"
                and _unchanged(before_m, after_m)
            ),
        )
        before_r = snapshot(admin)
        replay = request(base_url, method, path, token=token, idempotency_key=key, body=body)
        after_r = snapshot(admin)
        record_matrix(
            f"{prefix}-R",
            status=replay[0],
            resource_id=_resource(replay[2]),
            before=_observed(prefix, before_r),
            after=_observed(prefix, after_r),
            passed=(
                replay[0] == 200
                and _resource(replay[2]) == _resource(created[2])
                and _unchanged(before_r, after_r)
            ),
        )
        before_c = snapshot(admin)
        conflict = request(
            base_url, method, path, token=token, idempotency_key=key, body=conflict_body
        )
        after_c = snapshot(admin)
        record_matrix(
            f"{prefix}-C",
            status=conflict[0],
            code=_code(conflict[2]),
            before=_observed(prefix, before_c),
            after=_observed(prefix, after_c),
            passed=(
                conflict[0] == 409
                and _code(conflict[2]) == "review_idempotency_payload_conflict"
                and _unchanged(before_c, after_c)
            ),
        )
        _inject_and_retry(
            f"{prefix}-T",
            admin,
            base_url,
            method,
            t_path or path,
            t_body or body,
            token,
            t_table,
        )
        return created[2]

    def fresh_document() -> uuid.UUID:
        _principal, document_id, _source = seed_document(admin, f"wp10-5-t-{uuid.uuid4().hex[:8]}")
        execute(
            admin,
            "UPDATE core.document_versions SET source_published_at = %s WHERE id = %s",
            datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
            document_id,
        )
        return document_id

    def open_http(subject_id: uuid.UUID, case_type: str = "document") -> str:
        opened = request(
            base_url,
            "POST",
            "/admin/v1/review-cases",
            token=reviewer,
            idempotency_key=str(uuid.uuid4()),
            body={"case_type": case_type, "subject_id": str(subject_id), "reason": REASON},
        )
        if opened[0] != 200:
            raise RuntimeError(f"failed to open T fixture case: {opened[2]}")
        return str(opened[2]["resource_id"])

    def reject_http(case_id: str) -> None:
        rejected = request(
            base_url,
            "POST",
            f"/admin/v1/review-cases/{case_id}/decisions",
            token=reviewer,
            idempotency_key=str(uuid.uuid4()),
            body={"decision": "reject", "reason": REASON, "structured_changes": {}},
        )
        if rejected[0] != 200:
            raise RuntimeError(f"failed to reject fixture case: {rejected[2]}")

    # Close is only legal after a terminal decision (approved/rejected/disputed/withdrawn).
    close_open = request(
        base_url,
        "POST",
        "/admin/v1/review-cases",
        token=reviewer,
        idempotency_key=str(uuid.uuid4()),
        body={
            "case_type": "document",
            "subject_id": str(world["claim_document_id"]),
            "reason": REASON,
        },
    )
    if close_open[0] != 200:
        raise RuntimeError(f"failed to open W03 case: {close_open[2]}")
    close_id = close_open[2]["resource_id"]
    reject_http(str(close_id))
    t_close_id = open_http(fresh_document())
    reject_http(t_close_id)
    six(
        "G10-W03",
        "POST",
        f"/admin/v1/review-cases/{close_id}/close",
        {"reason": "close after opening xx"},
        token=reviewer,
        denied_token=operator,
        conflict_body={"reason": "a conflicting close reason"},
        t_table="audit.review_cases",
        t_path=f"/admin/v1/review-cases/{t_close_id}/close",
        t_body={"reason": "close after opening yy"},
    )

    publication = {
        "decision": "approve",
        "reason": REASON,
        "structured_changes": {
            "publication": {
                "title": "WP10.5 runtime document",
                "summary": "Stable snapshot",
                "category": "official_report",
                "fact_status": "source_reported",
                "summary_analysis_result_id": None,
            }
        },
    }

    t_case = open_http(fresh_document())
    request(
        base_url,
        "PUT",
        f"/admin/v1/review-cases/{t_case}/assignment",
        token=reviewer,
        idempotency_key=str(uuid.uuid4()),
        body={"assignee_id": str(world["senior"])},
    )
    six(
        "G10-W04",
        "POST",
        f"/admin/v1/review-cases/{assigned_case_id}/decisions",
        publication,
        token=senior,
        denied_token=operator,
        conflict_body={**publication, "reason": "a conflicting decision reason"},
        t_table="audit.review_decisions",
        t_path=f"/admin/v1/review-cases/{t_case}/decisions",
        t_body=publication,
    )

    t_doc = fresh_document()
    t_run = insert_model_run(
        admin,
        document_version_id=t_doc,
        task_type="claim_extraction",
        input_sha256=sha256_text("wp10-5-t-claim"),
        tag="wp10-5-t-claim",
    )
    t_analysis = insert_analysis(
        admin,
        model_run_id=t_run,
        document_version_id=t_doc,
        result_type="claim_extraction",
        result={"claims": [{"text": "t"}]},
    )
    six(
        "G10-W05",
        "POST",
        f"/admin/v1/analysis-results/{world['claim_analysis']}/selection",
        {"reason": REASON},
        token=reviewer,
        denied_token=operator,
        conflict_body={"reason": "a conflicting select reason"},
        t_table="core.analysis_selections",
        t_path=f"/admin/v1/analysis-results/{t_analysis}/selection",
        t_body={"reason": REASON},
    )
    t_span = insert_span(admin, world["document_version_id"], sha256_text("wp10-5-t-span"))
    t_run_c = insert_model_run(
        admin,
        document_version_id=world["document_version_id"],
        task_type="entity_extraction",
        input_sha256=sha256_text("wp10-5-t-cand"),
        tag="wp10-5-t-cand",
    )
    t_an_c = insert_analysis(
        admin,
        model_run_id=t_run_c,
        document_version_id=world["document_version_id"],
        result_type="entity_extraction",
        result={"entities": [{"name": "T"}]},
    )
    t_candidate, _tlink = insert_candidate_with_evidence(
        admin, t_an_c, world["document_version_id"], 0, "T Candidate", t_span
    )
    admin.autocommit = True
    six(
        "G10-W06",
        "POST",
        f"/admin/v1/entity-candidates/{world['candidate_id']}/accept",
        {"reason": REASON},
        token=reviewer,
        denied_token=operator,
        conflict_body={"reason": "a conflicting accept reason"},
        t_table="core.entity_candidates",
        t_path=f"/admin/v1/entity-candidates/{t_candidate}/accept",
        t_body={"reason": REASON},
    )

    other_entity = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'WP10.5 Bind Target', 'active')
        """,
        other_entity,
    )
    # W07 needs a pending candidate; accept consumed the first. Seed another.
    span2 = insert_span(admin, world["document_version_id"], sha256_text("wp10-5-span-2"))
    run2 = insert_model_run(
        admin,
        document_version_id=world["document_version_id"],
        task_type="entity_extraction",
        input_sha256=sha256_text("wp10-5-run-2"),
        tag="wp10-5-run-2",
    )
    analysis2 = insert_analysis(
        admin,
        model_run_id=run2,
        document_version_id=world["document_version_id"],
        result_type="entity_extraction",
        result={"entities": [{"name": "Bind"}]},
    )
    candidate2, _link = insert_candidate_with_evidence(
        admin, analysis2, world["document_version_id"], 0, "Bind Candidate", span2
    )
    admin.autocommit = True
    span_t = insert_span(admin, world["document_version_id"], sha256_text("wp10-5-span-t7"))
    run_t = insert_model_run(
        admin,
        document_version_id=world["document_version_id"],
        task_type="entity_extraction",
        input_sha256=sha256_text("wp10-5-run-t7"),
        tag="wp10-5-run-t7",
    )
    analysis_t = insert_analysis(
        admin,
        model_run_id=run_t,
        document_version_id=world["document_version_id"],
        result_type="entity_extraction",
        result={"entities": [{"name": "BindT"}]},
    )
    candidate_t, _link_t = insert_candidate_with_evidence(
        admin, analysis_t, world["document_version_id"], 0, "Bind T Candidate", span_t
    )
    admin.autocommit = True
    bind_target_t = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'WP10.5 Bind Target T', 'active')
        """,
        bind_target_t,
    )
    six(
        "G10-W07",
        "POST",
        f"/admin/v1/entity-candidates/{candidate2}/bind",
        {"entity_id": str(other_entity), "reason": REASON},
        token=reviewer,
        denied_token=operator,
        conflict_body={"entity_id": str(world["entity_id"]), "reason": REASON},
        t_table="core.entity_candidates",
        t_path=f"/admin/v1/entity-candidates/{candidate_t}/bind",
        t_body={"entity_id": str(bind_target_t), "reason": REASON},
    )

    source = uuid.uuid4()
    target = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'Merge Source 2', 'active'),
               (%s, 'person', 'Merge Target 2', 'active')
        """,
        source,
        target,
    )
    t_source = uuid.uuid4()
    t_target = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'Merge Source T', 'active'),
               (%s, 'person', 'Merge Target T', 'active')
        """,
        t_source,
        t_target,
    )
    merge_body = {
        "source_entity_id": str(source),
        "target_entity_id": str(target),
        "reason": REASON,
    }
    created_merge = six(
        "G10-W08",
        "POST",
        "/admin/v1/entities/merges",
        merge_body,
        token=senior,
        denied_token=reviewer,
        conflict_body={**merge_body, "reason": "a conflicting merge reason"},
        t_table="core.entity_merge_events",
        t_body={
            "source_entity_id": str(t_source),
            "target_entity_id": str(t_target),
            "reason": REASON,
        },
    )
    merge_event_id = created_merge["resource_id"]
    t_rev_source = uuid.uuid4()
    t_rev_target = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'Reverse Source T', 'active'),
               (%s, 'person', 'Reverse Target T', 'active')
        """,
        t_rev_source,
        t_rev_target,
    )
    t_merge = request(
        base_url,
        "POST",
        "/admin/v1/entities/merges",
        token=senior,
        idempotency_key=str(uuid.uuid4()),
        body={
            "source_entity_id": str(t_rev_source),
            "target_entity_id": str(t_rev_target),
            "reason": REASON,
        },
    )
    if t_merge[0] != 200:
        raise RuntimeError(f"W09-T fixture merge failed: {t_merge[2]}")
    six(
        "G10-W09",
        "POST",
        f"/admin/v1/entities/merge-events/{merge_event_id}/reverse",
        {"reason": REASON},
        token=senior,
        denied_token=reviewer,
        conflict_body={"reason": "a conflicting reverse reason"},
        t_table="core.entity_merge_events",
        t_path=(f"/admin/v1/entities/merge-events/{t_merge[2]['resource_id']}/reverse"),
        t_body={"reason": REASON},
    )
    six(
        "G10-W10",
        "POST",
        "/admin/v1/claims/manual",
        {
            "document_version_id": str(world["document_version_id"]),
            "claim_text": "manual claim from WP10.5",
            "claim_type": "observation",
            "assertion_status": "reported",
            "attribution": None,
            "span_ids": [str(world["span_id"])],
        },
        token=reviewer,
        denied_token=operator,
        conflict_body={
            "document_version_id": str(world["document_version_id"]),
            "claim_text": "different manual claim text",
            "claim_type": "observation",
            "assertion_status": "reported",
            "attribution": None,
            "span_ids": [str(world["span_id"])],
        },
        t_table="core.claims",
    )

    t_replay_doc = fresh_document()
    t_replay_case = open_http(t_replay_doc)
    assigned = request(
        base_url,
        "PUT",
        f"/admin/v1/review-cases/{t_replay_case}/assignment",
        token=reviewer,
        idempotency_key=str(uuid.uuid4()),
        body={"assignee_id": str(world["senior"])},
    )
    if assigned[0] != 200:
        raise RuntimeError(f"W11-T fixture assign failed: {assigned[2]}")
    decided = request(
        base_url,
        "POST",
        f"/admin/v1/review-cases/{t_replay_case}/decisions",
        token=senior,
        idempotency_key=str(uuid.uuid4()),
        body=publication,
    )
    if decided[0] != 200:
        raise RuntimeError(f"W11-T fixture decide failed: {decided[2]}")
    event_id = scalar(
        admin,
        """
        SELECT id FROM ops.outbox_events
         WHERE event_type LIKE %s
         ORDER BY occurred_at ASC, id ASC LIMIT 1
        """,
        "publication.%",
    )
    t_event_id = scalar(
        admin,
        """
        SELECT id FROM ops.outbox_events
         WHERE event_type LIKE %s
         ORDER BY occurred_at DESC, id DESC LIMIT 1
        """,
        "publication.%",
    )
    execute(
        admin,
        """
        UPDATE ops.outbox_events
           SET terminal_at = clock_timestamp(),
               terminal_error_code = 'publication_retry_exhausted',
               published_at = NULL
         WHERE id IN (%s, %s)
        """,
        event_id,
        t_event_id,
    )
    grants_before_replay = counts(admin)["grants"]
    # HTTP replay maps to audit.requeue_publication_event; ids G10-W11-S .. G10-W11-T.
    six(
        "G10-W11",
        "POST",
        f"/admin/v1/publication-events/{event_id}/replay",
        {"reason": REASON},
        token=operator,
        denied_token=reviewer,
        conflict_body={"reason": "a conflicting replay reason"},
        t_table="ops.outbox_events",
        t_path=f"/admin/v1/publication-events/{t_event_id}/replay",
        t_body={"reason": REASON},
    )
    require(
        "W11 replay does not add grants",
        counts(admin)["grants"],
        grants_before_replay,
    )


def extra_w04_and_grants(
    base_url: str,
    admin: psycopg.Connection[Any],
    world: dict[str, Any],
    tokens: dict[str, str],
    w04_case_id: str,
) -> None:
    reviewer = tokens["reviewer"]
    senior = tokens["senior"]
    revise_one = {
        "decision": "revise",
        "reason": REASON,
        "structured_changes": {
            "publication": {
                "title": "WP10.5 revised document",
                "summary": "Revised snapshot",
                "category": "official_report",
                "fact_status": "source_reported",
                "summary_analysis_result_id": None,
            }
        },
    }
    revise_two = {
        "decision": "revise",
        "reason": "second revise request xx",
        "structured_changes": {
            "publication": {
                "title": "WP10.5 second revision",
                "summary": "Second snapshot",
                "category": "official_report",
                "fact_status": "source_reported",
                "summary_analysis_result_id": None,
            }
        },
    }

    def open_case(subject_id: object, case_type: str) -> str:
        opened = request(
            base_url,
            "POST",
            "/admin/v1/review-cases",
            token=reviewer,
            idempotency_key=str(uuid.uuid4()),
            body={"case_type": case_type, "subject_id": str(subject_id), "reason": REASON},
        )
        if opened[0] != 200:
            raise RuntimeError(f"extra open failed: {opened[2]}")
        assigned = request(
            base_url,
            "PUT",
            f"/admin/v1/review-cases/{opened[2]['resource_id']}/assignment",
            token=reviewer,
            idempotency_key=str(uuid.uuid4()),
            body={"assignee_id": str(world["senior"])},
        )
        if assigned[0] != 200:
            raise RuntimeError(f"extra assign failed: {assigned[2]}")
        return str(opened[2]["resource_id"])

    def decide(
        case_id: str,
        body: dict[str, Any],
        token: str,
        key: str | None = None,
        barrier: threading.Barrier | None = None,
        flight: dict[str, list[float]] | None = None,
    ) -> Any:
        return request(
            base_url,
            "POST",
            f"/admin/v1/review-cases/{case_id}/decisions",
            token=token,
            idempotency_key=key or str(uuid.uuid4()),
            body=body,
            barrier=barrier,
            flight=flight,
        )

    detail = request(base_url, "GET", f"/admin/v1/review-cases/{w04_case_id}", token=senior)
    require("G10-22 W04 case readable", detail[0], 200)
    publication_state = (detail[2] or {}).get("publication") or {}
    require("G10-22 grant committed", publication_state.get("grant_status"), "active")
    require("G10-22 projection pending", publication_state.get("projection_state"), "pending")
    require("G10-22 revision >= 1", int(publication_state.get("revision") or 0) >= 1, True)

    before_concurrent = snapshot(admin)
    first_key = str(uuid.uuid4())
    second_key = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    flight: dict[str, list[float]] = {"starts": [], "ends": []}
    results: list[Any] = [None, None]

    def _revise(index: int, payload: dict[str, Any], key: str) -> None:
        try:
            results[index] = decide(
                w04_case_id, payload, senior, key, barrier=barrier, flight=flight
            )
        except Exception as error:
            results[index] = error

    threads = [
        threading.Thread(target=_revise, args=(0, revise_one, first_key)),
        threading.Thread(target=_revise, args=(1, revise_two, second_key)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    require(
        "W04 concurrent threads finished", all(not thread.is_alive() for thread in threads), True
    )
    first, second = results
    require("W04 concurrent first is response", isinstance(first, tuple), True)
    require("W04 concurrent second is response", isinstance(second, tuple), True)
    require("W04 concurrent first status", first[0], 200)
    require("W04 concurrent second status", second[0], 200)
    starts = list(flight.get("starts") or [])
    ends = list(flight.get("ends") or [])
    require("W04 concurrent both requests started", len(starts), 2)
    require("W04 concurrent both requests ended", len(ends), 2)
    require("W04 concurrent in-flight overlap", max(starts) < min(ends), True)
    after_concurrent = snapshot(admin)
    rev_first = int(first[2]["publication"]["revision"])
    rev_second = int(second[2]["publication"]["revision"])
    require("W04 concurrent revisions differ", rev_first != rev_second, True)
    require(
        "W04 revision strictly increasing",
        max(rev_first, rev_second) > min(rev_first, rev_second),
        True,
    )
    active = scalar(
        admin,
        """
        SELECT count(*) FROM audit.document_publication_grants
         WHERE document_version_id = %s AND grant_status = 'active'
        """,
        world["document_version_id"],
    )
    require("W04 exactly one active document grant", int(active), 1)
    require(
        "W04 concurrent grant/manifest/outbox changed",
        (
            after_concurrent["grants"] != before_concurrent["grants"]
            or after_concurrent["manifests"] != before_concurrent["manifests"]
            or after_concurrent["outbox"] != before_concurrent["outbox"]
        ),
        True,
    )
    EVIDENCE.append(
        {
            "kind": "w04_concurrent",
            "name": "W04 concurrent different-request revise",
            "passed": True,
            "first_status": first[0],
            "second_status": second[0],
            "first_revision": rev_first,
            "second_revision": rev_second,
            "active_grants": int(active),
            "in_flight": True,
            "start_delta_ms": round((max(starts) - min(starts)) * 1000, 3),
            "overlap_ms": round((min(ends) - max(starts)) * 1000, 3),
            "before": {
                "grants": before_concurrent["grants"],
                "manifests": before_concurrent["manifests"],
                "outbox": before_concurrent["outbox"],
            },
            "after": {
                "grants": after_concurrent["grants"],
                "manifests": after_concurrent["manifests"],
                "outbox": after_concurrent["outbox"],
            },
        }
    )
    winner_thread = 0 if rev_first > rev_second else 1
    winner_revision = max(rev_first, rev_second)
    lock_order = f"{rev_first}-then-{rev_second}"
    current_active_grant = scalar(
        admin,
        """
        SELECT id::text FROM audit.document_publication_grants
         WHERE document_version_id = %s AND grant_status = 'active'
        """,
        world["document_version_id"],
    )
    replay_rows: list[dict[str, Any]] = []
    for thread_index, original, payload, key in (
        (0, first, revise_one, first_key),
        (1, second, revise_two, second_key),
    ):
        replay_before = snapshot(admin)
        replay = decide(w04_case_id, payload, senior, key)
        replay_after = snapshot(admin)
        require(f"W04 thread {thread_index} same-request replay status", replay[0], 200)
        require(
            f"W04 thread {thread_index} same-request replay same resource_id",
            replay[2]["resource_id"],
            original[2]["resource_id"],
        )
        require(
            f"W04 thread {thread_index} replay grant delta zero",
            replay_after["grants"],
            replay_before["grants"],
        )
        require(
            f"W04 thread {thread_index} replay manifest delta zero",
            replay_after["manifests"],
            replay_before["manifests"],
        )
        require(
            f"W04 thread {thread_index} replay outbox delta zero",
            replay_after["outbox"],
            replay_before["outbox"],
        )
        replay_publication = replay[2].get("publication") or {}
        replay_grant = replay_publication.get("grant_id")
        if replay_grant is not None:
            require(
                f"W04 thread {thread_index} replay publication is current active grant",
                str(replay_grant),
                str(current_active_grant),
            )
        replay_rows.append(
            {
                "thread": thread_index,
                "status": replay[0],
                "resource_id": replay[2].get("resource_id"),
                "original_resource_id": original[2].get("resource_id"),
                "publication_grant_id": replay_grant,
                "publication_revision": replay_publication.get("revision"),
                "original_publication_revision": original[2]["publication"]["revision"],
                "before": {
                    "grants": replay_before["grants"],
                    "manifests": replay_before["manifests"],
                    "outbox": replay_before["outbox"],
                },
                "after": {
                    "grants": replay_after["grants"],
                    "manifests": replay_after["manifests"],
                    "outbox": replay_after["outbox"],
                },
            }
        )
    active_after_replays = scalar(
        admin,
        """
        SELECT count(*) FROM audit.document_publication_grants
         WHERE document_version_id = %s AND grant_status = 'active'
        """,
        world["document_version_id"],
    )
    require(
        "W04 after both replays exactly one active document grant",
        int(active_after_replays),
        1,
    )
    EVIDENCE.append(
        {
            "kind": "w04_concurrent_replay",
            "name": "W04 concurrent different-request revise same-request replays",
            "passed": True,
            "first_revision": rev_first,
            "second_revision": rev_second,
            "winner_thread": winner_thread,
            "winner_revision": winner_revision,
            "lock_order": lock_order,
            "order_2_then_3": lock_order == "2-then-3",
            "order_3_then_2": lock_order == "3-then-2",
            "current_active_grant_id": current_active_grant,
            "in_flight": True,
            "start_delta_ms": round((max(starts) - min(starts)) * 1000, 3),
            "overlap_ms": round((min(ends) - max(starts)) * 1000, 3),
            "replays": replay_rows,
            "active_grants_after_replays": int(active_after_replays),
        }
    )

    claim_id = scalar(
        admin,
        "SELECT id FROM core.claims ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    claim_case = open_case(claim_id, "claim")
    claim_s = decide(
        claim_case,
        {
            "decision": "approve",
            "reason": REASON,
            "structured_changes": {"bind_subject_entity_id": str(world["entity_id"])},
        },
        senior,
    )
    require("W04 claim approve", claim_s[0], 200)
    claim_r = decide(
        claim_case,
        {
            "decision": "revise",
            "reason": REASON,
            "structured_changes": {
                "bind_subject_entity_id": str(world["entity_id"]),
                "replace_supporting_span_ids": [str(world["span_id"])],
            },
        },
        senior,
    )
    require("W04 claim revise", claim_r[0], 200)
    require(
        "W04 claim revision increasing",
        int(claim_r[2]["publication"]["revision"]) > int(claim_s[2]["publication"]["revision"]),
        True,
    )
    claim_w = decide(
        claim_case,
        {
            "decision": "withdraw",
            "reason": REASON,
            "structured_changes": {"retire_supporting_evidence": True},
        },
        senior,
    )
    require("W04 claim withdraw", claim_w[0], 200)
    require("W04 claim withdrawn", claim_w[2]["publication"]["grant_status"], "withdrawn")
    require(
        "W04 claim projection withdrawn",
        claim_w[2]["publication"]["projection_state"],
        "withdrawn",
    )

    extra_entity = uuid.uuid4()
    execute(
        admin,
        """
        INSERT INTO core.entities (id, entity_type, canonical_name, status)
        VALUES (%s, 'person', 'WP10.5 Extra Entity', 'active')
        """,
        extra_entity,
    )
    entity_case = open_case(extra_entity, "entity")
    entity_s = decide(
        entity_case,
        {"decision": "approve", "reason": REASON, "structured_changes": {}},
        senior,
    )
    require("W04 entity approve", entity_s[0], 200)
    entity_r = decide(
        entity_case,
        {"decision": "revise", "reason": REASON, "structured_changes": {}},
        senior,
    )
    require("W04 entity revise", entity_r[0], 200)
    require(
        "W04 entity revision increasing",
        int(entity_r[2]["publication"]["revision"]) > int(entity_s[2]["publication"]["revision"]),
        True,
    )
    entity_w = decide(
        entity_case,
        {"decision": "withdraw", "reason": REASON, "structured_changes": {}},
        senior,
    )
    require("W04 entity withdraw", entity_w[0], 200)
    require("W04 entity withdrawn", entity_w[2]["publication"]["grant_status"], "withdrawn")

    document_w = decide(
        w04_case_id,
        {"decision": "withdraw", "reason": REASON, "structured_changes": {}},
        senior,
    )
    require("W04 document withdraw", document_w[0], 200)
    require("W04 document withdrawn", document_w[2]["publication"]["grant_status"], "withdrawn")
    require(
        "W04 document projection withdrawn",
        document_w[2]["publication"]["projection_state"],
        "withdrawn",
    )


def run(admin_url: str) -> None:
    name = f"uap_wp10_5_admin_{uuid.uuid4().hex[:10]}"
    create_database(admin_url, name)
    isolated = database_url(admin_url, name)
    process: subprocess.Popen[str] | None = None
    admin: psycopg.Connection[Any] | None = None
    try:
        run_alembic(isolated, "upgrade", "head")
        admin = psycopg.connect(libpq_url(isolated), autocommit=True)
        world = seed_world(admin)
        api = connect_role(isolated, "uap_api")
        tokens = {
            "reviewer": mint("reviewer-sub"),
            "senior": mint("senior-sub"),
            "operator": mint("operator-sub"),
        }
        port = 38082
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(PLATFORM_ROOT / "src") + os.pathsep + str(PLATFORM_ROOT),
            "LANG": "C.UTF-8",
            "UAP_ADMIN_DATABASE_URL": make_conninfo(
                libpq_url(isolated),
                user="uap_api",
                password=os.environ["UAP_API_PASSWORD"],
            ),
            "UAP_ADMIN_CURSOR_SECRET": CURSOR_KEY,
            "UAP_ADMIN_OIDC_ISSUER": ISSUER,
            "UAP_ADMIN_OIDC_AUDIENCE": AUDIENCE,
            "UAP_ADMIN_OIDC_JWKS": json.dumps({"keys": [jwk()]}),
            "UAP_ADMIN_HOST": "127.0.0.1",
            "UAP_ADMIN_PORT": str(port),
        }
        # Fail closed if privileged DSNs leak into the child.
        for forbidden in (
            "UAP_DATABASE_URL",
            "UAP_PUBLIC_DATABASE_URL",
            "UAP_PUBLISHER_DATABASE_URL",
            "UAP_WORKER_DATABASE_URL",
        ):
            environment.pop(forbidden, None)
        process = subprocess.Popen(
            [sys.executable, "-m", "uap_platform.admin_api.server"],
            cwd=PLATFORM_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        wait_ready(base_url, process)
        g10_21(base_url)
        g10_22(base_url, world, tokens["reviewer"], tokens["senior"], tokens["operator"])
        g10_23(base_url, tokens["reviewer"])
        write_matrix(base_url, admin, world, tokens)
        w04_case = next(item["resource_id"] for item in MATRIX if item["id"] == "G10-W01-S")
        extra_w04_and_grants(base_url, admin, world, tokens, str(w04_case))
        g10_24(admin, api, tokens["senior"], base_url, world)
        api.close()
        admin.close()
        require("W matrix size", len(MATRIX), 66)
        require(
            "W matrix identities",
            {item["id"] for item in MATRIX},
            {
                f"G10-W{index:02d}-{kind}"
                for index in range(1, 12)
                for kind in ("S", "P", "M", "R", "C", "T")
            },
        )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if admin is not None and not admin.closed:
            admin.close()
        drop_database(admin_url, name)


def _admin_url() -> str:
    url = os.environ.get("UAP_WP10_5_ADMIN_URL")
    if not url:
        raise SystemExit("admin URL missing")
    return url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument("--matrix-out", type=Path)
    args = parser.parse_args()
    status = "passed"
    try:
        run(_admin_url())
    except Exception:
        status = "failed"
        raise
    finally:
        payload = {
            "schema": "wp10.5-runtime-evidence.v1",
            "status": status,
            "checks": EVIDENCE,
            "matrix_ids": [item["id"] for item in MATRIX],
        }
        if args.evidence_out:
            args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
            args.evidence_out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        if args.matrix_out:
            args.matrix_out.parent.mkdir(parents=True, exist_ok=True)
            args.matrix_out.write_text(
                json.dumps(
                    {
                        "schema": "wp10.5-w-matrix-evidence.v1",
                        "status": status,
                        "instances": MATRIX,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
    print("G10-21 G10-22 G10-23 G10-24 W01-W11 runtime probe passed")


if __name__ == "__main__":
    main()
