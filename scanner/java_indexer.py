"""Ties java_parser to the SQLite repository: walks .java files and persists
classes/methods/fields/annotations, resolving each file to its module.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from index import repository as repo
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
    failures: list[tuple[str, str]] = dc_field(default_factory=list)


def _normalize(p: str) -> str:
    return p.replace("\\", "/").strip("/")


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


def _persist_class(conn, pc: ParsedClass, module_id: int | None, stats: IndexStats) -> None:
    package_id = None
    if pc.package:
        package_id = repo.insert_package(conn, fqn=pc.package, module_id=module_id, commit=False)

    class_ann_names = {a.name for a in pc.annotations}
    role = classify_role(class_ann_names, pc.simple_name, pc.kind)
    uses_ctor_di = class_uses_constructor_di(class_ann_names)

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

    for f in pc.fields:
        field_ann_names = {a.name for a in f.annotations}
        injected = field_is_injected(
            field_ann_names,
            is_final=f.is_final,
            is_static=f.is_static,
            class_uses_ctor_di=uses_ctor_di,
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

    stats.classes += 1


def index_repo(conn, repo_path: str, skip_tests: bool = True, progress_every: int = 0) -> IndexStats:
    repo_root = Path(repo_path).resolve()
    lookup = _build_module_lookup(conn)
    stats = IndexStats()

    for java_file in _iter_java_files(repo_root, skip_tests):
        rel = java_file.relative_to(repo_root)
        module_id = _resolve_module(str(rel), lookup)
        try:
            parsed = parse_file(java_file)
        except Exception as exc:  # noqa: BLE001 - keep scanning on any single-file failure
            stats.files_failed += 1
            stats.failures.append((str(rel), f"{type(exc).__name__}: {exc}"))
            continue

        for pc in parsed.classes:
            _persist_class(conn, pc, module_id, stats)

        conn.commit()  # one commit per file
        stats.files_parsed += 1
        if progress_every and stats.files_parsed % progress_every == 0:
            print(f"  ... {stats.files_parsed} files, {stats.classes} classes")

    return stats
