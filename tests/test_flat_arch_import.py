"""M4: import_flat must resolve a class by its ``id`` (repo-relative source path ->
class.file_path) first. gigacode's architecture-generator returns a correct ``id``
even when it drops ``pkg`` entirely (or rewrites ``name``); matching on pkg+name /
simple name alone would leave such a class unmatched or land it on the wrong one."""

from __future__ import annotations

from analysis.flat_arch import export_flat, import_flat
from index.repository import init_db
from scanner.pipeline import build_index

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"
_SVC = """
package ru.bank.svc;

public class WidgetService {
    public String make(String in) { return in; }
}
"""


def _build(tmp_path):
    root = tmp_path / "repo"
    for rel, content in {"pom.xml": _POM, "src/main/java/ru/bank/svc/WidgetService.java": _SVC}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    return root, conn


def test_import_matches_by_id_when_pkg_dropped_and_name_wrong(tmp_path):
    root, conn = _build(tmp_path)
    try:
        exported = export_flat(conn, str(root))
        entry = next(c for c in exported["classes"] if c["name"] == "WidgetService")
        # gigacode returned the right id but dropped pkg and mangled the name: only
        # the id can still pin this to the real class
        data = {"classes": [{
            "id": entry["id"],
            "name": "NotEvenTheRightName",
            "description": "ОПИСАНИЕ ПО ID",
            "methods": [],
        }]}
        stats = import_flat(conn, str(root), data)
        assert stats["classes_matched"] == 1
        assert stats["unmatched_classes"] == []
        row = conn.execute(
            "SELECT summary FROM class WHERE fqn = 'ru.bank.svc.WidgetService'").fetchone()
        assert row["summary"] == "ОПИСАНИЕ ПО ID"
    finally:
        conn.close()


def test_import_tolerates_id_with_java_suffix(tmp_path):
    root, conn = _build(tmp_path)
    try:
        exported = export_flat(conn, str(root))
        entry = next(c for c in exported["classes"] if c["name"] == "WidgetService")
        data = {"classes": [{"id": entry["id"] + ".java", "description": "DESC", "methods": []}]}
        stats = import_flat(conn, str(root), data)
        assert stats["classes_matched"] == 1
    finally:
        conn.close()


def test_import_still_matches_by_pkg_name_without_id(tmp_path):
    root, conn = _build(tmp_path)
    try:
        # no id at all -> falls back to fqn (pkg+name)
        data = {"classes": [{"pkg": "ru.bank.svc", "name": "WidgetService",
                             "description": "БЕЗ ID", "methods": []}]}
        stats = import_flat(conn, str(root), data)
        assert stats["classes_matched"] == 1
        row = conn.execute(
            "SELECT summary FROM class WHERE fqn = 'ru.bank.svc.WidgetService'").fetchone()
        assert row["summary"] == "БЕЗ ID"
    finally:
        conn.close()
