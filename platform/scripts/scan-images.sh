#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 APP_IMAGE POSTGRES_IMAGE REPORT_DIR" >&2
    exit 2
fi

app_image=$1
postgres_image=$2
report_dir=$3
platform_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

# shellcheck disable=SC1091
. "$platform_dir/.env.versions"

mkdir -p "$report_dir"
report_dir=$(CDPATH= cd -- "$report_dir" && pwd)

scan_image() {
    report_name=$1
    image_ref=$2
    echo "Scanning $report_name for HIGH and CRITICAL vulnerabilities"
    docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        -v uap-trivy-cache:/root/.cache/trivy \
        -v "$report_dir:/reports" \
        "$UAP_TRIVY_IMAGE" image \
        --scanners vuln \
        --skip-version-check \
        --severity HIGH,CRITICAL \
        --exit-code 1 \
        --format json \
        --output "/reports/$report_name.json" \
        "$image_ref"
}

scan_image app "$app_image"
scan_image postgres "$postgres_image"
scan_image object-store "$UAP_OBJECT_STORE_IMAGE"

echo "Image vulnerability gate passed for app, PostgreSQL, and object storage."
