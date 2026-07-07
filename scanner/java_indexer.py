"""Ties java_parser to the SQLite repository: walks .java files and persists
classes/methods/fields/annotations, resolving each file to its module.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from index import repository as repo
from index.queries import _simple_type
from scanner.endpoint_scanner import class_base_path, extract_endpoints, join_paths
from scanner.fact_emitter import FactConfig, class_observed_facts, file_import_facts
from scanner.java_parser import ParsedClass, parse_file
from scanner.repo_scanner import _is_ignored_path, prune_dirnames
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
        prune_dirnames(Path(dirpath), dirnames, repo_root)
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
                candidate = Path(dirpath) / name
                # prune_dirnames only prunes subdirectories to recurse into; a stray
                # .java file directly inside an otherwise-ignored dir (e.g. build/
                # itself, not build/generated/) would still be yielded without this.
                if not _is_ignored_path(candidate, repo_root):
                    yield candidate


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
    # method names of this class, used to record same-class self-calls (helper hops)
    own_methods = {mm.name for mm in pc.methods}

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

        # Persist two kinds of intra-class call that trace follows (deterministic,
        # bounded):
        #   1. calls on a field of this class (controller -> service -> repo hops);
        #   2. same-class self-calls (``this.helper()`` / ``helper()``) so trace can
        #      step one level into a delegating helper. Self-calls carry a NULL
        #      receiver_field and the class's own FQN as receiver_type_fqn.
        seen_calls: set[tuple[str, str]] = set()
        for call in m.calls:
            if call.receiver is not None and call.receiver in field_types:
                receiver_field = call.receiver
                receiver_type = field_types[call.receiver]
            elif call.receiver in (None, "this") and call.name in own_methods and call.name != m.name:
                receiver_field = None
                receiver_type = pc.fqn
            else:
                continue
            key = (receiver_field or "self", call.name)
            if key in seen_calls:
                continue
            seen_calls.add(key)
            repo.insert_method_call(
                conn,
                caller_method_id=method_id,
                caller_class_id=class_id,
                callee_name=call.name,
                receiver_field=receiver_field,
                receiver_type_fqn=receiver_type,
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
            parsed = parse_file(java_file, display_path=str(rel).replace("\\", "/"))
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


def _is_bare_identifier(t: str) -> bool:
    """A plain simple type name — no package, generics, array or wildcard. Only
    these are candidates for same-package FQN resolution."""
    return bool(t) and t.isidentifier()


def resolve_same_package_types(conn) -> int:
    """Second pass: resolve a written type that has no import because it lives in the
    *same package* as the declaring class (Java doesn't require an import for that).

    ``_resolve_types_inplace`` only rewrites types reachable through the file's
    ``import`` statements, so a field/return/parameter typed as a sibling class stays
    a bare simple name while a cross-package one becomes an FQN — an inconsistency
    that leaks into class-dependency edges and the exported flat JSON. This runs once
    the whole project is indexed (so every class fqn is known): for each bare simple
    type on a class in package P, if ``P.<Simple>`` is a real class, rewrite it to
    that FQN. Types with no owning package, or that don't resolve, are left as-is —
    the existing simple-name fallback still covers them.
    """
    known_fqns = {r["fqn"] for r in conn.execute("SELECT fqn FROM class")}
    resolved = 0

    def resolve(rows, table: str) -> None:
        nonlocal resolved
        updates: list[tuple[str, int]] = []
        for r in rows:
            t = r["type_fqn"]
            pkg = r["pkg"]
            if not pkg or not t or not _is_bare_identifier(t):
                continue
            candidate = f"{pkg}.{t}"
            if candidate in known_fqns:
                updates.append((candidate, r["id"]))
        for fqn, row_id in updates:
            conn.execute(f"UPDATE {table} SET type_fqn = ? WHERE id = ?", (fqn, row_id))
        resolved += len(updates)

    resolve(
        conn.execute(
            "SELECT f.id, f.type_fqn, p.fqn AS pkg FROM field f "
            "JOIN class c ON c.id = f.class_id LEFT JOIN package p ON p.id = c.package_id "
            "WHERE f.type_fqn IS NOT NULL AND f.type_fqn NOT LIKE '%.%'"
        ).fetchall(),
        "field",
    )
    resolve(
        conn.execute(
            "SELECT mp.id, mp.type_fqn, p.fqn AS pkg FROM method_parameter mp "
            "JOIN method m ON m.id = mp.method_id JOIN class c ON c.id = m.class_id "
            "LEFT JOIN package p ON p.id = c.package_id "
            "WHERE mp.type_fqn IS NOT NULL AND mp.type_fqn NOT LIKE '%.%'"
        ).fetchall(),
        "method_parameter",
    )
    # method.return_type lives under a different column name — adapt the rows
    ret_rows = conn.execute(
        "SELECT m.id, m.return_type AS type_fqn, p.fqn AS pkg FROM method m "
        "JOIN class c ON c.id = m.class_id LEFT JOIN package p ON p.id = c.package_id "
        "WHERE m.return_type IS NOT NULL AND m.return_type NOT LIKE '%.%'"
    ).fetchall()
    ret_updates = 0
    for r in ret_rows:
        t, pkg = r["type_fqn"], r["pkg"]
        if not pkg or not _is_bare_identifier(t):
            continue
        candidate = f"{pkg}.{t}"
        if candidate in known_fqns:
            conn.execute("UPDATE method SET return_type = ? WHERE id = ?", (candidate, r["id"]))
            ret_updates += 1
    resolved += ret_updates

    conn.commit()
    return resolved


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


_CONTROLLER_ANNOTATIONS = ("@RestController", "@Controller")


def reattribute_interface_endpoints(conn) -> int:
    """Post-pass: resolve REST endpoints whose HTTP mapping annotation lives on an
    ancestor interface method rather than directly on the @RestController/@Controller
    class (openapi-generator / Feign-style codegen: ``ControllerImpl implements
    FeignClient extends BaseApi``, sometimes several hops deep, sometimes with several
    concrete controllers behind the same shared interface).

    Walks each controller *method's own* implemented-interface chain (breadth-first,
    arbitrary depth) looking for a same-name/same-arity ancestor method that carries a
    mapping annotation, and creates the endpoint attributed to the concrete controller
    + its own method, using the controller's *own* class-level base path (not the
    interface's -- a controller can add its own @RequestMapping prefix). Each concrete
    controller is resolved independently by walking *up* from itself, so N sibling
    implementations of a shared interface each get their own correctly-attributed
    endpoint -- no arbitrary pick, no merging needed. The original interface-level
    endpoint row is dropped only when every controller behind the interface produced
    its own replacement; if some sibling didn't, it's kept (the
    ``interface_impl_unresolved`` limitation stays honest for those).

    Known limitation: only methods *overridden in the concrete controller* are
    reattributed. In the delegate pattern (``ApiController implements ApiApi`` where
    the controller inherits ``default`` methods without overriding them) the endpoint
    stays attributed to the interface -- honest, but the concrete controller is not
    linked. Detecting inherited-but-not-overridden handlers would require attributing
    an endpoint to a class that has no matching method row, which the schema's
    handler_method_id semantics don't support today."""
    controller_annotated: set[int] = {
        r["class_id"] for r in conn.execute(
            "SELECT DISTINCT class_id FROM class_annotation WHERE name IN (?, ?)",
            _CONTROLLER_ANNOTATIONS,
        )
    }
    if not controller_annotated:
        return 0

    # class_interface.interface_fqn is written as-seen in source (often an
    # unqualified simple name, e.g. "DealsApi") -- resolve FQN-first, falling back
    # to simple-name like index_class_dependencies does.
    name_to_ids: dict[str, list[int]] = {}
    fqn_to_id: dict[str, int] = {}
    for r in conn.execute("SELECT id, simple_name, fqn FROM class"):
        name_to_ids.setdefault(r["simple_name"], []).append(r["id"])
        fqn_to_id[r["fqn"]] = r["id"]

    def resolve_ref(ref: str) -> list[int]:
        if ref in fqn_to_id:
            return [fqn_to_id[ref]]
        # unlike read-time search (where linking every same-name candidate just
        # over-approximates results), this is a write path that mutates endpoint
        # rows -- an ambiguous simple name could walk into an unrelated class and
        # attribute its annotation as if it were this controller's own mapping.
        # Be conservative: skip it, leaving the interface-level endpoint row
        # unclaimed (honest `interface_impl_unresolved`) rather than guessing.
        candidates = name_to_ids.get(_simple_type(ref) or ref, [])
        return candidates if len(candidates) == 1 else []

    parents: dict[int, list[int]] = {}
    for r in conn.execute("SELECT class_id, interface_fqn FROM class_interface"):
        parents.setdefault(r["class_id"], []).extend(resolve_ref(r["interface_fqn"]))

    methods_by_class: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT id, class_id, name, return_type FROM method"):
        methods_by_class.setdefault(r["class_id"], []).append(dict(r))

    param_types: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT method_id, type_fqn FROM method_parameter ORDER BY method_id, position"
    ):
        param_types.setdefault(r["method_id"], []).append(_simple_type(r["type_fqn"]) or r["type_fqn"])

    # preload every method/class annotation once (the reattribution BFS otherwise
    # re-queries these per method, including deep inside the ancestor walk).
    method_anns: dict[int, list[tuple[str, str | None]]] = {}
    for r in conn.execute("SELECT method_id, name, attributes FROM method_annotation"):
        method_anns.setdefault(r["method_id"], []).append((r["name"], r["attributes"]))
    class_anns: dict[int, list[tuple[str, str | None]]] = {}
    for r in conn.execute("SELECT class_id, name, attributes FROM class_annotation"):
        class_anns.setdefault(r["class_id"], []).append((r["name"], r["attributes"]))

    def method_annotations(method_id: int) -> list[tuple[str, str | None]]:
        return method_anns.get(method_id, [])

    def type_base_path(class_id: int) -> str | None:
        """The type-level base path Spring applies to ``class_id``'s handlers: the
        class's own class-level @RequestMapping/@Path if it has one, otherwise the
        nearest one inherited from an ancestor interface (openapi-generator puts
        @RequestMapping('/api/v1') on the interface and leaves the impl bare). The
        controller's own mapping wins over the interface's — same as Spring."""
        seen: set[int] = set()
        queue = deque([class_id])
        while queue:
            cid = queue.popleft()
            if cid in seen:
                continue
            seen.add(cid)
            bp = class_base_path(class_anns.get(cid, []))
            if bp:
                return bp
            queue.extend(parents.get(cid, []))
        return None

    def find_ancestor_endpoint(class_id: int, name: str, params: list[str]):
        """BFS the implements/extends chain for a same-name/same-parameter-types
        ancestor method that carries its own mapping annotation. Matching on
        parameter types (not just arity) avoids attributing the wrong mapping to
        an overload that merely happens to take the same number of arguments."""
        seen: set[int] = set()
        queue = deque(parents.get(class_id, []))
        while queue:
            cid = queue.popleft()
            if cid in seen:
                continue
            seen.add(cid)
            for m in methods_by_class.get(cid, []):
                if m["name"] != name or param_types.get(m["id"], []) != params:
                    continue
                eps = extract_endpoints(method_annotations(m["id"]))
                if eps:
                    return m, eps
            queue.extend(parents.get(cid, []))
        return None, []

    def ancestor_closure(class_id: int) -> set[int]:
        out: set[int] = set()
        queue = deque(parents.get(class_id, []))
        while queue:
            cid = queue.popleft()
            if cid in out:
                continue
            out.add(cid)
            queue.extend(parents.get(cid, []))
        return out

    controller_ancestors = {cid: ancestor_closure(cid) for cid in controller_annotated}

    # For each interface (ancestor) mapping method:
    #   claims  — controllers that reattributed it (produced a replacement row);
    #   covered — controllers that *represent* it, either by reattributing it OR by
    #             overriding it with their own mapping annotation (H4: an override
    #             carrying its own @GetMapping is indexed directly and no longer
    #             needs the interface row, so it must count towards coverage).
    # src_owner maps the method to the class that declares the annotation.
    claims: dict[int, set[int]] = {}
    covered: dict[int, set[int]] = {}
    src_owner: dict[int, int] = {}
    created = 0

    for class_id in controller_annotated:
        base_path = type_base_path(class_id)

        for m in methods_by_class.get(class_id, []):
            own_eps = extract_endpoints(method_annotations(m["id"]))
            src_method, anc_eps = find_ancestor_endpoint(
                class_id, m["name"], param_types.get(m["id"], [])
            )
            if own_eps:
                # already indexed from its own mapping annotation; if it also
                # overrides an ancestor mapping method, record that this controller
                # covers the interface row so it can be superseded.
                if src_method is not None and anc_eps:
                    covered.setdefault(src_method["id"], set()).add(class_id)
                    src_owner[src_method["id"]] = src_method["class_id"]
                continue
            if not anc_eps:
                continue

            for ep in anc_eps:
                full_path = join_paths(base_path, ep.sub_path)
                exists = conn.execute(
                    "SELECT 1 FROM endpoint WHERE controller_class_id = ? AND handler_method_id = ? "
                    "AND http_method = ? AND full_path = ?",
                    (class_id, m["id"], ep.http_method, full_path),
                ).fetchone()
                if exists:
                    continue
                repo.insert_endpoint(
                    conn,
                    http_method=ep.http_method,
                    path=ep.sub_path or "",
                    full_path=full_path,
                    controller_class_id=class_id,
                    handler_method_id=m["id"],
                    produces=ep.produces,
                    consumes=ep.consumes,
                    response_dto_fqn=m["return_type"],
                    # the mapping annotation itself lives on the ancestor method, not
                    # here — keep that truthful for evidence, even though the DI-trace
                    # should start from the concrete controller/method above.
                    annotation_class_id=src_method["class_id"],
                    annotation_method_id=src_method["id"],
                    commit=False,
                )
                created += 1

            claims.setdefault(src_method["id"], set()).add(class_id)
            covered.setdefault(src_method["id"], set()).add(class_id)
            src_owner[src_method["id"]] = src_method["class_id"]

    # Supersede (hide — never delete) an interface-level endpoint row only when
    # EVERY controller behind that interface represents it (reattributed it or
    # overrides it with its own mapping). If a sibling doesn't (it inherits the
    # default method without overriding — delegate pattern — or its interface link
    # failed to resolve), the interface row stays visible as that sibling's only
    # honest representation. Superseding instead of deleting means a heuristic
    # mistake degrades to a hidden-but-recoverable row, not a vanished endpoint.
    for src_id, coverers in covered.items():
        owner = src_owner[src_id]
        implementors = {
            cid for cid, ancestors in controller_ancestors.items() if owner in ancestors
        }
        if implementors - coverers:
            continue
        conn.execute(
            "UPDATE endpoint SET superseded = 1 WHERE handler_method_id = ? AND superseded = 0",
            (src_id,),
        )

    conn.commit()
    return created
