"""Reconcile content-addressed object storage with PostgreSQL registration."""

from __future__ import annotations

import json

import psycopg

from uap_platform.config import load_settings
from uap_platform.object_registry import StorageDomain, reconcile_unregistered_objects
from uap_platform.object_store_init import build_client


def main() -> None:
    settings = load_settings()
    client = build_client(settings)
    with psycopg.connect(settings.psycopg_database_url) as connection:
        removed = reconcile_unregistered_objects(connection, client, StorageDomain.RAW)
        connection.commit()
    print(json.dumps({"removed": removed, "domain": StorageDomain.RAW.value}, sort_keys=True))


if __name__ == "__main__":
    main()
