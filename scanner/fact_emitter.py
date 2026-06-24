"""Stage 3: turn a parsed class into ObservedFacts with evidence.

Every fact here is something read *directly* from source (package/class/method/
field declarations, annotations, REST mappings), so it is recorded at ``high``
confidence. Heuristic conclusions (role = controller, endpoint purpose, …) are
*not* produced here — those are InferredFindings for a later stage.

Volume control: legacy repos have tens of thousands of methods/fields, most of
them uninteresting. By default we only emit method/field facts that carry signal
(annotated members, REST handlers, injected fields). ``FactConfig`` flips the
high-volume categories on when an exhaustive fact base is wanted.
"""

from __future__ import annotations

from dataclasses import dataclass

from models import ConfidenceLevel, Evidence, ObservedFact
from scanner.endpoint_scanner import (
    _JAXRS_VERBS,
    _SPRING_VERB_MAPPINGS,
    class_base_path,
    extract_endpoints,
    join_paths,
)
from scanner.java_parser import ParsedAnnotation, ParsedClass, ParsedFile, ParsedMethod
from scanner.spring_scanner import class_uses_constructor_di, field_is_injected

# Annotation names whose meaning is "this method/class is a REST mapping". These
# are represented by richer ``mapping_annotation`` facts (with the resolved
# verb + full path), so they are skipped by the generic method-annotation pass.
_MAPPING_RELATED: frozenset[str] = frozenset(
    set(_JAXRS_VERBS)
    | set(_SPRING_VERB_MAPPINGS)
    | {"@RequestMapping", "@Path", "@Produces", "@Consumes"}
)


@dataclass
class FactConfig:
    """Which (potentially high-volume) fact categories to record."""

    record_all_methods: bool = False   # method facts even for unannotated, non-handler methods
    record_all_fields: bool = False    # field facts even for plain, un-annotated fields
    record_imports: bool = False       # one fact per import statement (very high volume)


def _anno_tuples(annos: list[ParsedAnnotation]) -> list[tuple[str, str | None]]:
    return [(a.name, a.attributes) for a in annos]


def _attrs_suffix(attrs: str | None) -> str:
    return attrs.strip() if attrs else ""


def _mapping_annotation_for(method: ParsedMethod, http_method: str) -> ParsedAnnotation | None:
    """Find the annotation on ``method`` that produced an endpoint with ``http_method``."""
    by_name = {a.name: a for a in method.annotations}
    # JAX-RS verb annotation, e.g. POST -> @POST
    jaxrs = f"@{http_method}"
    if jaxrs in by_name:
        return by_name[jaxrs]
    # Spring shortcut mapping, e.g. POST -> @PostMapping
    for ann_name, verb in _SPRING_VERB_MAPPINGS.items():
        if verb == http_method and ann_name in by_name:
            return by_name[ann_name]
    # generic @RequestMapping(method=...) or ANY
    return by_name.get("@RequestMapping")


def _ev(kind: str, description: str, file_path: str, *, line=None, line_end=None, symbol=None) -> Evidence:
    return Evidence(
        kind=kind,
        description=description,
        file_path=file_path,
        line_start=line,
        line_end=line_end,
        symbol=symbol,
    )


def class_observed_facts(
    pc: ParsedClass, config: FactConfig | None = None
) -> list[ObservedFact]:
    """Build the list of ObservedFacts for a single parsed class."""
    config = config or FactConfig()
    fp = pc.file_path
    facts: list[ObservedFact] = []

    # --- package declaration ---------------------------------------------
    if pc.package:
        facts.append(
            ObservedFact(
                fact_type="package_declaration",
                subject=pc.fqn,
                predicate="declared_in_package",
                object=pc.package,
                evidence=[
                    _ev(
                        "package_declaration",
                        f"{pc.simple_name} is declared in package {pc.package}",
                        fp,
                        line=pc.package_line,
                        symbol=pc.fqn,
                    )
                ],
            )
        )

    # --- class declaration -----------------------------------------------
    facts.append(
        ObservedFact(
            fact_type="class_declaration",
            subject=pc.fqn,
            predicate="is_a",
            object=pc.kind,
            evidence=[
                _ev(
                    "class_declaration",
                    f"{pc.kind} {pc.simple_name} is declared",
                    fp,
                    line=pc.line_start,
                    line_end=pc.line_end,
                    symbol=pc.fqn,
                )
            ],
        )
    )

    # --- class annotations (stereotypes etc.) ----------------------------
    for ann in pc.annotations:
        facts.append(
            ObservedFact(
                fact_type="class_annotation",
                subject=pc.fqn,
                predicate="is_annotated_with",
                object=ann.name,
                evidence=[
                    _ev(
                        "annotation",
                        f"Class {pc.simple_name} is annotated with {ann.name}{_attrs_suffix(ann.attributes)}",
                        fp,
                        line=ann.line,
                        symbol=pc.simple_name,
                    )
                ],
            )
        )

    # --- methods, method annotations, REST mappings ----------------------
    base_path = class_base_path(_anno_tuples(pc.annotations))
    for m in pc.methods:
        endpoints = extract_endpoints(_anno_tuples(m.annotations))
        interesting = bool(m.annotations) or bool(endpoints)
        symbol = f"{pc.simple_name}#{m.name}"

        if config.record_all_methods or interesting:
            facts.append(
                ObservedFact(
                    fact_type="method_declaration",
                    subject=f"{pc.fqn}#{m.name}",
                    predicate="declares_method",
                    object=m.signature,
                    evidence=[
                        _ev(
                            "method_declaration",
                            f"Method {m.signature} is declared on {pc.simple_name}",
                            fp,
                            line=m.line_start,
                            line_end=m.line_end,
                            symbol=symbol,
                        )
                    ],
                )
            )

        # REST mapping facts (resolved verb + full path)
        for ep in endpoints:
            full_path = join_paths(base_path, ep.sub_path)
            ann = _mapping_annotation_for(m, ep.http_method)
            ann_label = ann.name if ann else "@RequestMapping"
            facts.append(
                ObservedFact(
                    fact_type="mapping_annotation",
                    subject=f"{pc.fqn}#{m.name}",
                    predicate="maps_http",
                    object=f"{ep.http_method} {full_path}",
                    evidence=[
                        _ev(
                            "mapping_annotation",
                            f"Method {m.name} has {ann_label}{_attrs_suffix(ann.attributes) if ann else ''} "
                            f"-> {ep.http_method} {full_path}",
                            fp,
                            line=ann.line if ann else m.line_start,
                            symbol=symbol,
                        )
                    ],
                )
            )

        # other method annotations (@Scheduled, @KafkaListener, @Transactional, …)
        for ann in m.annotations:
            if ann.name in _MAPPING_RELATED:
                continue
            facts.append(
                ObservedFact(
                    fact_type="method_annotation",
                    subject=f"{pc.fqn}#{m.name}",
                    predicate="is_annotated_with",
                    object=ann.name,
                    evidence=[
                        _ev(
                            "annotation",
                            f"Method {m.name} is annotated with {ann.name}{_attrs_suffix(ann.attributes)}",
                            fp,
                            line=ann.line,
                            symbol=symbol,
                        )
                    ],
                )
            )

    # --- fields + field annotations --------------------------------------
    # Mirror the indexer's injection detection so Lombok constructor-injected
    # final fields (the common Fineract pattern) count as "interesting" too.
    uses_ctor_di = class_uses_constructor_di({a.name for a in pc.annotations})
    for f in pc.fields:
        injected = field_is_injected(
            {a.name for a in f.annotations},
            is_final=f.is_final,
            is_static=f.is_static,
            class_uses_ctor_di=uses_ctor_di,
            ctor_assigned=f.name in pc.ctor_injected_fields,
        )
        interesting = injected or bool(f.annotations)
        if not (config.record_all_fields or interesting):
            continue
        fsym = f"{pc.simple_name}.{f.name}"
        facts.append(
            ObservedFact(
                fact_type="field",
                subject=f"{pc.fqn}.{f.name}",
                predicate="has_type",
                object=f.type_fqn,
                evidence=[
                    _ev(
                        "field",
                        f"Field {f.name} of type {f.type_fqn} is declared on {pc.simple_name}",
                        fp,
                        line=f.line,
                        symbol=fsym,
                    )
                ],
            )
        )
        for ann in f.annotations:
            facts.append(
                ObservedFact(
                    fact_type="field_annotation",
                    subject=f"{pc.fqn}.{f.name}",
                    predicate="is_annotated_with",
                    object=ann.name,
                    evidence=[
                        _ev(
                            "annotation",
                            f"Field {f.name} is annotated with {ann.name}{_attrs_suffix(ann.attributes)}",
                            fp,
                            line=ann.line,
                            symbol=fsym,
                        )
                    ],
                )
            )

    return facts


def file_import_facts(parsed: ParsedFile, config: FactConfig | None = None) -> list[ObservedFact]:
    """Optional, high-volume: one fact per import, attributed to each top-level class."""
    config = config or FactConfig()
    if not config.record_imports or not parsed.imports:
        return []
    facts: list[ObservedFact] = []
    fp = parsed.file_path
    for pc in parsed.classes:
        for imp in parsed.imports:
            facts.append(
                ObservedFact(
                    fact_type="import",
                    subject=pc.fqn,
                    predicate="imports",
                    object=imp,
                    confidence=ConfidenceLevel.HIGH,
                    evidence=[_ev("import", f"{pc.simple_name} imports {imp}", fp, symbol=pc.fqn)],
                )
            )
    return facts
