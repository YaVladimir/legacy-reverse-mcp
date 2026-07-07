"""H2 + M5: the durable description cache (descriptions.sqlite3) must not be able
to break a scan, and the freshness hash it stores must be read deterministically.

H2: a legacy cache (no content_hash column) or a corrupt/non-sqlite file used to
crash the whole scan via reapply_imported. It must migrate or degrade to a
warning. M5: imported_for_class must judge freshness by the class row's hash, not
an arbitrary row picked without ORDER BY."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from index.repository import init_db
from scanner.pipeline import build_index
from summarizer import describe

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"
_SVC = """
package ru.bank.svc;

public class WidgetService {
    public String make(String in) { return in; }
}
"""


def _build(tmp_path) -> tuple[Path, sqlite3.Connection]:
    root = tmp_path / "repo"
    for rel, content in {"pom.xml": _POM, "src/main/java/ru/bank/svc/WidgetService.java": _SVC}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    return root, conn


def test_reapply_imported_migrates_legacy_cache_without_content_hash(tmp_path):
    root, conn = _build(tmp_path)
    try:
        fqn = "ru.bank.svc.WidgetService"
        # a legacy cache: imported_description created before content_hash existed
        cache_path = root / ".reverse" / "descriptions.sqlite3"
        legacy = sqlite3.connect(cache_path)
        legacy.executescript(
            "CREATE TABLE imported_description ("
            " ref_key TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,"
            " source TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        legacy.execute(
            "INSERT INTO imported_description (ref_key, kind, content, source) VALUES (?, 'class', ?, 'flat-json')",
            (fqn, "LEGACY DESCRIPTION"),
        )
        legacy.commit()
        legacy.close()

        stats = describe.reapply_imported(conn, str(root))
        assert "error" not in stats
        assert stats["classes"] == 1
        row = conn.execute("SELECT summary FROM class WHERE fqn = ?", (fqn,)).fetchone()
        assert row["summary"] == "LEGACY DESCRIPTION"
    finally:
        conn.close()


def test_reapply_imported_tolerates_corrupt_cache(tmp_path):
    root, conn = _build(tmp_path)
    try:
        (root / ".reverse" / "descriptions.sqlite3").write_bytes(b"this is not a sqlite database")
        stats = describe.reapply_imported(conn, str(root))
        assert "error" in stats  # degraded to a warning instead of raising
        assert stats["classes"] == 0
    finally:
        conn.close()


def test_scan_survives_corrupt_description_cache(tmp_path):
    root = tmp_path / "repo"
    for rel, content in {"pom.xml": _POM, "src/main/java/ru/bank/svc/WidgetService.java": _SVC}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (root / ".reverse").mkdir(parents=True, exist_ok=True)
    (root / ".reverse" / "descriptions.sqlite3").write_bytes(b"garbage, definitely not sqlite")

    conn = init_db(root / ".reverse" / "index.sqlite3")
    try:
        summary = build_index(conn, str(root))  # must not raise
        assert summary["classes"] >= 1
        assert conn.execute("SELECT COUNT(*) FROM scan_manifest").fetchone()[0] == 1
    finally:
        conn.close()


def test_imported_for_class_freshness_hash_is_deterministic(tmp_path):
    root, conn = _build(tmp_path)
    try:
        fqn = "ru.bank.svc.WidgetService"
        cache = describe._open_cache(str(root))
        try:
            # simulate a partial re-import: the class row carries a newer hash than
            # the (older) method row. Freshness must be judged by the class row.
            cache.execute(
                "INSERT INTO imported_description (ref_key, kind, content, source, content_hash) "
                "VALUES (?, 'class', ?, 'flat-json', 'HASH_CLASS_NEW')",
                (fqn, "class desc"),
            )
            cache.execute(
                "INSERT INTO imported_description (ref_key, kind, content, source, content_hash) "
                "VALUES (?, 'method', ?, 'flat-json', 'HASH_METHOD_OLD')",
                (f"{fqn}#make(String in): String", "method desc"),
            )
            cache.commit()
            _class_text, _methods, stored_hash = describe.imported_for_class(cache, fqn)
            assert stored_hash == "HASH_CLASS_NEW"
        finally:
            cache.close()
    finally:
        conn.close()
