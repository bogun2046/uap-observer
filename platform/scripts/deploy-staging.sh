#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 /absolute/path/to/staging.env" >&2
    exit 2
fi

env_file=$1
case "$env_file" in
    /*) ;;
    *) echo "staging env path must be absolute" >&2; exit 2 ;;
esac

if [ ! -f "$env_file" ]; then
    echo "staging env file does not exist" >&2
    exit 2
fi

if permissions=$(stat -c '%a' "$env_file" 2>/dev/null); then
    :
else
    permissions=$(stat -f '%Lp' "$env_file")
fi
if [ "$permissions" != "600" ]; then
    echo "staging env file permissions must be 600" >&2
    exit 2
fi

platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$platform_dir"

compose() {
    docker compose \
        --env-file "$env_file" \
        --env-file .env.versions \
        -f compose.yaml \
        -f compose.staging.yaml \
        "$@"
}

compose config --quiet
compose up --build --detach postgres object-store
compose up --build --abort-on-container-exit --exit-code-from object-store-init object-store-init
compose run --rm app alembic upgrade head
compose up --build --detach --wait
compose ps
