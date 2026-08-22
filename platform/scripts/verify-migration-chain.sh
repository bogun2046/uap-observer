#!/bin/sh
set -eu

platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
requested_project=${UAP_COMPOSE_PROJECT_NAME:-}
# shellcheck disable=SC1091
. "$platform_dir/.env"
if [ -n "$requested_project" ]; then
    UAP_COMPOSE_PROJECT_NAME=$requested_project
    export UAP_COMPOSE_PROJECT_NAME
fi

test_database=uap_wp3_migration_chain
failclosed_database=uap_wp8_claim_backfill
compose="docker compose --env-file $platform_dir/.env.versions --env-file $platform_dir/.env -f $platform_dir/compose.yaml"
database_url="postgresql+psycopg://${UAP_POSTGRES_USER}:${UAP_POSTGRES_PASSWORD}@postgres:5432/${test_database}"
failclosed_url="postgresql+psycopg://${UAP_POSTGRES_USER}:${UAP_POSTGRES_PASSWORD}@postgres:5432/${failclosed_database}"

cleanup() {
    $compose exec -T postgres psql --no-psqlrc \
        --username "$UAP_POSTGRES_USER" --dbname "$UAP_POSTGRES_DB" \
        --command "ALTER ROLE uap_migrator NOLOGIN" >/dev/null 2>&1 || true
    $compose exec -T postgres dropdb --if-exists --force \
        --username "$UAP_POSTGRES_USER" "$test_database" >/dev/null
    $compose exec -T postgres dropdb --if-exists --force \
        --username "$UAP_POSTGRES_USER" "$failclosed_database" >/dev/null
}
trap cleanup EXIT HUP INT TERM
cleanup
$compose exec -T postgres createdb --username "$UAP_POSTGRES_USER" "$test_database"

alembic_step() {
    $compose run --rm --no-deps \
        --env "UAP_DATABASE_URL=$database_url" \
        object-store-init alembic "$@"
}

query() {
    $compose exec -T postgres psql --no-psqlrc --tuples-only --no-align \
        --username "$UAP_POSTGRES_USER" --dbname "$test_database" --command "$1"
}

alembic_step upgrade 0001_roles_and_schemas
test "$(query "SELECT version_num FROM public.alembic_version")" = "0001_roles_and_schemas"
test "$(query "SELECT count(*) FROM information_schema.schemata WHERE schema_name IN ('ingest','core','ops','audit','public')")" = "5"
test "$(query "SELECT rolname || ':' || rolinherit::text FROM pg_roles WHERE rolname='uap_migrator'")" = "uap_migrator:false"
test "$(query "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()")" = "uap_owner"

$compose run --rm --no-deps --env "UAP_DATABASE_URL=$database_url" \
    object-store-init python tools/configure_roles.py configure
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$database_url" \
    object-store-init python tools/configure_roles.py enable-migrator
test "$(query "SELECT rolcanlogin::text FROM pg_roles WHERE rolname='uap_migrator'")" = "true"

alembic_step -x role=migrator upgrade 0002_authoritative_schema
test "$(query "SELECT version_num FROM public.alembic_version")" = "0002_authoritative_schema"
query "INSERT INTO audit.principals (id, principal_type, service_name, display_name) VALUES ('00000000-0000-7000-8000-000000000777','service','migration-chain-probe','Migration chain probe')" >/dev/null

alembic_step -x role=migrator upgrade head
alembic_step -x role=migrator upgrade head
test "$(query "SELECT version_num FROM public.alembic_version")" = "0010_knowledge_foundation"
test "$(query "SELECT count(*) FROM pg_tables WHERE schemaname IN ('ingest','core','ops','audit','public') AND tablename <> 'alembic_version'")" = "50"
test "$(query "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='core' AND tablename='entity_candidate_evidence')")" = "t"
test "$(query "SELECT attnotnull FROM pg_attribute WHERE attrelid='core.claims'::regclass AND attname='document_version_id'")" = "t"
test "$(query "SELECT count(*) FROM audit.principals WHERE id='00000000-0000-7000-8000-000000000777'")" = "1"

alembic_step -x role=migrator downgrade 0009_model_governance_boundaries
test "$(query "SELECT version_num FROM public.alembic_version")" = "0009_model_governance_boundaries"
test "$(query "SELECT count(*) FROM pg_tables WHERE schemaname IN ('ingest','core','ops','audit','public') AND tablename <> 'alembic_version'")" = "49"
test "$(query "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='core' AND tablename='entity_candidate_evidence')")" = "f"
alembic_step -x role=migrator upgrade head
test "$(query "SELECT version_num FROM public.alembic_version")" = "0010_knowledge_foundation"
test "$(query "SELECT count(*) FROM pg_tables WHERE schemaname IN ('ingest','core','ops','audit','public') AND tablename <> 'alembic_version'")" = "50"

alembic_step -x role=migrator downgrade 0002_authoritative_schema
test "$(query "SELECT version_num FROM public.alembic_version")" = "0002_authoritative_schema"
test "$(query "SELECT count(*) FROM audit.principals WHERE id='00000000-0000-7000-8000-000000000777'")" = "1"
test "$(query "SELECT NOT EXISTS (SELECT 1 FROM pg_database AS d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, '{}'::aclitem[])) AS acl JOIN pg_roles AS r ON r.oid=acl.grantee WHERE d.datname=current_database() AND r.rolname='uap_model_governance' AND acl.privilege_type='CONNECT')")" = "t"
test "$(query "SELECT has_schema_privilege('uap_model_governance', 'core', 'USAGE')")" = "f"
test "$(query "SELECT has_schema_privilege('uap_model_governance', 'ops', 'USAGE')")" = "f"
test "$(query "SELECT has_table_privilege('uap_model_governance', 'core.stored_objects', 'INSERT')")" = "f"
test "$(query "SELECT has_table_privilege('uap_model_governance', 'core.extractions', 'SELECT')")" = "f"
alembic_step -x role=migrator upgrade head
test "$(query "SELECT version_num FROM public.alembic_version")" = "0010_knowledge_foundation"
test "$(query "SELECT count(*) FROM pg_tables WHERE schemaname IN ('ingest','core','ops','audit','public') AND tablename <> 'alembic_version'")" = "50"

$compose exec -T postgres dropdb --if-exists --force \
    --username "$UAP_POSTGRES_USER" "$failclosed_database" >/dev/null
$compose exec -T postgres createdb --username "$UAP_POSTGRES_USER" "$failclosed_database"
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$failclosed_url" \
    object-store-init alembic upgrade 0001_roles_and_schemas
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$failclosed_url" \
    object-store-init python tools/configure_roles.py configure
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$failclosed_url" \
    object-store-init python tools/configure_roles.py enable-migrator
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$failclosed_url" \
    object-store-init alembic -x role=migrator upgrade 0009_model_governance_boundaries
$compose exec -T postgres psql --no-psqlrc \
    --username "$UAP_POSTGRES_USER" --dbname "$failclosed_database" \
    --command "INSERT INTO audit.principals (id, principal_type, service_name, display_name) VALUES ('00000000-0000-7000-8000-000000000778','service','wp8-backfill','WP8 backfill'); INSERT INTO core.claims (id, claim_text, claim_fingerprint, claim_type, assertion_status, created_by) VALUES ('00000000-0000-7000-8000-000000000779','undervable manual claim', repeat('a', 64), 'observation', 'reported', '00000000-0000-7000-8000-000000000778');" >/dev/null
if failclosed_output=$($compose run --rm --no-deps --env "UAP_DATABASE_URL=$failclosed_url" \
    object-store-init alembic -x role=migrator upgrade 0010_knowledge_foundation 2>&1); then
    echo "expected fail-closed claim backfill to abort" >&2
    exit 1
fi
printf '%s\n' "$failclosed_output" | grep -q knowledge_claim_backfill_required

$compose run --rm --no-deps --env "UAP_DATABASE_URL=$database_url" \
    object-store-init python tools/configure_roles.py disable-migrator
test "$(query "SELECT rolcanlogin::text FROM pg_roles WHERE rolname='uap_migrator'")" = "false"

echo "Migration chain verified: 0001 -> 0002 -> 0003 -> 0004 -> 0005 -> 0006 -> 0007 -> 0008 -> 0009 -> 0010, idempotent head, 0010 roundtrip, fail-closed backfill, downgrade smoke."
