"""``import_flat`` must attach a method description even when a foreign arch.json
carries Java modifiers and a return type in the ``sig`` (``public String make(...)``).
Our own export stores a clean ``name(types): ret`` form, but GigaCode/CBMC-produced
signatures routinely lead with modifiers — a naive ``sig[:sig.index("(")]`` grab would
name the method ``public String make`` and leave its description unmatched."""

from __future__ import annotations

from analysis.flat_arch import _name_and_params, export_flat, import_flat
from index.repository import init_db
from scanner.pipeline import build_index

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"
_SVC = """
package ru.bank.svc;

public class WidgetService {
    public String make(String in) { return in; }
    public static void reset() {}
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


def test_name_and_params_peels_modifiers_and_return_type():
    assert _name_and_params("public static void main(String[] args)") == ("main", ["String"])
    assert _name_and_params("public String make(String in): String") == ("make", ["String"])
    # clean form (our own export) still works unchanged
    assert _name_and_params("make(String in): String") == ("make", ["String"])
    assert _name_and_params("reset()") == ("reset", [])
    # modifiers are lowercase reserved words; `Final` is a legal method name and
    # must not be peeled as one (a case-insensitive check would return "String")
    assert _name_and_params("public String Final(String x)") == ("Final", ["String"])
    # extended modifier set: native/default/strictfp are method modifiers too
    assert _name_and_params("public native void ping()") == ("ping", [])


def test_import_matches_method_with_modifier_laden_sig(tmp_path):
    root, conn = _build(tmp_path)
    try:
        exported = export_flat(conn, str(root))
        entry = next(c for c in exported["classes"] if c["name"] == "WidgetService")
        # a foreign generator prefixed the sig with modifiers + return type
        data = {"classes": [{
            "id": entry["id"],
            "pkg": "ru.bank.svc",
            "name": "WidgetService",
            "methods": [
                {"sig": "public String make(String in): String", "description": "ОПИСАНИЕ make"},
                {"sig": "public static void reset()", "description": "ОПИСАНИЕ reset"},
            ],
        }]}
        stats = import_flat(conn, str(root), data)
        assert stats["classes_matched"] == 1
        assert stats["methods_matched"] == 2, stats
        assert stats["methods_unmatched"] == 0
        rows = dict(conn.execute(
            "SELECT name, summary FROM method WHERE class_id = "
            "(SELECT id FROM class WHERE fqn = 'ru.bank.svc.WidgetService')"
        ).fetchall())
        assert rows["make"] == "ОПИСАНИЕ make"
        assert rows["reset"] == "ОПИСАНИЕ reset"
    finally:
        conn.close()
