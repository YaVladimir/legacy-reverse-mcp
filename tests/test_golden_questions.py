"""Stage 9: the golden-questions runner stays green (structural quality gates)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "golden_runner", _ROOT / "eval" / "run_golden_questions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_golden_questions_pass():
    runner = _load_runner()
    report = runner.run()
    assert report["total"] >= 8, "at least 8 golden questions required"
    failed = [r["id"] for r in report["results"] if not r["passed"]]
    assert not failed, f"golden questions failed: {failed}"
