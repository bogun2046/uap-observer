#!/bin/sh
# Disposable G10-25 object-store sidecar contract for Codex.
# Does not print credentials, DSN, or role passwords.
# Usage: g10-25-disposable-object-store.sh start|stop|status
#
# Host access (diagnostics only): 127.0.0.1:$UAP_G10_25_S3_API_PORT
# Python 3.12 sentinel sidecar on $UAP_G10_25_NETWORK: g10-25-object-store:8333
#
# Sentinel secrets: process environment wins. Optional files fill missing
# UAP_* keys only. platform/.env is optional (WP2 isolated worktrees often
# have none). UAP_G10_25_ENV_FILE may point at an explicit external env file.
set -eu
set +x

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLATFORM_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$PLATFORM_DIR/compose.g10-25-object-store.yaml"
OPERATION=${1:-}

# The guard's Settings contract still needs a database URL, although the
# disposable sentinel itself does not query PostgreSQL. In the r9 topology the
# legacy URL is the safe compatibility default for that settings object.
if [ -z "${UAP_DATABASE_URL:-}" ] && [ -n "${UAP_WP10_LEGACY_DATABASE_URL:-}" ]; then
    export UAP_DATABASE_URL="$UAP_WP10_LEGACY_DATABASE_URL"
fi

if [ -z "${UAP_S3_DISPOSABLE:-}" ] || [ "$UAP_S3_DISPOSABLE" != "1" ]; then
    echo "refusing: set UAP_S3_DISPOSABLE=1" >&2
    exit 2
fi

if [ -z "${UAP_S3_DISPOSABLE_ID:-}" ] || [ "${#UAP_S3_DISPOSABLE_ID}" -lt 8 ]; then
    echo "refusing: set UAP_S3_DISPOSABLE_ID to a unique run id" >&2
    exit 2
fi

SHORT_ID=$(printf '%s' "$UAP_S3_DISPOSABLE_ID" | tr -cd 'a-zA-Z0-9' | cut -c1-12)
if [ "${#SHORT_ID}" -lt 8 ]; then
    echo "refusing: disposable id has no usable charset" >&2
    exit 2
fi

PROJECT="uap-g10-25-os-${SHORT_ID}"
NETWORK="uap-g10-25-os-${SHORT_ID}"
HOST_PORT=${UAP_G10_25_S3_API_PORT:-18333}
HOST_ENDPOINT="127.0.0.1:${HOST_PORT}"
SIDECAR_ENDPOINT="g10-25-object-store:8333"
SIDECAR_IMAGE=${UAP_APP_IMAGE:-uap-platform:development}
SIDECAR_PYTHON=/app/.venv/bin/python
SIDECAR_CONTAINER="${PROJECT}-sentinel"

case "$PROJECT" in
    *wp3-test*|*wp10-impl*)
        echo "refusing: compose project looks like a shared instance" >&2
        exit 2
        ;;
esac

# Process environment wins; files fill missing keys only.
load_uap_env_file() {
    env_file=$1
    if [ ! -f "$env_file" ]; then
        return 0
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        case "$line" in
            ''|\#*) continue ;;
            export\ *) line=${line#export } ;;
        esac
        case "$line" in
            UAP_*=*)
                key=${line%%=*}
                val=${line#*=}
                case "$key" in
                    *[!A-Za-z0-9_]*|'') continue ;;
                esac
                case "$val" in
                    \"*\") val=${val#\"}; val=${val%\"} ;;
                    \'*\') val=${val#\'}; val=${val%\'} ;;
                esac
                eval "already=\${$key:-}"
                if [ -n "$already" ]; then
                    continue
                fi
                export "$key=$val"
                ;;
        esac
    done < "$env_file"
}

require_sentinel_env() {
    missing=0
    for key in UAP_DATABASE_URL UAP_S3_ACCESS_KEY UAP_S3_SECRET_KEY; do
        eval "val=\${$key:-}"
        if [ -z "$val" ]; then
            echo "refusing: $key is required for the sentinel sidecar" >&2
            missing=1
        fi
    done
    if [ "$missing" -ne 0 ]; then
        return 2
    fi
    return 0
}

require_compose_image_env() {
    missing=0
    for key in UAP_OBJECT_STORE_IMAGE UAP_GO_IMAGE UAP_SEAWEEDFS_COMMIT UAP_SEAWEEDFS_BASE_IMAGE; do
        eval "val=\${$key:-}"
        if [ -z "$val" ]; then
            echo "refusing: $key is required (process env, UAP_G10_25_ENV_FILE, or platform/.env.versions)" >&2
            missing=1
        fi
    done
    if [ "$missing" -ne 0 ]; then
        return 2
    fi
    return 0
}

apply_optional_env_files() {
    if [ -n "${UAP_G10_25_ENV_FILE:-}" ]; then
        if [ ! -f "$UAP_G10_25_ENV_FILE" ]; then
            echo "refusing: UAP_G10_25_ENV_FILE is not a readable file" >&2
            exit 2
        fi
        load_uap_env_file "$UAP_G10_25_ENV_FILE"
    fi
    if [ -f "$PLATFORM_DIR/.env" ]; then
        load_uap_env_file "$PLATFORM_DIR/.env"
    fi
    if [ -f "$PLATFORM_DIR/.env.versions" ]; then
        load_uap_env_file "$PLATFORM_DIR/.env.versions"
    fi
}

apply_optional_env_files

case "${UAP_S3_ENDPOINT:-}" in
    *uap-wp3-test*|*uap-wp10-impl*)
        echo "refusing: UAP_S3_ENDPOINT names a shared instance" >&2
        exit 2
        ;;
esac

# Keep the published host endpoint available for diagnostics and compose. The
# sentinel sidecar overrides it with the disposable network DNS endpoint.
# Do not keep object-store:8333 from process env or files; that is the shared store.
export UAP_S3_ENDPOINT="$HOST_ENDPOINT"
export UAP_S3_SECURE=false
export UAP_S3_DISPOSABLE=1
export UAP_G10_25_NETWORK="$NETWORK"
export UAP_G10_25_COMPOSE_PROJECT="$PROJECT"
export UAP_G10_25_S3_API_PORT="$HOST_PORT"

run_compose() {
    set -- -f "$COMPOSE_FILE" -p "$PROJECT" "$@"
    if [ -f "$PLATFORM_DIR/.env" ]; then
        set -- --env-file "$PLATFORM_DIR/.env" "$@"
    fi
    if [ -n "${UAP_G10_25_ENV_FILE:-}" ] && [ -f "$UAP_G10_25_ENV_FILE" ]; then
        set -- --env-file "$UAP_G10_25_ENV_FILE" "$@"
    fi
    if [ -f "$PLATFORM_DIR/.env.versions" ]; then
        set -- --env-file "$PLATFORM_DIR/.env.versions" "$@"
    fi
    docker compose "$@"
}

print_identity() {
    printf 'status=%s\n' "$1"
    printf 'project=%s\n' "$PROJECT"
    printf 'host_endpoint=%s\n' "$HOST_ENDPOINT"
    printf 'sidecar_endpoint=%s\n' "$SIDECAR_ENDPOINT"
    printf 'sidecar_network=%s\n' "$NETWORK"
    printf 'sentinel_image=%s\n' "$SIDECAR_IMAGE"
    printf 'sentinel_python_executable=%s\n' "$SIDECAR_PYTHON"
    printf 'sentinel_python=3.12\n'
    printf 's3_api_port=%s\n' "$HOST_PORT"
    printf 'sentinel_via=sidecar\n'
    if [ -f "$PLATFORM_DIR/.env" ]; then
        printf 'platform_dotenv=present\n'
    else
        printf 'platform_dotenv=absent\n'
    fi
    if [ -n "${UAP_G10_25_ENV_FILE:-}" ]; then
        printf 'external_env_file=set\n'
    else
        printf 'external_env_file=unset\n'
    fi
    printf 'service=g10-25-object-store\n'
    printf 'disposable=1\n'
    printf 'disposable_id=%s\n' "$UAP_S3_DISPOSABLE_ID"
    printf 'live=NOT RUN until probe preflight\n'
}

install_sentinel() {
    # Secret-bearing variables are inherited by name. Never put their values in
    # Docker argv; non-secret endpoint/runtime parameters are explicit.
    docker run --rm \
        --name "$SIDECAR_CONTAINER" \
        --network "$NETWORK" \
        --volume "$PLATFORM_DIR:/workspace:ro" \
        --workdir /workspace \
        -e UAP_DATABASE_URL \
        -e UAP_S3_ACCESS_KEY \
        -e UAP_S3_SECRET_KEY \
        -e UAP_S3_DISPOSABLE \
        -e UAP_S3_DISPOSABLE_ID \
        -e "UAP_S3_ENDPOINT=$SIDECAR_ENDPOINT" \
        -e UAP_S3_SECURE=false \
        -e UAP_S3_BUCKETS=raw,derived,model-io,public-assets,backups \
        -e PYTHONPATH=/workspace/src \
        "$SIDECAR_IMAGE" \
        /bin/sh -ec '
            version=$(/app/.venv/bin/python -c '\''import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'\'')
            if [ "$version" != "3.12" ]; then
                echo "refusing: sentinel sidecar Python is not 3.12" >&2
                exit 2
            fi
            exec /app/.venv/bin/python -m tools.wp10_object_store_guard --install-sentinel
        '
}

STARTED_OK=0
teardown_if_needed() {
    if [ "${OPERATION}" = "start" ] && [ "$STARTED_OK" -ne 1 ]; then
        echo "sentinel or start failed; removing disposable compose project and volumes" >&2
        docker rm --force "$SIDECAR_CONTAINER" >/dev/null 2>&1 || true
        run_compose down --volumes --remove-orphans || true
        printf 'status=failed-and-removed\n' >&2
        printf 'project=%s\n' "$PROJECT" >&2
        printf 'sidecar_network=%s\n' "$NETWORK" >&2
    fi
}

handle_signal() {
    signal_status=$1
    trap - EXIT HUP INT TERM
    teardown_if_needed
    exit "$signal_status"
}

case "$OPERATION" in
    start)
        require_sentinel_env
        require_compose_image_env
        trap teardown_if_needed EXIT
        trap 'handle_signal 129' HUP
        trap 'handle_signal 130' INT
        trap 'handle_signal 143' TERM
        run_compose up --build --detach --wait
        install_sentinel
        STARTED_OK=1
        print_identity ready
        ;;
    stop)
        run_compose down --volumes --remove-orphans
        print_identity stopped
        ;;
    status)
        run_compose ps
        print_identity status
        ;;
    *)
        echo "usage: g10-25-disposable-object-store.sh start|stop|status" >&2
        exit 2
        ;;
esac
