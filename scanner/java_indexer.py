"""Ties java_parser to the SQLite repository: walks .java files and persists
classes/methods/fields/annotations, resolving each file to its module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from index import repository as repo
from index.queries import _simple_type
from scanner.endpoint_scanner import class_base_path, extract_endpoints, join_paths
from scanner.fact_emitter import FactConfig, class_observed_facts, file_import_facts
from scanner.java_parser import ParsedClass, parse_file
from scanner.repo_scanner import IGNORED_DIRS
from scanner.spring_scanner import (
    class_uses_constructor_di,
    classify_role,
    field_is_injected,
)


@dataclass
class IndexStats:
    files_parsed: int = 0
    files_failed: int = 0
    classes: int = 0
    methods: int = 0
    fields: int = 0
    endpoints: int = 0
    method_calls: int = 0
    observed_facts: int = 0
    failures: list[tuple[str, str]] = dc_field(default_factory=list)


def _normalize(p: str) -> str:
    p = p.replace("\\", "/").strip("/")
    # a root module path of "." is the catch-all; normalise it to "" so classes
    # at the repo root resolve to it (otherwise single-module repos get module_id NULL).
    return "" if p == "." else p


def _build_module_lookup(conn) -> list[tuple[int, str]]:
    """Return (module_id, normalized_path) sorted by path length desc (longest prefix first)."""
    modules = repo.list_modules(conn)
    lookup = [(m["id"], _normalize(m["path"])) for m in modules]
    # "." (root) becomes "" — keep it last as the catch-all fallback
    lookup.sort(key=lambda t: len(t[1]), reverse=True)
    return lookup


def _resolve_module(rel_path: str, lookup: list[tuple[int, str]]) -> int | None:
    rel = _normalize(rel_path)
    for module_id, mod_path in lookup:
        if mod_path == "":
            return module_id  # root catch-all
        if rel == mod_path or rel.startswith(mod_path + "/"):
            return module_id
    return None


def _iter_java_files(repo_root: Path, skip_tests: bool):
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        if skip_tests:
            parts = Path(dirpath).parts
            # skip standard test source roots: .../src/test/...
            if "src" in parts:
                i = parts.index("src")
                if i + 1 < len(parts) and parts[i + 1] in ("test", "integrationTest"):
                    dirnames[:] = []
                    continue
        for name in filenames:
            if name.endswith(".java"):
                yield Path(dirpath) / name


def _import_map(imports: list[str]) -> dict[str, str]:
    """{simpleName: fqn} from a file's import statements (skip wildcards; first wins)."""
    out: dict[str, str] = {}
    for imp in imports:
        if imp.endswith(".*"):
            continue
        out.setdefault(imp.rsplit(".", 1)[-1], imp)
    return out


def _resolve_types_inplace(pc: ParsedClass, imap: dict[str, str]) -> None:
    """Rewrite written type names to FQN using the file's imports, so type
    references match a unique class instead of every same-named candidate.

    Interfaces are left as written: ``_find_impl`` matches ``class_interface``
    against the simple name, so resolving them would break impl resolution.
    """
    if not imap:
        return

    def r(t: str | None) -> str | None:
        if not t:
            return t
        simple = _simple_type(t)
        return imap.get(simple, t) if simple else t

    pc.superclass_fqn = r(pc.superclass_fqn)
    for f in pc.fields:
        f.type_fqn = r(f.type_fqn)
    for m in pc.methods:
        m.return_type = r(m.return_type)
        for p in m.parameters:
            p.type_fqn = r(p.type_fqn)


def _persist_class(
    conn,
    pc: ParsedClass,
    module_id: int | None,
    stats: IndexStats,
    fact_config: FactConfig | None = None,
) -> None:
    package_id = None
    if pc.package:
        package_id = repo.insert_package(conn, fqn=pc.package, module_id=module_id, commit=False)

    class_ann_names = {a.name for a in pc.annotations}
    role = classify_role(class_ann_names, pc.simple_name, pc.kind)
    uses_ctor_di = class_uses_constructor_di(class_ann_names)
    base_path = class_base_path([(a.name, a.attributes) for a in pc.annotations])

    class_id = repo.insert_class(
        conn,
        fqn=pc.fqn,
        simple_name=pc.simple_name,
        file_path=pc.file_path,
        package_id=package_id,
        module_id=module_id,
        line_start=pc.line_start,
        line_end=pc.line_end,
        kind=pc.kind,
        role=role,
        is_abstract=pc.is_abstract,
        visibility=pc.visibility,
        superclass_fqn=pc.superclass_fqn,
        commit=False,
    )
    # fresh-insert semantics: drop any previously indexed members for this class
    repo.clear_class_members(conn, class_id, commit=False)

    for ann in pc.annotations:
        repo.insert_class_annotation(conn, class_id, ann.name, ann.attributes, commit=False)
    for iface in pc.interfaces:
        repo.insert_class_interface(conn, class_id, iface, commit=False)

    # field name -> declared type, used to resolve call receivers (controller -> service)
    field_types = {f.name: f.type_fqn for f in pc.fields}

    for m in pc.methods:
        method_id = repo.insert_method(
            conn,
            class_id=class_id,
            name=m.name,
            signature=m.signature,
            return_type=m.return_type,
            visibility=m.visibility,
            is_static=m.is_static,
            is_constructor=m.is_constructor,
            line_start=m.line_start,
            line_end=m.line_end,
            commit=False,
        )
        for ann in m.annotations:
            repo.insert_method_annotation(conn, method_id, ann.name, ann.attributes, commit=False)
        for p in m.parameters:
            repo.insert_method_parameter(conn, method_id, p.position, p.name, p.type_fqn, commit=False)
        stats.methods += 1

        # persist only calls whose receiver is a field of this class (deterministic,
        # bounded): these are the controller->service->repository hops trace needs.
        seen_calls: set[tuple[str, str]] = set()
        for call in m.calls:
            if call.receiver is None or call.receiver not in field_types:
                continue
            key = (call.receiver, call.name)
            if key in seen_calls:
                continue
            seen_calls.add(key)
            repo.insert_method_call(
                conn,
                caller_method_id=method_id,
                caller_class_id=class_id,
                callee_name=call.name,
                receiver_field=call.receiver,
                receiver_type_fqn=field_types[call.receiver],
                line=call.line,
                commit=False,
            )
            stats.method_calls += 1

        for ep in extract_endpoints([(a.name, a.attributes) for a in m.annotations]):
            repo.insert_endpoint(
                conn,
                http_method=ep.http_method,
                path=ep.sub_path or "",
                full_path=join_paths(base_path, ep.sub_path),
                controller_class_id=class_id,
                handler_method_id=method_id,
                produces=ep.produces,
                consumes=ep.consumes,
                response_dto_fqn=m.return_type,
                commit=False,
            )
            stats.endpoints += 1

    for f in pc.fields:
        field_ann_names = {a.name for a in f.annotations}
        injected = field_is_injected(
            field_ann_names,
            is_final=f.is_final,
            is_static=f.is_static,
            class_uses_ctor_di=uses_ctor_di,
            ctor_assigned=f.name in pc.ctor_injected_fields,
        )
        ann_names = json.dumps([a.name for a in f.annotations]) if f.annotations else None
        repo.insert_field(
            conn,
            class_id=class_id,
            name=f.name,
            type_fqn=f.type_fqn,
            visibility=f.visibility,
            is_static=f.is_static,
            is_injected=injected,
            annotation_names=ann_names,
            commit=False,
        )
        stats.fields += 1

    # observed facts (Stage 3): direct, high-confidence facts with evidence.
    if fact_config is not None:
        for fact in class_observed_facts(pc, fact_config):
            repo.insert_observed_fact(conn, fact, commit=False)
            stats.observed_facts += 1

    stats.classes += 1


def index_repo(
    conn,
    repo_path: str,
    skip_tests: bool = True,
    progress_every: int = 0,
    emit_facts: bool = True,
    fact_config: FactConfig | None = None,
) -> IndexStats:
    repo_root = Path(repo_path).resolve()
    lookup = _build_module_lookup(conn)
    stats = IndexStats()

    # Stage 3: record observed facts by default. ``None`` disables emission so the
    # member-only index can still be built (e.g. for fast/legacy callers).
    fact_config = fact_config if fact_config is not None else (FactConfig() if emit_facts else None)
    if fact_config is not None:
        repo.clear_observed_facts(conn, commit=False)

    for java_file in _iter_java_files(repo_root, skip_tests):
        rel = java_file.relative_to(repo_root)
        module_id = _resolve_module(str(rel), lookup)
        try:
            parsed = parse_file(java_file)
        except Exception as exc:  # noqa: BLE001 - keep scanning on any single-file failure
            stats.files_failed += 1
            stats.failures.append((str(rel), f"{type(exc).__name__}: {exc}"))
            continue

        imap = _import_map(parsed.imports)
        for pc in parsed.classes:
            _resolve_types_inplace(pc, imap)
            _persist_class(conn, pc, module_id, stats, fact_config)

        if fact_config is not None:
            for fact in file_import_facts(parsed, fact_config):
                repo.insert_observed_fact(conn, fact, commit=False)
                stats.observed_facts += 1

        conn.commit()  # one commit per file
        stats.files_parsed += 1
        if progress_every and stats.files_parsed % progress_every == 0:
            print(f"  ... {stats.files_parsed} files, {stats.classes} classes")

    return stats


def index_class_dependencies(conn) -> int:
    """Post-pass: derive intra-project class->class edges from already-indexed data.

    FQN-first matching: a reference resolved (via imports) to a known class fqn
    links exactly that class; otherwise it falls back to linking every
    same-simple-name candidate (over-approximation, so change-impact never misses
    a dependent). Interfaces stay simple-name (see _resolve_types_inplace).
    """
    repo.clear_class_dependencies(conn, commit=False)

    name_to_ids: dict[str, list[int]] = {}
    fqn_to_id: dict[str, int] = {}
    for r in conn.execute("SELECT id, simple_name, fqn FROM class"):
        name_to_ids.setdefault(r["simple_name"], []).append(r["id"])
        fqn_to_id[r["fqn"]] = r["id"]

    # collect references per class in a few bulk queries (not per-class); keep the
    # written type as-is so an FQN can be matched exactly before simple-name.
    refs: dict[int, list[tuple[str | None, str]]] = {}

    def add(cid, ref, kind):
        refs.setdefault(cid, []).append((ref, kind))

    for r in conn.execute("SELECT id, superclass_fqn FROM class WHERE superclass_fqn IS NOT NULL"):
        add(r["id"], r["superclass_fqn"], "inheritance")
    for r in conn.execute("SELECT class_id, interface_fqn FROM class_interface"):
        add(r["class_id"], r["interface_fqn"], "inheritance")
    for r in conn.execute("SELECT class_id, type_fqn FROM field WHERE is_injected = 1"):
        add(r["class_id"], r["type_fqn"], "field_injection")
    for r in conn.execute("SELECT class_id, return_type FROM method WHERE return_type IS NOT NULL"):
        add(r["class_id"], r["return_type"], "return_type")
    for r in conn.execute(
        "SELECT m.class_id AS class_id, mp.type_fqn AS t FROM method_parameter mp "
        "JOIN method m ON m.id = mp.method_id WHERE mp.type_fqn IS NOT NULL"
    ):
        add(r["class_id"], r["t"], "method_param")

    edges = 0
    for cid, ref_list in refs.items():
        seen: set[tuple[int, str]] = set()
        for ref, kind in ref_list:
            if not ref:
                continue
            if ref in fqn_to_id:
                targets = [fqn_to_id[ref]]  # precise: resolved to a unique class
            else:
                simple = _simple_type(ref)
                targets = name_to_ids.get(simple, []) if simple else []
            for tid in targets:
                if tid == cid or (tid, kind) in seen:
                    continue
                seen.add((tid, kind))
                repo.insert_class_dependency(conn, cid, tid, kind, commit=False)
                edges += 1

    conn.commit()
    return edges
