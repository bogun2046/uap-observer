#!/bin/sh
set -eu

close_migrator() {
    python tools/configure_roles.py disable-migrator
}

trap close_migrator EXIT HUP INT TERM
python tools/ensure_model_governance_role.py
if python tools/configure_roles.py database-bootstrapped; then
    :
else
    status=$?
    if [ "$status" -ne 3 ]; then
        exit "$status"
    fi
    alembic upgrade 0001_roles_and_schemas
fi
python tools/configure_roles.py configure
python tools/configure_roles.py enable-migrator
alembic -x role=migrator upgrade head
close_migrator
trap - EXIT HUP INT TERM
