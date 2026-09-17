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

test_database=uap_wp3_migrator_failure
compose="docker compose --env-file $platform_dir/.env.versions --env-file $platform_dir/.env -f $platform_dir/compose.yaml"
database_url="postgresql+psycopg://${UAP_POSTGRES_USER}:${UAP_POSTGRES_PASSWORD}@postgres:5432/${test_database}"

admin_query() {
    $compose exec -T postgres psql --no-psqlrc --tuples-only --no-align \
        --username "$UAP_POSTGRES_USER" --dbname "$UAP_POSTGRES_DB" --command "$1"
}

test_query() {
    $compose exec -T postgres psql --no-psqlrc --tuples-only --no-align \
        --username "$UAP_POSTGRES_USER" --dbname "$test_database" --command "$1"
}

cleanup() {
    admin_query "ALTER ROLE uap_migrator NOLOGIN" >/dev/null 2>&1 || true
    $compose exec -T postgres dropdb --if-exists --force \
        --username "$UAP_POSTGRES_USER" "$test_database" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
cleanup
$compose exec -T postgres createdb --username "$UAP_POSTGRES_USER" "$test_database"

$compose run --rm --no-deps --env "UAP_DATABASE_URL=$database_url" \
    object-store-init alembic upgrade 0003_permissions_and_guards
test "$(test_query "SELECT version_num FROM public.alembic_version")" = \
    "0003_permissions_and_guards"
test_query "ALTER TRIGGER public_claim_requires_evidence ON public.claims RENAME TO injected_missing_claim_trigger" >/dev/null

set +e
$compose run --rm --no-deps --env "UAP_DATABASE_URL=$database_url" \
    object-store-init scripts/migrate-platform.sh
migration_status=$?
set -e

test "$migration_status" -ne 0
test "$(test_query "SELECT version_num FROM public.alembic_version")" = \
    "0003_permissions_and_guards"
if [ "$(admin_query "SELECT rolcanlogin::text FROM pg_roles WHERE rolname='uap_migrator'")" != "false" ]; then
    echo "migration failure left uap_migrator LOGIN enabled" >&2
    exit 1
fi

echo "Failed migration returned non-zero and closed uap_migrator LOGIN."
