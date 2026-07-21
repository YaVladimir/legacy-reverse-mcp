"""Stage 8: post-scan baseline report (markdown + json).

A short, honest snapshot of the project: inventory counts, top modules/packages,
public API surface, candidate domain areas, a sample of low-confidence layer
findings (each with evidence), and the tool's limitations. Degrades gracefully
on empty/tiny projects.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from index import repository as repo
from models import LIMITATIONS

REPORTS_RELATIVE = Path(".reverse") / "reports"

_LISTENER_ANNOTATIONS = ("@KafkaListener", "@JmsListener", "@RabbitListener")

# config keys that look like outbound HTTP integration endpoints ...
_URL_KEY_SUFFIXES = (".url", ".uri", ".base-url", ".baseurl", ".endpoint")
# ... minus infrastructure (datastore/broker/management) URLs that aren't app-to-app clients
_INFRA_URL_PREFIXES = (
    "spring.datasource", "spring.r2dbc", "spring.flyway", "spring.liquibase",
    "spring.jpa", "spring.data", "spring.rabbitmq", "spring.kafka", "spring.redis",
    "spring.activemq", "spring.artemis", "spring.elasticsearch", "spring.mail",
    "spring.cloud.config", "spring.cloud.bus", "spring.cloud.vault", "spring.h2",
    "management", "server", "eureka", "logging", "springdoc", "springfox",
)
_URL_CREDS_RE = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


def _mask_url_creds(value: str | None) -> str | None:
    """Redact ``user:pass@`` embedded in a URL value before it reaches the report."""
    if not value:
        return value
    return _URL_CREDS_RE.sub("://***:***@", value)


def _external_service_url_keys(conn) -> list[dict]:
    """Distinct non-secret config keys that name an outbound service endpoint."""
    seen: dict[str, str | None] = {}
    for r in conn.execute("SELECT key, value FROM config_property WHERE is_secret = 0 ORDER BY key"):
        low = r["key"].lower()
        if not low.endswith(_URL_KEY_SUFFIXES) or low.startswith(_INFRA_URL_PREFIXES):
            continue
        seen.setdefault(r["key"], r["value"])
    return [{"key": k, "value": _mask_url_creds(v)} for k, v in seen.items()]


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _inventory(conn) -> dict:
    roles = {r["role"]: r["n"] for r in conn.execute("SELECT role, COUNT(*) n FROM class GROUP BY role")}
    return {
        "java_files": _scalar(conn, "SELECT COUNT(DISTINCT file_path) FROM class"),
        "maven_modules": _scalar(conn, "SELECT COUNT(*) FROM module WHERE build_file LIKE '%pom.xml'"),
        "gradle_modules": _scalar(conn, "SELECT COUNT(*) FROM module WHERE build_file LIKE '%build.gradle%'"),
        "packages": _scalar(conn, "SELECT COUNT(*) FROM package"),
        "classes": _scalar(conn, "SELECT COUNT(*) FROM class"),
        "methods": _scalar(conn, "SELECT COUNT(*) FROM method"),
        "controllers": roles.get("controller", 0),
        "services": roles.get("service", 0),
        "repositories": roles.get("repository", 0),
        "entities": roles.get("entity", 0),
        "endpoints": _scalar(conn, "SELECT COUNT(*) FROM endpoint WHERE superseded = 0"),
        "scheduled_jobs": _scalar(conn, "SELECT COUNT(*) FROM method_annotation WHERE name = '@Scheduled'"),
        "message_listeners": _scalar(
            conn,
            "SELECT COUNT(*) FROM method_annotation WHERE name IN (%s)"
            % ",".join("?" * len(_LISTENER_ANNOTATIONS)),
            _LISTENER_ANNOTATIONS,
        ),
        "external_clients": (
            _scalar(conn, "SELECT COUNT(DISTINCT class_id) FROM class_annotation WHERE name = '@FeignClient'")
            # the scanner rewrites field types to FQN via the file's imports, so a
            # normally-imported RestTemplate is stored fully qualified — match the
            # simple-name suffix, not just the bare name (wildcard-import case)
            + _scalar(
                conn,
                "SELECT COUNT(*) FROM field WHERE type_fqn IN ('RestTemplate', 'WebClient') "
                "OR type_fqn LIKE '%.RestTemplate' OR type_fqn LIKE '%.WebClient'",
            )
        ),
        # complementary, config-derived signal (kept separate from the code-based count above)
        "external_service_urls": len(_external_service_url_keys(conn)),
    }


def _top_modules(conn, limit=10):
    return [
        {"name": r["name"], "classes": r["n"], "endpoints": r["ep"]}
        for r in conn.execute(
            "SELECT mo.name, COUNT(DISTINCT cl.id) n, COUNT(DISTINCT e.id) ep "
            "FROM module mo LEFT JOIN class cl ON cl.module_id = mo.id "
            "LEFT JOIN endpoint e ON e.controller_class_id = cl.id AND e.superseded = 0 "
            "GROUP BY mo.id ORDER BY n DESC LIMIT ?",
            (limit,),
        )
    ]


def _top_packages(conn, limit=10):
    return [
        {"package": r["fqn"], "classes": r["n"]}
        for r in conn.execute(
            "SELECT p.fqn, COUNT(c.id) n FROM class c JOIN package p ON p.id = c.package_id "
            "GROUP BY p.id ORDER BY n DESC LIMIT ?",
            (limit,),
        )
    ]


def _api_surface(conn, limit=20):
    by_verb = {r["http_method"]: r["n"] for r in conn.execute(
        "SELECT http_method, COUNT(*) n FROM endpoint WHERE superseded = 0 GROUP BY http_method")}
    sample = [
        {"http_method": r["http_method"], "path": r["full_path"], "controller": r["controller_fqn"]}
        for r in conn.execute(
            "SELECT http_method, full_path, controller_fqn FROM v_endpoint_full ORDER BY full_path LIMIT ?",
            (limit,),
        )
    ]
    return {"by_verb": by_verb, "sample": sample}


def _low_confidence_findings(conn, limit=25):
    """Read the low-confidence layer findings persisted during scan."""
    return [
        {
            "finding_type": f["finding_type"],
            "subject": f["subject"],
            "summary": f["summary"],
            "confidence": f["confidence"],
            "evidence": f["evidence"],
        }
        for f in repo.list_inferred_findings(conn, finding_type="spring_layer", limit=limit)
    ]


def _config(conn) -> dict:
    files = repo.list_config_files(conn)
    kinds: dict[str, int] = {}
    for f in files:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    datasources = [
        {"key": r["key"], "value": _mask_url_creds(r["value"])}
        for r in conn.execute(
            "SELECT DISTINCT key, value FROM config_property "
            "WHERE is_secret = 0 AND lower(key) LIKE '%datasource%url%' ORDER BY key"
        )
    ]
    return {
        "files": len(files),
        "properties": repo.count_config_properties(conn),
        "kinds": kinds,
        "profiles": sorted({f["profile"] for f in files if f["profile"]}),
        "datasources": datasources,
        "external_service_urls": _external_service_url_keys(conn),
        "feign_config_keys": _scalar(
            conn,
            "SELECT COUNT(DISTINCT key) FROM config_property "
            "WHERE lower(key) LIKE 'feign.%' OR lower(key) LIKE '%.feign.%'",
        ),
        "secret_keys": _scalar(conn, "SELECT COUNT(*) FROM config_property WHERE is_secret = 1"),
    }


_LIMITATION_CODES = [
    "external_types_unresolved", "interface_impl_unresolved", "spring_proxies",
    "syntactic_calls", "no_call_graph", "dynamic_endpoints", "tests_not_indexed",
]


def collect_baseline(conn: sqlite3.Connection) -> dict:
    manifest = conn.execute("SELECT * FROM scan_manifest ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "repo_path": manifest["repo_path"] if manifest else None,
        "build_tool": manifest["build_tool"] if manifest else None,
        "scanned_at": manifest["scanned_at"] if manifest else None,
        "inventory": _inventory(conn),
        "top_modules": _top_modules(conn),
        "top_packages": _top_packages(conn),
        "api_surface": _api_surface(conn),
        "config": _config(conn),
        "low_confidence_findings": _low_confidence_findings(conn),
        "limitations": [LIMITATIONS[c].model_dump(mode="json") for c in _LIMITATION_CODES],
    }


def render_markdown(data: dict) -> str:
    inv = data["inventory"]
    lines = ["# Legacy Reverse Baseline", ""]
    if data.get("repo_path"):
        lines += [f"- Repo: `{data['repo_path']}`",
                  f"- Build tool: {data.get('build_tool') or 'unknown'}",
                  f"- Scanned at: {data.get('scanned_at') or '-'}", ""]

    lines += ["## Inventory", ""]
    for label, key in [
        ("Java files", "java_files"), ("Maven modules", "maven_modules"),
        ("Gradle modules", "gradle_modules"), ("Packages", "packages"),
        ("Classes", "classes"), ("Methods", "methods"), ("Controllers", "controllers"),
        ("Services", "services"), ("Repositories", "repositories"), ("Entities", "entities"),
        ("Endpoints", "endpoints"), ("Scheduled jobs", "scheduled_jobs"),
        ("Message listeners", "message_listeners"), ("External clients", "external_clients"),
        ("External service URLs (config)", "external_service_urls"),
    ]:
        lines.append(f"- {label}: {inv.get(key, 0)}")
    lines.append("")

    lines += ["## Top modules", ""]
    for m in data["top_modules"]:
        lines.append(f"- `{m['name']}` — {m['classes']} classes, {m['endpoints']} endpoints")
    lines += ["", "## Top packages", ""]
    for p in data["top_packages"]:
        lines.append(f"- `{p['package']}` — {p['classes']} classes")

    api = data["api_surface"]
    lines += ["", "## Public API surface", "", f"By verb: {api['by_verb'] or '(none)'}", ""]
    for e in api["sample"]:
        ctrl = (e["controller"] or "").split(".")[-1]
        lines.append(f"- `{e['http_method']} {e['path']}` -> {ctrl}")

    cfg = data.get("config") or {}
    lines += ["", "## Config / profiles", ""]
    kinds = ", ".join(f"{k}: {v}" for k, v in sorted(cfg.get("kinds", {}).items())) or "none"
    lines.append(f"- Config files: {cfg.get('files', 0)} ({kinds})")
    lines.append(
        f"- Properties: {cfg.get('properties', 0)} "
        f"({cfg.get('secret_keys', 0)} secret-bearing, values masked)"
    )
    profiles = cfg.get("profiles") or []
    lines.append(f"- Profiles: {', '.join(profiles) if profiles else '(default only)'}")
    for d in cfg.get("datasources", []):
        lines.append(f"- Datasource `{d['key']}` = `{d['value']}`")
    ext = cfg.get("external_service_urls") or []
    if ext:
        lines.append(f"- External service URLs (from config, heuristic): {len(ext)}")
        for e in ext[:10]:
            val = f" = `{e['value']}`" if e.get("value") else ""
            lines.append(f"    - `{e['key']}`{val}")
    if cfg.get("feign_config_keys"):
        lines.append(f"- Feign config keys: {cfg['feign_config_keys']}")

    lines += ["", "## Candidate domain areas", ""]
    for p in data["top_packages"][:8]:
        leaf = p["package"].split(".")[-1]
        lines.append(f"- **{leaf}** (`{p['package']}`, {p['classes']} classes)")

    lines += ["", "## Low-confidence findings", ""]
    if data["low_confidence_findings"]:
        for f in data["low_confidence_findings"]:
            lines.append(f"- {f['subject']} — {f['summary']} (confidence: {f['confidence']})")
    else:
        lines.append("- (none)")

    lines += ["", "## Limitations", ""]
    for lim in data["limitations"]:
        lines.append(f"- **{lim['code']}**: {lim['description']}")

    return "\n".join(lines) + "\n"


def write_baseline(conn: sqlite3.Connection, repo_path: str | Path, report_dir: Path | None = None) -> dict:
    data = collect_baseline(conn)
    markdown = render_markdown(data)
    out_dir = report_dir or (Path(repo_path) / REPORTS_RELATIVE)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "baseline.md"
    json_path = out_dir / "baseline.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"markdown_path": str(md_path), "json_path": str(json_path), "data": data, "markdown": markdown}
