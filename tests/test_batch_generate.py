"""batch_generate: chunking, chunk-response validation and the prompt contract.

No GigaCode subprocess is launched here — only the pure functions that decide
what gets sent and, more importantly, what is *accepted back* (a model that
renames/invents classes must not smuggle descriptions onto wrong symbols).
"""

from __future__ import annotations

from pathlib import Path

from summarizer.batch_generate import (
    _chunk_classes,
    _chunk_prompt,
    _make_chunk_json,
    _validate_chunk_result,
)


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


def test_prompt_demands_reading_sources_and_forbids_invention():
    prompt = _chunk_prompt(Path("chunk-0000.json"), 0, 4)
    assert ".java" in prompt                 # id -> source file to actually read
    assert "не выдумывай" in prompt          # anti-hallucination rule
    assert "не изменяй id" in prompt         # structure must round-trip for import
    assert "часть 1 из 4" in prompt
