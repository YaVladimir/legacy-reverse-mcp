"""batch_generate: chunking, chunk-response validation and the prompt contract.

No GigaCode subprocess is launched here — only the pure functions that decide
what gets sent and, more importantly, what is *accepted back* (a model that
renames/invents classes must not smuggle descriptions onto wrong symbols).
"""

from __future__ import annotations

import json
from pathlib import Path

from analysis import flat_arch
from index.repository import get_conn, init_db
from scanner.pipeline import build_index
from summarizer.batch_generate import (
    _chunk_classes,
    _chunk_prompt,
    _make_chunk_json,
    _validate_chunk_result,
    main,
)
from tests.conftest import write_fixture_repo


def _cls(i: int, described: bool = False) -> dict:
    return {
        "id": f"src/main/java/ru/bank/C{i}",
        "pkg": "ru.bank",
        "name": f"C{i}",
        "description": f"описание {i}" if described else "",
        "methods": [],
    }


def test_chunk_classes_splits_and_covers_everything():
    classes = [_cls(i) for i in range(7)]
    chunks = _chunk_classes(classes, 3)
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert [c["name"] for ch in chunks for c in ch] == [f"C{i}" for i in range(7)]


def test_make_chunk_json_keeps_flat_envelope():
    original = {"project": "saldo", "generated_at": "2026-07-05", "classes": []}
    chunk = [_cls(1), _cls(2)]
    data = _make_chunk_json(original, chunk)
    assert data["project"] == "saldo"
    assert data["total_classes"] == 2
    assert data["classes"] == chunk


def test_validate_accepts_only_sent_classes():
    sent = [_cls(1), _cls(2), _cls(3)]
    returned = {
        "classes": [
            _cls(1, described=True),
            _cls(2, described=True),
            # renamed/invented by the model -> must be dropped
            {"id": "src/main/java/ru/bank/Invented", "pkg": "ru.bank",
             "name": "Invented", "description": "выдумано", "methods": []},
        ]
    }
    accepted, info = _validate_chunk_result(sent, returned)
    assert [c["name"] for c in accepted] == ["C1", "C2"]
    assert info["extraneous"] == 1
    assert info["missing"] == ["src/main/java/ru/bank/C3"]


def test_validate_rejects_unparseable_or_empty():
    sent = [_cls(1)]
    for bad in (None, {}, {"classes": "oops"}):
        accepted, info = _validate_chunk_result(sent, bad)
        assert accepted == []
        assert info["missing"] == ["src/main/java/ru/bank/C1"]


def test_validate_matches_by_pkg_name_when_id_missing():
    sent = [{"pkg": "ru.bank", "name": "NoId", "description": "", "methods": []}]
    returned = {"classes": [{"pkg": "ru.bank", "name": "NoId", "description": "ок", "methods": []}]}
    accepted, info = _validate_chunk_result(sent, returned)
    assert len(accepted) == 1 and info["missing"] == []


def test_validate_tolerates_cosmetic_id_rewrites():
    """Real gigacode run: the model echoed ids with '.java' appended (and models
    on Windows produce backslashes) — a whole chunk must not be rejected over a
    cosmetic id rewrite."""
    sent = [_cls(1), _cls(2)]
    returned = {"classes": [
        dict(_cls(1, described=True), id="src/main/java/ru/bank/C1.java"),
        dict(_cls(2, described=True), id=".\\src\\main\\java\\ru\\bank\\C2"),
    ]}
    accepted, info = _validate_chunk_result(sent, returned)
    assert [c["name"] for c in accepted] == ["C1", "C2"]
    assert info["missing"] == [] and info["extraneous"] == 0


def test_validate_falls_back_to_pkg_name_when_id_rewritten_beyond_cosmetics():
    sent = [_cls(1)]
    returned = {"classes": [dict(_cls(1, described=True), id="C:/somewhere/else/C1.java")]}
    accepted, info = _validate_chunk_result(sent, returned)
    assert len(accepted) == 1 and info["missing"] == []


def test_validate_unions_partial_results_without_double_accept():
    """A partial first attempt + its retry validate as a union via the shared
    ``seen`` set: overlap isn't accepted twice, missing reflects the union."""
    sent = [_cls(1), _cls(2), _cls(3)]
    first = {"classes": [_cls(1, described=True), _cls(2, described=True)]}
    second = {"classes": [_cls(2, described=True), _cls(3, described=True)]}
    seen: set[str] = set()
    acc1, info1 = _validate_chunk_result(sent, first, seen)
    assert len(acc1) == 2 and info1["missing"] == ["src/main/java/ru/bank/C3"]
    acc2, info2 = _validate_chunk_result(sent, second, seen)
    assert [c["name"] for c in acc2] == ["C3"]  # C2 not accepted twice
    assert info2["missing"] == []


def test_merge_only_imports_outputs_from_disk(tmp_path, monkeypatch):
    """--merge-only: no GigaCode at all — validate/merge/import out-chunk files
    that another generator (e.g. a Claude agent) already wrote to the work dir."""
    monkeypatch.delenv("LEGACY_REVERSE_LLM_BASE_URL", raising=False)
    repo = write_fixture_repo(tmp_path / "repo")
    db = repo / ".reverse" / "index.sqlite3"
    conn = init_db(db)
    build_index(conn, str(repo))

    arch = flat_arch.export_flat(conn, str(repo))
    conn.close()
    arch_path = tmp_path / "arch.json"
    arch_path.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")

    # chunk-size 3 over 5 fixture classes -> chunks of 3 and 2; simulate an
    # external agent describing every class of both chunks
    chunks = _chunk_classes(arch["classes"], 3)
    work_dir = repo / ".reverse" / "batch"
    work_dir.mkdir(parents=True)
    for i, ch in enumerate(chunks):
        described = [dict(c, description=f"АГЕНТ: класс {c['name']}.") for c in ch]
        out = _make_chunk_json(arch, described)
        (work_dir / f"out-chunk-{i:04d}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8")

    main([str(arch_path), "--repo", str(repo), "--merge-only",
          "--chunk-size", "3", "--skip-describe"])

    conn = get_conn(db)
    row = conn.execute(
        "SELECT summary FROM class WHERE simple_name = 'DepositController'").fetchone()
    assert row["summary"] == "АГЕНТ: класс DepositController."
    # merged artifact is preserved under .reverse/, outputs are not deleted
    assert (repo / ".reverse" / "arch-merged.json").exists()
    assert (work_dir / "out-chunk-0000.json").exists()
    conn.close()


def test_prompt_demands_reading_sources_and_forbids_invention():
    prompt = _chunk_prompt(Path("chunk-0000.json"), 0, 4)
    assert ".java" in prompt                 # id -> source file to actually read
    assert "не выдумывай" in prompt          # anti-hallucination rule
    assert "не изменяй id" in prompt         # structure must round-trip for import
    assert "БЕЗ расширения" in prompt        # ...and unambiguously: id has no .java
    assert "ВСЕ классы" in prompt            # partial responses are an error
    assert "часть 1 из 4" in prompt
