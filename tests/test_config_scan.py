"""Phase D: indexing of Spring externalized configuration."""

from __future__ import annotations

import pytest

from analysis.report import collect_baseline, render_markdown
from index.repository import (
    count_config_properties,
    init_db,
    list_config_files,
    list_config_properties,
)
from scanner.config_scanner import index_config
from scanner.pipeline import build_index

_SRC = "src/main/java/ru/bank/pay"
_RES = "src/main/resources"

_FILES: dict[str, str] = {
    "pom.xml": "<project><groupId>ru.bank</groupId><artifactId>pay</artifactId><version>1.0</version></project>",
    f"{_SRC}/PayController.java": """
package ru.bank.pay;
import org.springframework.web.bind.annotation.*;
@RestController
public class PayController {
    @GetMapping("/ping")
    public String ping() { return "ok"; }
}
""",
    f"{_RES}/application.yml": """
spring:
  application:
    name: pay-service
  datasource:
    url: jdbc:postgresql://db:5432/pay
    username: payuser
    password: ${DB_PASSWORD}
payments:
  service:
    url: https://payments.internal/api
  api-key: ${PAYMENTS_KEY}
feign:
  client:
    config:
      default:
        connectTimeout: 5000
""",
    f"{_RES}/application-dev.properties": """
# dev overrides
server.port=8081
fraud.service.url=http://localhost:9000
spring.jpa.hibernate.ddl-auto=update
admin.secret=topsecret
""",
    f"{_RES}/bootstrap.yml": """
spring:
  cloud:
    config:
      uri: http://config:8888
""",
}


@pytest.fixture
def scanned(tmp_path):
    root = tmp_path / "repo"
    for rel, content in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    summary = build_index(conn, str(root))
    try:
        yield summary, conn, str(root)
    finally:
        conn.close()


def test_pipeline_reports_config_counts(scanned):
    summary, _conn, _root = scanned
    assert summary["config_files"] == 3
    assert summary["config_properties"] > 0
    assert summary["config_profiles"] == ["dev"]
    assert summary["config_secrets"] == 3  # datasource.password, payments.api-key, admin.secret


def test_nested_yaml_flattened_to_dotted_keys(scanned):
    _summary, conn, _root = scanned
    keys = {r["key"]: r["value"] for r in list_config_properties(conn, limit=1000)}
    assert keys["spring.application.name"] == "pay-service"
    assert keys["spring.datasource.url"].startswith("jdbc:postgresql://")
    assert keys["feign.client.config.default.connectTimeout"] == "5000"


def test_properties_parsed_with_profile(scanned):
    _summary, conn, _root = scanned
    dev = {r["key"]: r["value"] for r in list_config_properties(conn, profile="dev")}
    assert dev["server.port"] == "8081"
    assert dev["fraud.service.url"] == "http://localhost:9000"
    # every dev row really came from the dev-profile file
    assert all(r["profile"] == "dev" for r in list_config_properties(conn, profile="dev"))


def test_secret_values_masked_on_read(scanned):
    _summary, conn, _root = scanned
    pw = list_config_properties(conn, key_contains="password")
    assert pw and all(r["is_secret"] and r["value"] == "***" for r in pw)
    # but the index keeps the raw value, available on explicit opt-in
    raw = list_config_properties(conn, key_contains="password", include_secret_values=True)
    assert raw[0]["value"] == "${DB_PASSWORD}"


def test_config_files_carry_kind_and_module(scanned):
    _summary, conn, _root = scanned
    by_path = {r["file_path"]: r for r in list_config_files(conn)}
    assert by_path[f"{_RES}/application.yml"]["kind"] == "application-yaml"
    assert by_path[f"{_RES}/application-dev.properties"]["kind"] == "application-properties"
    assert by_path[f"{_RES}/bootstrap.yml"]["kind"] == "bootstrap-yaml"
    # config files associate to the (single, root) module
    assert by_path[f"{_RES}/application.yml"]["module_name"] == "pay"


def test_report_has_config_section(scanned):
    _summary, conn, _root = scanned
    data = collect_baseline(conn)
    cfg = data["config"]
    assert cfg["files"] == 3
    assert cfg["profiles"] == ["dev"]
    assert cfg["feign_config_keys"] == 1
    # only genuine app-to-app endpoints, not datasource/cloud-config infra URLs
    ext_keys = {e["key"] for e in cfg["external_service_urls"]}
    assert ext_keys == {"payments.service.url", "fraud.service.url"}
    assert data["inventory"]["external_service_urls"] == 2

    md = render_markdown(data)
    assert "## Config / profiles" in md
    assert "Profiles: dev" in md
    # secrets never reach the rendered report
    assert "topsecret" not in md


def test_rescan_is_idempotent(scanned):
    _summary, conn, root = scanned
    before = count_config_properties(conn)
    index_config(conn, root)  # clear_config + re-insert
    assert count_config_properties(conn) == before
    assert len(list_config_files(conn)) == 3
