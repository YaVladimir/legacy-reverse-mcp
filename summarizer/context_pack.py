"""Build a compact, task-scoped context pack for an agent.

Retrieval + assembly (no LLM): find the code areas relevant to a task, then
render endpoints (with a heuristic trace), classes (trimmed detail) and the
involved module context, trimmed to a token budget (~chars/4).
"""

from __future__ import annotations

import sqlite3


def _est_tokens(text: str) -> int:
    return len(text) // 4


def generate_context_pack(conn: sqlite3.Connection, task: str, max_tokens: int = 4000) -> dict:
    from index.queries import class_detail, find_code_areas, module_map, trace_endpoint

    budget_chars = max_tokens * 4
    found = find_code_areas(conn, task, limit=8)

    lines: list[str] = [f"# Context pack: {task}", ""]
    truncated = False

    def room_for(block: str) -> bool:
        nonlocal truncated
        if sum(len(x) + 1 for x in lines) + len(block) > budget_chars:
            truncated = True
            return False
        return True

    involved_modules: set[str] = set()

    # 1) relevant endpoints (+ short trace of the first one or two)
    if found["endpoints"]:
        section = ["## Relevant endpoints", ""]
        for i, e in enumerate(found["endpoints"][:6]):
            ctrl = (e.get("controller_fqn") or "").split(".")[-1]
            line = f"- `{e['http_method']} {e['full_path']}` -> {ctrl}.{e.get('handler_name')}"
            if i < 2 and e.get("id") is not None:
                tr = trace_endpoint(conn, e["id"])
                if tr:
                    chain = " -> ".join(
                        dict.fromkeys(s["role"] for s in tr["steps"])  # ordered unique roles
                    )
                    line += f"  \n    trace: {chain} (confidence: {tr['confidence']})"
            section.append(line)
        section.append("")
        block = "\n".join(section)
        if room_for(block):
            lines.append(block)

    # 2) relevant classes (trimmed detail)
    if found["classes"]:
        header_added = False
        for c in found["classes"][:6]:
            d = class_detail(conn, c["fqn"])
            if not d:
                continue
            involved_modules.add(d["module"] or "")
            deps = ", ".join(dep["name"] for dep in d["injected_dependencies"][:6])
            key_methods = ", ".join(m["name"] for m in d["methods"][:6])
            parts = [
                f"### {d['simple_name']}  ({d['role']}, module `{d['module']}`)",
                f"`{d['fqn']}`",
            ]
            if d.get("summary"):
                parts.append(d["summary"])
            if deps:
                parts.append(f"Injected: {deps}")
            if key_methods:
                parts.append(f"Methods: {key_methods}")
            block = ("## Relevant classes\n\n" if not header_added else "") + "\n".join(parts) + "\n"
            if room_for(block):
                lines.append(block)
                header_added = True
            else:
                break

    # 3) module context for involved modules
    involved_modules.discard("")
    if involved_modules:
        mm = module_map(conn)
        section = ["## Module context", ""]
        for m in mm["modules"]:
            if m["name"] in involved_modules:
                section.append(
                    f"- `{m['name']}`: {m['classes']} classes, {m['endpoints']} endpoints; "
                    f"depends on {', '.join(m['depends_on']) or '(none)'}"
                )
        section.append("")
        block = "\n".join(section)
        if room_for(block):
            lines.append(block)

    markdown = "\n".join(lines)
    return {
        "task": task,
        "max_tokens": max_tokens,
        "estimated_tokens": _est_tokens(markdown),
        "truncated": truncated,
        "markdown": markdown,
        "matched": found["counts"],
    }
