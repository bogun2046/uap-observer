#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 BACKUP_DIRECTORY" >&2
    exit 2
fi

platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
backup_dir=$1
env_file=${UAP_ENV_FILE:-$platform_dir/.env}
case "$backup_dir" in
    /*) ;;
    *) backup_dir=$(pwd)/$backup_dir ;;
esac

requested_project=${UAP_COMPOSE_PROJECT_NAME:-}
# shellcheck disable=SC1091
. "$env_file"
if [ -n "$requested_project" ]; then
    UAP_COMPOSE_PROJECT_NAME=$requested_project
    export UAP_COMPOSE_PROJECT_NAME
fi
compose="docker compose --env-file $platform_dir/.env.versions --env-file $env_file -f $platform_dir/compose.yaml"

(cd "$backup_dir" && sha256sum -c database.dump.sha256)
$compose exec -T postgres pg_restore \
    --username "$UAP_POSTGRES_USER" \
    --dbname "$UAP_POSTGRES_DB" \
    --data-only \
    --disable-triggers \
    --single-transaction \
    --no-owner \
    --no-acl < "$backup_dir/database.dump"

$compose run --rm --no-deps \
    --volume "$backup_dir:/backup:ro" \
    app python tools/object_backup.py restore --directory /backup

$compose run --rm --no-deps app python tools/object_backup.py verify
echo "Restore and cross-medium verification completed."
