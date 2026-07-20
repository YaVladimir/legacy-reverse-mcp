"""CBMC (codebase-memory-mcp, Layer 1) grounding for batch description generation.

These are unit tests over the seams — no subprocess, no live binary. They pin the
three behaviours that make the integration safe: (1) the enriched prompt inlines
real code for grounded classes and degrades *per class* to the signature for the
rest (never all-or-nothing); (2) project resolution prefers an explicit config over
fuzzy guessing and refuses ambiguous matches; (3) a per-class fetch failure is
logged, not silently swallowed, and simply drops that class from the context."""

from __future__ import annotations

import summarizer.batch_generate as bg

_CLASSES = [
    {"id": "src/A", "pkg": "com.example.app", "name": "OrderService", "sig": "class OrderService",
     "methods": [{"sig": "place(Order): Receipt", "modifiers": "public", "description": ""}]},
    {"id": "src/B", "pkg": "com.example.app", "name": "Money", "sig": "record Money(long amount)"},
]


def _snippet(source, neighbors=()):
    return {"source": source, "neighbors": [{"qualified_name": n} for n in neighbors]}


# --- prompt: grounded inline + per-class signature degradation ---------------

def test_cbmc_prompt_inlines_code_and_degrades_per_class():
    ctx = {"com.example.app.OrderService": {
        "code": "public class OrderService { void place() {} }",
        "related": ["com.example.app.OrderRepository"],
    }}
    prompt = bg._cbmc_chunk_prompt(_CLASSES, 0, 1, ctx)
    # grounded class carries real code + neighbours
    assert "public class OrderService { void place() {} }" in prompt
    assert "com.example.app.OrderRepository" in prompt
    # ungrounded class degrades to its signature, explicitly marked
    assert "[Код не найден в knowledge graph" in prompt
    assert "record Money(long amount)" in prompt


def test_cbmc_prompt_carries_copyable_metadata_and_method_list():
    """The final instruction says 'copy id/pkg/name/sig from Метаданные' and 'describe
    the methods from Методы' — so both MUST literally appear in the prompt; a model
    can't echo fields it was never shown, and an invented id would fail validation."""
    prompt = bg._cbmc_chunk_prompt(_CLASSES, 0, 1, {})
    assert '"id": "src/A"' in prompt          # the validator's primary key is copyable
    assert '"id": "src/B"' in prompt
    assert '"sig": "class OrderService"' in prompt
    assert '"place(Order): Receipt"' in prompt  # the exact method set to describe


def test_cbmc_prompt_caps_oversized_source():
    huge = "x" * (bg._MAX_SNIPPET_CHARS + 500)
    ctx = {"com.example.app.OrderService": {"code": huge, "related": []}}
    prompt = bg._cbmc_chunk_prompt(_CLASSES[:1], 0, 1, ctx)
    assert "[код усечён]" in prompt
    assert huge not in prompt  # full source must not leak through the cap


def test_fetch_class_code_grounds_by_fqn(monkeypatch):
    def fake_snippet(qn, project=None, include_neighbors=True, binary=None, timeout=30.0):
        assert qn == "com.example.app.OrderService"
        return _snippet("SRC", ["com.example.app.OrderRepository"]), {}

    monkeypatch.setattr(bg, "cbmc_get_code_snippet", fake_snippet)
    res = bg._fetch_class_code(_CLASSES[0], "proj", None, 30.0)
    assert res == {"fqn": "com.example.app.OrderService", "code": "SRC",
                   "related": ["com.example.app.OrderRepository"]}


def test_fetch_chunk_context_logs_failure_not_swallow(monkeypatch, capsys):
    def flaky(cls, project, binary, timeout):
        if cls["name"] == "Money":
            raise RuntimeError("boom")
        return {"fqn": bg._cls_fqn(cls), "code": "SRC", "related": []}

    monkeypatch.setattr(bg, "_fetch_class_code", flaky)
    ctx = bg._fetch_chunk_context(_CLASSES, "proj", None, 30.0)
    assert set(ctx) == {"com.example.app.OrderService"}  # failed class absent
    assert "CBMC fetch failed for com.example.app.Money" in capsys.readouterr().out


# --- project resolution: explicit wins, ambiguity refuses --------------------

def test_resolve_project_prefers_explicit_config(monkeypatch):
    called = {"list": False}
    monkeypatch.setattr(bg, "cbmc_list_projects", lambda *a, **k: (called.__setitem__("list", True), [])[1])
    assert bg._resolve_cbmc_project("/repo/x", {"project": "explicit-proj"}) == "explicit-proj"
    assert called["list"] is False  # never needed to guess


def test_resolve_project_exact_path_match(monkeypatch, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    expected = repo.resolve().as_posix().replace("/", "-")
    monkeypatch.setattr(bg, "cbmc_list_projects", lambda *a, **k: ([{"name": expected}], {}))
    assert bg._resolve_cbmc_project(str(repo)) == expected


def test_resolve_project_refuses_ambiguous(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "svc"
    repo.mkdir()
    monkeypatch.setattr(
        bg, "cbmc_list_projects",
        lambda *a, **k: ([{"name": "a-svc-1"}, {"name": "b-svc-2"}], {}),
    )
    assert bg._resolve_cbmc_project(str(repo)) is None
    assert "ambiguous project" in capsys.readouterr().out


def test_resolve_project_passes_explicit_binary(monkeypatch, tmp_path):
    """--cbmc-bin points at a binary that isn't on PATH: listing projects must go
    through THAT binary, or the listing silently comes back empty and grounding
    falls back to file mode despite a perfectly available CBMC."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    seen = {}

    def fake_list(binary=None):
        seen["binary"] = binary
        return [{"name": repo.resolve().as_posix().replace("/", "-")}], {}

    monkeypatch.setattr(bg, "cbmc_list_projects", fake_list)
    assert bg._resolve_cbmc_project(str(repo), binary="/opt/custom/cbmc") is not None
    assert seen["binary"] == "/opt/custom/cbmc"
