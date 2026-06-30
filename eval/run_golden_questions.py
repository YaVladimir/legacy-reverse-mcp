"""Golden-questions runner: scan the fixture mini-project, call tool functions
directly (no MCP transport) and check STRUCTURAL quality gates.

Usage:
    py eval/run_golden_questions.py          # markdown report, exit 0/1
    py eval/run_golden_questions.py --json    # machine-readable report
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from analysis.context_pack import generate_context_pack  # noqa: E402
from analysis.explain import explain_class  # noqa: E402
from analysis.impact import change_impact  # noqa: E402
from analysis.trace import trace_endpoint  # noqa: E402
from index import queries  # noqa: E402
from index.repository import init_db  # noqa: E402
from scanner.pipeline import build_index  # noqa: E402

FIXTURE = ROOT / "eval" / "fixture"
QUESTIONS = ROOT / "eval" / "golden_questions.yaml"


# ------------------------------------------------------------
# tool dispatch
# ------------------------------------------------------------

def call_tool(conn, tool: str, inp: dict):
    if tool == "get_project_overview":
        return queries.project_overview(conn)
    if tool == "get_module_map":
        return queries.module_map(conn)
    if tool == "list_endpoints":
        rows = queries.list_endpoints(conn)
        return {"count": len(rows), "endpoints": rows}
    if tool == "find_code_areas":
        return queries.find_code_areas(conn, inp.get("query", ""))
    if tool == "find_feature":
        return queries.find_feature(conn, inp.get("topic", ""))
    if tool == "get_class_card":
        return queries.class_card(conn, inp["class"]) or {"error": "not_found"}
    if tool == "explain_class":
        return explain_class(conn, inp["class"])
    if tool == "trace_endpoint":
        return trace_endpoint(conn, http_method=inp.get("method"), path_contains=inp.get("path_contains"))
    if tool == "get_change_impact":
        return change_impact(conn, inp["symbol"])
    if tool == "generate_context_pack":
        return generate_context_pack(conn, inp["task"])
    raise ValueError(f"unknown tool {tool!r}")


# ------------------------------------------------------------
# structural gates
# ------------------------------------------------------------

def _non_empty(x) -> bool:
    return bool(x)


GATES = {
    "has_confidence": lambda r: "confidence" in r,
    "has_limitations": lambda r: _non_empty(r.get("limitations")),
    "findings_have_evidence": lambda r: bool(r.get("inferred_findings"))
    and all(_non_empty(f.get("evidence")) for f in r["inferred_findings"]),
    "steps_have_evidence": lambda r: bool(r.get("trace"))
    and all(_non_empty(s.get("evidence")) for s in r["trace"]),
    "impacts_have_confidence": lambda r: all(
        "confidence" in i for i in (r.get("direct_impacts", []) + r.get("candidate_impacts", []))
    ),
    "pack_not_empty": lambda r: _non_empty(r.get("selected_items")),
    "selected_items_have_reason": lambda r: all(
        i.get("reason") and "evidence" in i for i in r.get("selected_items", [])
    ),
}


def _count_items(result: dict, field: str | None) -> int:
    if field and isinstance(result.get(field), list):
        return len(result[field])
    if isinstance(result.get("count"), int):
        return result["count"]
    return 0


def evaluate(question: dict, result: dict) -> list[dict]:
    checks: list[dict] = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    if isinstance(result, dict) and result.get("error"):
        add("no_error", False, f"tool returned error: {result.get('message', result['error'])}")
        return checks

    for sub in question.get("expected_contains", []):
        blob = json.dumps(result, ensure_ascii=False).lower()
        add(f"contains:{sub}", sub.lower() in blob)

    if "expected_min_items" in question:
        n = _count_items(result, question.get("items_field"))
        add(f"min_items>={question['expected_min_items']}", n >= question["expected_min_items"], f"got {n}")

    for field in question.get("expected_fields", []):
        add(f"field:{field}", field in result)

    for gate in question.get("gates", []):
        fn = GATES.get(gate)
        add(f"gate:{gate}", fn(result) if fn else False, "" if fn else "unknown gate")

    return checks


# ------------------------------------------------------------
# run
# ------------------------------------------------------------

def run() -> dict:
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]

    tmp = Path(tempfile.mkdtemp(prefix="lrmcp-golden-"))
    repo = tmp / "fixture"
    shutil.copytree(FIXTURE, repo)
    conn = init_db(repo / ".reverse" / "index.sqlite3")
    scan = build_index(conn, str(repo))

    results = []
    try:
        for q in questions:
            try:
                result = call_tool(conn, q["tool"], q.get("input", {}))
                checks = evaluate(q, result)
            except Exception as exc:  # noqa: BLE001
                checks = [{"check": "tool_runs", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}]
            results.append(
                {
                    "id": q["id"],
                    "tool": q["tool"],
                    "question": q["question"],
                    "passed": all(c["ok"] for c in checks) and bool(checks),
                    "checks": checks,
                }
            )
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r["passed"])
    return {"scan": scan, "total": len(results), "passed": passed, "results": results}


def to_markdown(report: dict) -> str:
    lines = ["# Golden questions report", ""]
    s = report["scan"]
    lines.append(
        f"Fixture scanned: {s['classes']} classes, {s['endpoints']} endpoints, "
        f"{s.get('observed_facts', 0)} facts, {s.get('method_calls', 0)} calls."
    )
    lines.append(f"\n**{report['passed']}/{report['total']} questions passed.**\n")
    for r in report["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"## [{mark}] {r['id']} — {r['tool']}")
        lines.append(f"_{r['question']}_")
        for c in r["checks"]:
            cm = "ok" if c["ok"] else "XX"
            detail = f" ({c['detail']})" if c["detail"] else ""
            lines.append(f"- [{cm}] {c['check']}{detail}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    # questions are in Russian; force UTF-8 stdout so a cp1252 console won't choke
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    report = run()
    if "--json" in sys.argv:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(report))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
