#!/bin/sh
set -eu

platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file="$platform_dir/.env"

if [ -f "$env_file" ]; then
    exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate local credentials" >&2
    exit 1
fi

umask 077
tmp_file="$env_file.tmp.$$"
trap 'rm -f "$tmp_file"' EXIT HUP INT TERM

postgres_password=$(openssl rand -hex 24)
s3_access_key="uap$(openssl rand -hex 8)"
s3_secret_key=$(openssl rand -hex 24)

{
    printf '%s\n' 'UAP_COMPOSE_PROJECT_NAME=uap-platform-dev'
    printf '%s\n' 'UAP_APP_ENV=development'
    printf '%s\n' 'UAP_LOG_LEVEL=INFO'
    printf '%s\n' 'UAP_POSTGRES_DB=uap_platform'
    printf '%s\n' 'UAP_POSTGRES_USER=uap_platform'
    printf 'UAP_POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'UAP_S3_ACCESS_KEY=%s\n' "$s3_access_key"
    printf 'UAP_S3_SECRET_KEY=%s\n' "$s3_secret_key"
    printf '%s\n' 'UAP_HEALTH_PORT=8080'
    printf '%s\n' 'UAP_S3_API_PORT=8333'
} >"$tmp_file"

chmod 600 "$tmp_file"
mv "$tmp_file" "$env_file"
trap - EXIT HUP INT TERM
echo "Created platform/.env with local-only random credentials."
