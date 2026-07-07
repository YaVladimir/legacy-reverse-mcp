"""M1: reading an index built by an older version must not raise mid-query.

Two safety nets: ``get_conn`` forward-migrates the ``endpoint`` table + view on
open, and ``trace_endpoint`` degrades to a structured error if it still meets a
pre-provenance endpoint view (or a superseded row) instead of crashing."""

from __future__ import annotations

from pathlib import Path

from analysis.trace import trace_endpoint
from index.repository import get_conn, init_db
from scanner.pipeline import build_index

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"

_CONTROLLER = """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1")
public class PingController {
    @GetMapping("/ping")
    public String ping() { return "pong"; }
}
"""

# a pre-provenance endpoint view: no annotation_* / annotation_inherited / superseded
_OLD_VIEW = """
CREATE VIEW v_endpoint_full AS
SELECT e.id, e.http_method, e.full_path,
       c.fqn AS controller_fqn, c.file_path AS controller_file,
       m.name AS handler_name, m.signature AS handler_signature, m.line_start AS handler_line
FROM endpoint e
LEFT JOIN class c ON c.id = e.controller_class_id
LEFT JOIN method m ON m.id = e.handler_method_id
"""


def _build(tmp_path) -> Path:
    root = tmp_path / "repo"
    for rel, content in {"pom.xml": _POM, "src/main/java/ru/bank/api/PingController.java": _CONTROLLER}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    conn.close()
    return root / ".reverse" / "index.sqlite3"


def _degrade_view(db_path: Path) -> None:
    # open raw (bypass the migration) and swap in the old view
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("DROP VIEW v_endpoint_full")
    conn.executescript(_OLD_VIEW)
    conn.commit()
    conn.close()


def test_get_conn_heals_stale_endpoint_view(tmp_path):
    db = _build(tmp_path)
    _degrade_view(db)
    conn = get_conn(db)  # migration on open recreates the current view
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(v_endpoint_full)")}
        assert "annotation_inherited" in cols
        # and a trace now works end-to-end
        res = trace_endpoint(conn, http_method="GET", path_contains="/ping")
        assert res.get("error") is None
        assert res["endpoint"]["path"] == "/v1/ping"
    finally:
        conn.close()


def test_trace_on_stale_view_returns_structured_error_not_crash(tmp_path):
    db = _build(tmp_path)
    _degrade_view(db)
    # a raw connection whose view was NOT healed (didn't go through get_conn)
    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        ep_id = conn.execute("SELECT id FROM endpoint LIMIT 1").fetchone()["id"]
        res = trace_endpoint(conn, endpoint_id=ep_id)
        assert res["error"] == "not_found"  # structured, not an IndexError
    finally:
        conn.close()
