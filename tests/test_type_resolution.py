"""Phase A: import-based type resolution disambiguates same-simple-name classes,
removing the false edges/impacts that plain simple-name matching produced."""

from __future__ import annotations

from analysis.impact import change_impact
from index.repository import init_db
from scanner.pipeline import build_index


def _build(tmp_path):
    files = {
        "src/main/java/com/a/Widget.java": "package com.a;\npublic class Widget {}\n",
        "src/main/java/com/b/Widget.java": "package com.b;\npublic class Widget {}\n",
        # uses com.a.Widget explicitly via import
        "src/main/java/com/app/WidgetUser.java": (
            "package com.app;\n"
            "import com.a.Widget;\n"
            "import org.springframework.beans.factory.annotation.Autowired;\n"
            "public class WidgetUser {\n"
            "    @Autowired\n"
            "    private Widget widget;\n"
            "}\n"
        ),
    }
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(tmp_path / ".reverse" / "index.sqlite3")
    build_index(conn, str(tmp_path))
    return conn


def test_field_type_resolves_to_imported_fqn(tmp_path):
    conn = _build(tmp_path)
    try:
        t = conn.execute(
            "SELECT type_fqn FROM field WHERE name = 'widget'"
        ).fetchone()["type_fqn"]
        assert t == "com.a.Widget"  # resolved via the import, not left as 'Widget'
    finally:
        conn.close()


def test_change_impact_does_not_link_the_other_package(tmp_path):
    conn = _build(tmp_path)
    try:
        # the Widget actually used
        used = change_impact(conn, "com.a.Widget")
        assert any(d["target"] == "com.app.WidgetUser" for d in used["direct_impacts"])

        # the same-named Widget that is NOT used must have no WidgetUser impact
        other = change_impact(conn, "com.b.Widget")
        assert all(
            d["target"] != "com.app.WidgetUser" for d in other["direct_impacts"]
        ), "false positive: WidgetUser linked to the wrong Widget"
    finally:
        conn.close()


def test_class_dependency_edge_is_precise(tmp_path):
    conn = _build(tmp_path)
    try:
        rows = conn.execute(
            "SELECT c2.fqn AS target FROM class_dependency cd "
            "JOIN class c1 ON c1.id = cd.from_class_id "
            "JOIN class c2 ON c2.id = cd.to_class_id "
            "WHERE c1.simple_name = 'WidgetUser' AND cd.kind = 'field_injection'"
        ).fetchall()
        targets = {r["target"] for r in rows}
        assert targets == {"com.a.Widget"}  # not both Widgets
    finally:
        conn.close()
