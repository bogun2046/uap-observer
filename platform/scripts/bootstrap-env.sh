#!/bin/sh
set -eu

platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file="$platform_dir/.env"

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required to generate local credentials" >&2
    exit 1
fi

umask 077
tmp_file="$env_file.tmp.$$"
trap 'rm -f "$tmp_file"' EXIT HUP INT TERM

postgres_password=$(openssl rand -hex 24)
migrator_password=$(openssl rand -hex 24)
api_password=$(openssl rand -hex 24)
worker_password=$(openssl rand -hex 24)
scheduler_password=$(openssl rand -hex 24)
publisher_password=$(openssl rand -hex 24)
model_governance_password=$(openssl rand -hex 24)
public_reader_password=$(openssl rand -hex 24)
audit_reader_password=$(openssl rand -hex 24)
backup_password=$(openssl rand -hex 24)
s3_access_key="uap$(openssl rand -hex 8)"
s3_secret_key=$(openssl rand -hex 24)

{
    if [ -f "$env_file" ]; then
        sed -n 'p' "$env_file"
    else
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
    fi
    grep -q '^UAP_MIGRATOR_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_MIGRATOR_PASSWORD=%s\n' "$migrator_password"
    grep -q '^UAP_API_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_API_PASSWORD=%s\n' "$api_password"
    grep -q '^UAP_WORKER_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_WORKER_PASSWORD=%s\n' "$worker_password"
    grep -q '^UAP_SCHEDULER_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_SCHEDULER_PASSWORD=%s\n' "$scheduler_password"
    grep -q '^UAP_PUBLISHER_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_PUBLISHER_PASSWORD=%s\n' "$publisher_password"
    grep -q '^UAP_MODEL_GOVERNANCE_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_MODEL_GOVERNANCE_PASSWORD=%s\n' "$model_governance_password"
    grep -q '^UAP_PUBLIC_READER_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_PUBLIC_READER_PASSWORD=%s\n' "$public_reader_password"
    grep -q '^UAP_AUDIT_READER_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_AUDIT_READER_PASSWORD=%s\n' "$audit_reader_password"
    grep -q '^UAP_BACKUP_PASSWORD=' "$env_file" 2>/dev/null || printf 'UAP_BACKUP_PASSWORD=%s\n' "$backup_password"
} >"$tmp_file"

chmod 600 "$tmp_file"
mv "$tmp_file" "$env_file"
trap - EXIT HUP INT TERM
echo "Ensured platform/.env contains local-only random credentials."
