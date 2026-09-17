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

umask 077
mkdir -p "$backup_dir"
test ! -e "$backup_dir/database.dump" || {
    echo "backup target already contains database.dump" >&2
    exit 1
}

$compose exec -T -e "PGPASSWORD=${UAP_BACKUP_PASSWORD}" postgres pg_dump \
    --username uap_backup \
    --dbname "$UAP_POSTGRES_DB" \
    --format custom \
    --no-owner \
    --no-acl \
    --exclude-table-data public.alembic_version \
    --serializable-deferrable > "$backup_dir/database.dump"

$compose run --rm --no-deps \
    --volume "$backup_dir:/backup" \
    app python tools/object_backup.py backup --directory /backup

database_sha256=$(sha256sum "$backup_dir/database.dump" | awk '{print $1}')
printf '%s  database.dump\n' "$database_sha256" > "$backup_dir/database.dump.sha256"
chmod -R go-rwx "$backup_dir"
echo "Backup completed with database and object manifests."
