"""CBMC (codebase-memory-mcp, Layer 1) grounding for batch description generation.

These are unit tests over the seams — no subprocess, no live binary. They pin the
three behaviours that make the integration safe: (1) the enriched prompt inlines
real code for grounded classes and degrades *per class* to the signature for the
rest (never all-or-nothing); (2) project resolution prefers an explicit config over
fuzzy guessing and refuses ambiguous matches; (3) a per-class fetch failure is
logged, not silently swallowed, and simply drops that class from the context."""

from __future__ import annotations

import argparse
from pathlib import Path

import summarizer.batch_generate as bg

_CLASSES = [
    {"id": "src/A", "pkg": "com.example.app", "name": "OrderService", "sig": "class OrderService",
     "methods": [{"sig": "place(Order): Receipt", "modifiers": "public", "description": ""}]},
    {"id": "src/B", "pkg": "com.example.app", "name": "Money", "sig": "record Money(long amount)"},
]


def _snippet(source, callers=(), callees=()):
    # real binary response shape: caller_names/callee_names are plain string arrays
    return {"source": source, "caller_names": list(callers), "callee_names": list(callees)}


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
        return _snippet("SRC", callers=["com.example.app.OrderController"],
                        callees=["com.example.app.OrderRepository"]), {}

    monkeypatch.setattr(bg, "cbmc_get_code_snippet", fake_snippet)
    res = bg._fetch_class_code(_CLASSES[0], "proj", None, 30.0)
    assert res == {"fqn": "com.example.app.OrderService", "code": "SRC",
                   "related": ["com.example.app.OrderController",
                               "com.example.app.OrderRepository"]}


def test_fetch_class_code_falls_back_to_class_label_search(monkeypatch):
    """When the FQN doesn't resolve, the fallback must search with name_pattern +
    label=Class (exact regex, server-side filter) — a BM25 query would camelCase-split
    the name and could surface a method/route as the top hit."""
    calls = {}

    def fake_snippet(qn, project=None, include_neighbors=True, binary=None, timeout=30.0):
        if qn == "com.example.app.OrderService":
            return None, {"error": "not found"}
        assert qn == "proj.src.com.example.app.OrderService"  # canonical graph QN
        return _snippet("SRC2"), {}

    def fake_call(tool, args, binary=None, timeout=300.0, repo_path=None):
        calls["tool"], calls["args"], calls["timeout"] = tool, args, timeout
        return {"results": [{"qualified_name": "proj.src.com.example.app.OrderService",
                             "label": "Class"}]}, {}

    monkeypatch.setattr(bg, "cbmc_get_code_snippet", fake_snippet)
    monkeypatch.setattr(bg, "cbmc_call", fake_call)
    res = bg._fetch_class_code(_CLASSES[0], "proj", None, 30.0)
    assert res and res["code"] == "SRC2"
    assert calls["tool"] == "search_graph"
    assert calls["args"]["label"] == "Class"
    assert calls["args"]["name_pattern"] == "^OrderService$"
    # --cbmc-timeout must govern the fallback search too, not the 300s default
    assert calls["timeout"] == 30.0


def test_fetch_class_code_fallback_filters_foreign_package(monkeypatch):
    """Two same-named classes in different packages: the wrong-package hit must be
    filtered out by the pkg-suffix check — grounding a class in a same-named foreign
    class's code is worse than describing from the signature."""
    def fake_snippet(qn, project=None, include_neighbors=True, binary=None, timeout=30.0):
        if qn == "com.example.app.OrderService":
            return None, {"error": "not found"}
        assert qn == "proj.src.com.example.app.OrderService"  # never the foreign one
        return _snippet("RIGHT"), {}

    def fake_call(tool, args, binary=None, timeout=300.0, repo_path=None):
        return {"results": [
            {"qualified_name": "proj.src.com.other.legacy.OrderService", "label": "Class"},
            {"qualified_name": "proj.src.com.example.app.OrderService", "label": "Class"},
        ]}, {}

    monkeypatch.setattr(bg, "cbmc_get_code_snippet", fake_snippet)
    monkeypatch.setattr(bg, "cbmc_call", fake_call)
    res = bg._fetch_class_code(_CLASSES[0], "proj", None, 30.0)
    assert res and res["code"] == "RIGHT"


def test_fetch_class_code_fallback_refuses_ambiguity_without_pkg(monkeypatch, capsys):
    """No pkg to filter on and several same-named candidates: refuse (degrade to the
    signature) rather than ground with the first hit."""
    cls = {"id": "src/C", "name": "OrderService", "sig": "class OrderService"}
    monkeypatch.setattr(bg, "cbmc_get_code_snippet",
                        lambda *a, **k: (None, {"error": "not found"}))
    monkeypatch.setattr(bg, "cbmc_call", lambda *a, **k: ({"results": [
        {"qualified_name": "p.a.OrderService"}, {"qualified_name": "p.b.OrderService"},
    ]}, {}))
    assert bg._fetch_class_code(cls, "proj", None, 30.0) is None
    assert "ambiguous graph match" in capsys.readouterr().out


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
    expected = bg._derive_cbmc_project_name(repo)
    monkeypatch.setattr(bg, "cbmc_list_projects", lambda *a, **k: ([{"name": expected}], {}))
    assert bg._resolve_cbmc_project(str(repo)) == expected


def test_derive_cbmc_project_name_mirrors_binary_rules(tmp_path):
    """Mirror of fqn.c:cbm_project_name_from_path: unsafe chars (':' '/' '\\') → '-',
    dashes collapse, leading dash trimmed. The naive replace('/', '-') kept the drive
    colon on Windows and a leading dash on POSIX — exact match then never fired."""
    name = bg._derive_cbmc_project_name(tmp_path / "myrepo")
    assert ":" not in name and "/" not in name and "\\" not in name
    assert not name.startswith(("-", "."))
    assert "--" not in name
    assert name.endswith("myrepo")


def test_cbmc_name_map_posix_windows_and_nonascii():
    """Cross-platform shapes, pinned OS-independently (the resolve() step would graft
    a drive onto a POSIX path on Windows, so the pure mapping is tested directly).
    The POSIX expectation matches the binary's own docs: "/tmp/bench/..." → "tmp-bench-..."."""
    # POSIX: leading '/' becomes a leading dash and is trimmed
    assert bg._cbmc_name_map("/home/user/myrepo") == "home-user-myrepo"
    assert bg._cbmc_name_map("/tmp/bench") == "tmp-bench"
    # Windows: drive colon + separator collapse into a single dash
    assert bg._cbmc_name_map("D:/dev/legacy") == "D-dev-legacy"
    # non-ASCII bytes → two lowercase hex digits each (fqn.c #571)
    assert bg._cbmc_name_map("/tmp/пример") == "tmp-d0bfd180d0b8d0bcd0b5d180"
    # spaces are unsafe too (fqn.c #349: "my project" must not keep the space)
    assert bg._cbmc_name_map("/home/u/my project") == "home-u-my-project"


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


def test_resolve_project_env_var_wins(monkeypatch):
    monkeypatch.setenv("LEGACY_REVERSE_CBMC_PROJECT", "env-proj")
    monkeypatch.setattr(bg, "cbmc_list_projects",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not list")))
    assert bg._resolve_cbmc_project("/repo/x") == "env-proj"


# --- _setup_cbmc: --cbmc-bin must not lose toml project pinning --------------

def test_setup_cbmc_explicit_bin_keeps_toml_project(monkeypatch):
    """--cbmc-bin overrides the binary but must NOT discard the toml [cbmc] project —
    losing it silently drops project resolution to the substring heuristic (the old
    code passed an empty config dict whenever --cbmc-bin was given)."""
    monkeypatch.delenv("LEGACY_REVERSE_CBMC_PROJECT", raising=False)
    monkeypatch.setattr(bg, "resolve_cbmc_config",
                        lambda repo: ("/resolved/bin", {"project": "pinned-proj"}))
    monkeypatch.setattr(bg, "cbmc_available", lambda b: True)
    monkeypatch.setattr(bg, "cbmc_list_projects",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not list")))
    args = argparse.Namespace(use_cbmc=True, cbmc_bin="/custom/bin")
    assert bg._setup_cbmc(args, Path("/repo")) == (True, "/custom/bin", "pinned-proj")


def test_setup_cbmc_disabled_without_flag():
    args = argparse.Namespace(use_cbmc=False, cbmc_bin=None)
    assert bg._setup_cbmc(args, Path("/repo")) == (False, None, None)


def test_setup_cbmc_unavailable_binary_degrades(monkeypatch, capsys):
    monkeypatch.setattr(bg, "resolve_cbmc_config", lambda repo: ("/gone/bin", {}))
    monkeypatch.setattr(bg, "cbmc_available", lambda b: False)
    args = argparse.Namespace(use_cbmc=True, cbmc_bin=None)
    assert bg._setup_cbmc(args, Path("/repo")) == (False, None, None)
    assert "falling back to file mode" in capsys.readouterr().out


# --- _build_prompt_for_chunk: CBMC / file-mode wiring ------------------------

def test_build_prompt_grounded_uses_cbmc_prompt(monkeypatch, tmp_path):
    ctx = {"com.example.app.OrderService": {"code": "SRC", "related": []}}
    monkeypatch.setattr(bg, "_fetch_chunk_context", lambda *a, **k: ctx)
    prompt, grounded = bg._build_prompt_for_chunk(
        tmp_path / "chunk-0000.json", 0, 1, _CLASSES,
        use_cbmc=True, cbmc_project="proj", cbmc_binary=None, cbmc_timeout=30.0,
    )
    assert grounded == 1
    assert "[Код из knowledge graph]" in prompt


def test_build_prompt_total_miss_reverts_to_file_mode(monkeypatch, tmp_path):
    """Nothing grounded at all → the chunk falls back to the file-based prompt
    (grounded == 0 so the summary still counts it as a CBMC attempt)."""
    monkeypatch.setattr(bg, "_fetch_chunk_context", lambda *a, **k: {})
    prompt, grounded = bg._build_prompt_for_chunk(
        tmp_path / "chunk-0000.json", 0, 1, _CLASSES,
        use_cbmc=True, cbmc_project="proj", cbmc_binary=None, cbmc_timeout=30.0,
    )
    assert grounded == 0
    assert "Открой файл" in prompt


def test_build_prompt_file_mode_without_cbmc(tmp_path):
    prompt, grounded = bg._build_prompt_for_chunk(
        tmp_path / "chunk-0000.json", 0, 1, _CLASSES,
        use_cbmc=False, cbmc_project=None, cbmc_binary=None, cbmc_timeout=30.0,
    )
    assert grounded is None  # CBMC wasn't in play at all
    assert "Открой файл" in prompt
