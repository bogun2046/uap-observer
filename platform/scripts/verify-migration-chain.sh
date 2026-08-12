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
compose="docker compose --env-file $platform_dir/.env.versions --env-file $platform_dir/.env -f $platform_dir/compose.yaml"
database_url="postgresql+psycopg://${UAP_POSTGRES_USER}:${UAP_POSTGRES_PASSWORD}@postgres:5432/${test_database}"

cleanup() {
    $compose exec -T postgres dropdb --if-exists --force \
        --username "$UAP_POSTGRES_USER" "$test_database" >/dev/null
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

alembic_step -x role=migrator upgrade 0002_authoritative_schema
test "$(query "SELECT version_num FROM public.alembic_version")" = "0002_authoritative_schema"
query "INSERT INTO audit.principals (id, principal_type, service_name, display_name) VALUES ('00000000-0000-7000-8000-000000000777','service','migration-chain-probe','Migration chain probe')" >/dev/null

alembic_step -x role=migrator upgrade head
alembic_step -x role=migrator upgrade head
test "$(query "SELECT version_num FROM public.alembic_version")" = "0003_permissions_and_guards"
test "$(query "SELECT count(*) FROM pg_tables WHERE schemaname IN ('ingest','core','ops','audit','public') AND tablename <> 'alembic_version'")" = "49"
test "$(query "SELECT count(*) FROM audit.principals WHERE id='00000000-0000-7000-8000-000000000777'")" = "1"

alembic_step -x role=migrator downgrade 0002_authoritative_schema
test "$(query "SELECT version_num FROM public.alembic_version")" = "0002_authoritative_schema"
test "$(query "SELECT count(*) FROM audit.principals WHERE id='00000000-0000-7000-8000-000000000777'")" = "1"
alembic_step -x role=migrator upgrade head

echo "Migration chain verified: 0001 -> 0002 -> 0003, idempotent head, downgrade smoke."
