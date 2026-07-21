"""Stage 7: explainable, task-scoped context pack.

Selects code by priority (endpoint/controller match > direct service/repo deps >
entities/DTOs > keyword matches), and for *every* selected item records why it
was included (reason + evidence + confidence). Items that don't fit the token /
item budget are reported in ``excluded_items`` instead of silently dropped.
"""

from __future__ import annotations

import sqlite3

from analysis.common import conf_str, ev, limitations, min_confidence
from index.queries import _simple_type, find_code_areas

# role -> selection priority (lower = stronger)
_ROLE_PRIORITY = {"controller": 1, "service": 2, "repository": 2, "entity": 3, "dto": 3}


def _est_tokens(text: str) -> int:
    return len(text) // 4


def _class_row(conn, fqn):
    return conn.execute(
        "SELECT id, fqn, simple_name, role, kind, file_path, summary FROM class WHERE fqn = ?",
        (fqn,),
    ).fetchone()


def _resolve_simple(conn, type_fqn):
    if not type_fqn:
        return None
    row = conn.execute("SELECT * FROM class WHERE fqn = ? LIMIT 1", (type_fqn,)).fetchone()
    if row is not None:
        return row
    simple = _simple_type(type_fqn)
    if not simple:
        return None
    return conn.execute("SELECT * FROM class WHERE simple_name = ? LIMIT 1", (simple,)).fetchone()


def _render(item: dict) -> str:
    lines = [
        f"### {item['symbol']}  ({item['kind']}, {item.get('role', '?')})",
        f"`{item['fqn']}`",
        f"reason: {item['reason']}",
    ]
    if item.get("file_path"):
        lines.append(f"file: {item['file_path']}")
    if item.get("summary"):
        lines.append(item["summary"])
    return "\n".join(lines) + "\n"


def generate_context_pack(
    conn: sqlite3.Connection, task: str, max_tokens: int = 8000, max_items: int = 20
) -> dict:
    found = find_code_areas(conn, task, limit=12)
    candidates: dict[str, dict] = {}

    def consider(fqn, *, priority, confidence, reason, evidence, kind="class"):
        row = _class_row(conn, fqn)
        if row is None:
            return
        cur = candidates.get(fqn)
        if cur is not None and cur["priority"] <= priority:
            return
        candidates[fqn] = {
            "kind": kind,
            "symbol": row["simple_name"],
            "fqn": fqn,
            "role": row["role"],
            "file_path": row["file_path"],
            "summary": row["summary"],
            "reason": reason,
            "confidence": confidence,
            "evidence": evidence,
            "priority": priority,
            "_id": row["id"],
        }

    # priority 1: controllers behind matched endpoints
    for e in found["endpoints"]:
        if e.get("controller_fqn"):
            consider(
                e["controller_fqn"],
                priority=1,
                confidence="high",
                reason=f"Exposes endpoint {e['http_method']} {e['full_path']}",
                evidence=[ev("mapping_annotation",
                             f"{e['http_method']} {e['full_path']} -> {_simple_type(e['controller_fqn'])}",
                             symbol=_simple_type(e["controller_fqn"]))],
            )

    # priority by role: matched classes
    for c in found["classes"]:
        pr = _ROLE_PRIORITY.get(c["role"], 6)
        conf = "high" if c["role"] == "controller" else "medium" if pr <= 3 else "low"
        consider(
            c["fqn"],
            priority=pr,
            confidence=conf,
            reason=f"Matches task keywords (role: {c['role']})",
            evidence=[ev("keyword_match", f"Class {c['simple_name']} matched task query {task!r}",
                         file_path=c["file_path"], symbol=c["simple_name"], source="structure")],
        )

    # one hop: direct service/repo/entity dependencies injected into selected controllers/services
    for fqn, item in list(candidates.items()):
        if item["role"] not in ("controller", "service"):
            continue
        for f in conn.execute(
            "SELECT name, type_fqn FROM field WHERE class_id = ? AND is_injected = 1", (item["_id"],)
        ):
            dep = _resolve_simple(conn, f["type_fqn"])
            if dep is None:
                continue
            dep_pr = _ROLE_PRIORITY.get(dep["role"], 4)
            consider(
                dep["fqn"],
                priority=dep_pr,
                confidence="medium",
                reason=f"Injected dependency of {item['symbol']}",
                evidence=[ev("field_injection", f"{item['symbol']} injects {f['name']} : {dep['simple_name']}",
                             file_path=item["file_path"], symbol=f"{item['symbol']}.{f['name']}")],
            )

    # ---- budget: select in priority order ------------------------------
    budget_chars = max_tokens * 4
    ordered = sorted(candidates.values(), key=lambda i: (i["priority"], i["fqn"]))
    md_lines = [f"# Context pack: {task}", ""]
    used = sum(len(x) + 1 for x in md_lines)
    selected: list[dict] = []
    excluded: list[dict] = []

    for item in ordered:
        block = _render(item)
        if len(selected) >= max_items or used + len(block) > budget_chars:
            excluded.append({"symbol": item["symbol"], "fqn": item["fqn"],
                             "reason": "Lower relevance or token/item budget exceeded"})
            continue
        used += len(block) + 1
        md_lines.append(block)
        selected.append(
            {k: item[k] for k in ("kind", "symbol", "fqn", "file_path", "reason", "confidence", "evidence")}
        )

    context_markdown = "\n".join(md_lines)
    return {
        "task": task,
        "max_tokens": max_tokens,
        "max_items": max_items,
        "estimated_tokens": _est_tokens(context_markdown),
        "selected_items": selected,
        "excluded_items": excluded,
        "context_markdown": context_markdown,
        "matched": found["counts"],
        # weakest link across everything included — a pack whose tail is
        # low-confidence keyword matches must not inherit the head's "high"
        "confidence": conf_str(min_confidence(s["confidence"] for s in selected))
        if selected else "unknown",
        "limitations": limitations("syntactic_calls", "no_call_graph", "ambiguous_simple_name"),
        "warnings": [] if selected else ["No code areas matched the task query."],
    }
