from __future__ import annotations

import json
import sys
import uuid
from typing import Any, cast

import pytest
from psycopg import Connection

from uap_platform.v1 import bootstrap as bootstrap_module
from uap_platform.v1.config import load_v1_config
from uap_platform.v1.prompts import v1_prompts

PRINCIPAL = uuid.UUID("00000000-0000-7000-8000-000000001101")


class BootstrapCursor:
    def __init__(self, owner: BootstrapConnection) -> None:
        self.owner = owner
        self.sql = ""
        self.params: object = None
        self.executed: list[str] = []

    def __enter__(self) -> BootstrapCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.sql = sql
        self.params = params
        self.executed.append(sql)
        if "INSERT INTO ingest.source_config_versions" in sql:
            values = params
            assert isinstance(values, tuple)
            self.owner.config_hashes[str(values[0])] = str(values[2])
        elif "INSERT INTO ops.prompt_versions" in sql:
            values = params
            assert isinstance(values, tuple)
            self.owner.prompt_ids[str(values[6])] = values[0]

    def fetchone(self) -> tuple[object, ...] | None:
        if "INSERT INTO audit.principals" in self.sql:
            return (PRINCIPAL,)
        if "INSERT INTO ingest.sources" in self.sql:
            params = self.params
            assert isinstance(params, tuple)
            return (params[0],)
        if "SELECT id, configuration_sha256" in self.sql:
            params = self.params
            assert isinstance(params, tuple)
            digest = self.owner.config_hashes.get(str(params[0]))
            return None if digest is None else (params[0], digest)
        if "SELECT id FROM ops.prompt_versions" in self.sql:
            params = self.params
            assert isinstance(params, tuple)
            prompt_id = self.owner.prompt_ids.get(str(params[0]))
            return None if prompt_id is None else (prompt_id,)
        return None


class BootstrapConnection:
    def __init__(self) -> None:
        self.config_hashes: dict[str, str] = {}
        self.prompt_ids: dict[str, object] = {}
        self.query = BootstrapCursor(self)
        self.commits = 0

    def __enter__(self) -> BootstrapConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> BootstrapCursor:
        return self.query

    def commit(self) -> None:
        self.commits += 1


def test_configuration_adds_controlled_transport_fields() -> None:
    config = load_v1_config()
    rss = next(item for item in config.sources if item.source_type == "rss")
    web = next(item for item in config.sources if item.source_type == "web")

    rss_config = bootstrap_module._configuration(rss)
    web_config = bootstrap_module._configuration(web)

    assert rss_config["fetch_url"] == rss.fetch_url
    assert rss_config["source_type"] == "rss"
    assert rss_config["v1_1_enabled"] is True
    assert web_config["fetch_url"] == web.fetch_url
    assert web_config["source_type"] == "web"
    assert web_config["v1_1_enabled"] is False


def test_bootstrap_inserts_source_configs_and_prompts_once_then_is_idempotent() -> None:
    connection = BootstrapConnection()
    config = load_v1_config()

    created = bootstrap_module.bootstrap(cast(Connection[Any], connection))
    unchanged = bootstrap_module.bootstrap(cast(Connection[Any], connection))

    assert created == {
        "source_config_versions_created": len(config.sources),
        "prompts_created": len(v1_prompts()),
    }
    assert unchanged == {"source_config_versions_created": 0, "prompts_created": 0}
    assert connection.commits == 2
    assert connection.query.executed.count("SET ROLE uap_owner") == 2
    assert connection.query.executed.count("RESET ROLE") == 2
    assert len(connection.config_hashes) == len(config.sources)
    assert len(connection.prompt_ids) == len(v1_prompts())


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_V1_BOOTSTRAP_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["bootstrap"])

    with pytest.raises(SystemExit, match="UAP_V1_BOOTSTRAP_DATABASE_URL is required"):
        bootstrap_module.main()


def test_main_bootstraps_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = BootstrapConnection()
    monkeypatch.setenv("UAP_V1_BOOTSTRAP_DATABASE_URL", "postgresql+psycopg://owner@db/uap")
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--config", "sources.json"])
    monkeypatch.setattr(
        "uap_platform.v1.bootstrap.psycopg.connect", lambda _dsn: connection
    )
    monkeypatch.setattr(
        bootstrap_module,
        "bootstrap",
        lambda _connection, path: {"created": int(path is not None)},
    )

    bootstrap_module.main()

    assert json.loads(capsys.readouterr().out) == {"created": 1}
