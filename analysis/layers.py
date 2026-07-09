"""Deterministic Spring-layer inference for a class, as an InferredFinding.

Confidence ladder (per the Stage-4 contract):
    high    — a direct Spring/JAX-RS stereotype annotation is present.
    medium  — class *name* and *package* agree on a layer.
    low     — only the name (or only the package) hints at a layer.
    unknown — no usable signal.
Evidence is reused from the class's observed facts so the finding points back at
exactly the annotation / package / declaration that drove it.
"""

from __future__ import annotations

from analysis.common import ev
from models import ConfidenceLevel, Evidence, InferredFinding, LIMITATIONS

# stereotype annotation (object value, with '@') -> layer + human label
_STEREOTYPES: list[tuple[frozenset[str], str, str]] = [
    (frozenset({"@RestController", "@Controller"}), "controller", "Spring MVC controller"),
    (frozenset({"@Path"}), "controller", "JAX-RS resource"),
    (frozenset({"@Service"}), "service", "Spring service component"),
    (frozenset({"@Repository"}), "repository", "Spring data repository"),
    (frozenset({"@Entity", "@Table", "@MappedSuperclass", "@Embeddable"}), "entity", "JPA persistent type"),
    (frozenset({"@Configuration", "@ConfigurationProperties"}), "config", "Spring configuration"),
    (frozenset({"@Component", "@Provider"}), "component", "Spring/JAX-RS component"),
]

# class-name suffix -> layer
_NAME_LAYER: list[tuple[tuple[str, ...], str]] = [
    (("Controller", "Resource", "RestResource"), "controller"),
    (("Service", "PlatformService", "Manager", "Facade"), "service"),
    (("Repository", "Dao"), "repository"),
    (("Dto", "DTO", "Request", "Response", "Payload", "Form"), "dto"),
    (("Util", "Utils", "Helper", "Constants"), "util"),
]

# package path token -> layer
_PKG_LAYER: list[tuple[tuple[str, ...], str]] = [
    (("controller", "web", "api", "rest", "resource"), "controller"),
    (("service", "application", "usecase"), "service"),
    (("repository", "dao", "persistence", "jpa"), "repository"),
    (("entity", "domain", "model"), "entity"),
    (("dto", "payload"), "dto"),
    (("config",), "config"),
]

# kinds that carry data, not behaviour: a record/enum/annotation is never a
# Spring-managed bean (service/controller/repository/component) just because of
# its name or package — e.g. a value ``record`` living in a ``*.service`` package
# is a DTO, not "possibly a service".
_DATA_CARRIER_KINDS = frozenset({"record", "enum", "annotation"})
# layers that imply a runtime bean with behaviour
_COMPONENT_LAYERS = frozenset({"controller", "service", "repository", "component"})


def _first_fact(facts: list[dict], fact_type: str) -> dict | None:
    return next((f for f in facts if f["fact_type"] == fact_type), None)


def _name_layer(simple_name: str) -> tuple[str, str] | None:
    for suffixes, layer in _NAME_LAYER:
        for suf in suffixes:
            if simple_name.endswith(suf):
                return layer, suf
    return None


def _pkg_layer(package: str | None) -> tuple[str, str] | None:
    if not package:
        return None
    tokens = set(package.lower().split("."))
    for needles, layer in _PKG_LAYER:
        for n in needles:
            if n in tokens:
                return layer, n
    return None


def infer_spring_layer(
    *,
    fqn: str,
    simple_name: str,
    kind: str,
    package: str | None,
    file_path: str,
    class_facts: list[dict],
) -> dict:
    """Return a JSON-able InferredFinding describing the class's likely layer."""
    annotation_objects = {
        f["object"] for f in class_facts if f["fact_type"] == "class_annotation"
    }

    layer: str | None = None
    confidence = ConfidenceLevel.UNKNOWN
    label = "unknown layer"
    evidence: list[dict] = []

    # high: direct stereotype annotation
    for ann_set, st_layer, st_label in _STEREOTYPES:
        hit = annotation_objects & ann_set
        if hit:
            layer, confidence, label = st_layer, ConfidenceLevel.HIGH, st_label
            ann_name = sorted(hit)[0]
            fact = next(
                f for f in class_facts
                if f["fact_type"] == "class_annotation" and f["object"] == ann_name
            )
            evidence = fact["evidence"] or [
                ev("annotation", f"{simple_name} is annotated with {ann_name}",
                   file_path=file_path, symbol=simple_name)
            ]
            break

    if layer is None:
        nm = _name_layer(simple_name)
        pk = _pkg_layer(package)
        # a data carrier (record/enum/annotation) is not a behavioural bean: drop
        # any component-layer guess so a value record in a *.service package is not
        # reported as "possibly a service" (a dto/util/entity guess still stands).
        if kind in _DATA_CARRIER_KINDS:
            if nm and nm[0] in _COMPONENT_LAYERS:
                nm = None
            if pk and pk[0] in _COMPONENT_LAYERS:
                pk = None
        class_decl = _first_fact(class_facts, "class_declaration")
        pkg_decl = _first_fact(class_facts, "package_declaration")
        class_line = (class_decl["evidence"][0]["line_start"] if class_decl and class_decl["evidence"] else None)
        pkg_line = (pkg_decl["evidence"][0]["line_start"] if pkg_decl and pkg_decl["evidence"] else None)

        name_ev = (
            ev("naming", f"Class name '{simple_name}' ends with '{nm[1]}'",
               file_path=file_path, line_start=class_line, symbol=simple_name)
            if nm else None
        )
        pkg_ev = (
            ev("package", f"Package '{package}' contains '{pk[1]}', typical of the {pk[0]} layer",
               file_path=file_path, line_start=pkg_line, symbol=fqn)
            if pk else None
        )

        if nm and pk and nm[0] == pk[0]:
            # medium: name AND package agree
            layer, confidence, label = nm[0], ConfidenceLevel.MEDIUM, f"{nm[0]} (name + package agree)"
            evidence = [name_ev, pkg_ev]
        elif nm:
            layer, confidence, label = nm[0], ConfidenceLevel.LOW, f"{nm[0]} (by class name only)"
            evidence = [name_ev]
        elif pk:
            layer, confidence, label = pk[0], ConfidenceLevel.LOW, f"{pk[0]} (by package only)"
            evidence = [pkg_ev]
        else:
            # unknown: still cite that the class exists (evidence is mandatory)
            layer, confidence, label = "unknown", ConfidenceLevel.UNKNOWN, "layer could not be determined"
            evidence = [
                (class_decl["evidence"][0] if class_decl and class_decl["evidence"] else
                 ev("class_declaration", f"{kind} {simple_name} is declared", file_path=file_path, symbol=fqn))
            ]

    summary = f"Likely {label}" if layer != "unknown" else "Layer could not be determined from source signals"

    finding = InferredFinding(
        finding_type="spring_layer",
        subject=fqn,
        summary=summary,
        evidence=[Evidence(**e) for e in evidence],
        confidence=confidence,
        limitations=[LIMITATIONS["spring_proxies"]],
    )
    out = finding.model_dump(mode="json")
    out["layer"] = layer  # convenience for callers
    return out


def _endpoints_reattributed(conn, class_id: int) -> bool:
    """True if the class owns endpoint rows and every one of them is superseded —
    i.e. its mappings were reattributed to a concrete controller (the openapi
    ``*Api`` contract interfaces). Such a class is a resolved contract, not an
    unclassified controller."""
    try:
        rows = conn.execute(
            "SELECT superseded FROM endpoint WHERE controller_class_id = ?", (class_id,)
        ).fetchall()
    except Exception:  # noqa: BLE001 - old DB without the superseded column: no signal
        return False
    return bool(rows) and all(r["superseded"] for r in rows)


def compute_low_confidence_findings(conn, limit: int = 25) -> list[InferredFinding]:
    """Classes with no stereotype (role 'unknown') but a name/package layer hint.

    Cheap (no per-class facts query): used by the scan to persist into
    ``inferred_findings`` and surfaced by the baseline report. Each finding has
    naming/package evidence and low confidence.
    """
    out: list[InferredFinding] = []
    for r in conn.execute(
        "SELECT cl.id, cl.fqn, cl.simple_name, cl.kind, cl.file_path, p.fqn AS pkg "
        "FROM class cl LEFT JOIN package p ON p.id = cl.package_id "
        "WHERE cl.role = 'unknown' ORDER BY cl.fqn"
    ):
        nm = _name_layer(r["simple_name"])
        pk = _pkg_layer(r["pkg"])
        if not (nm or pk):
            continue
        layer = (nm or pk)[0]
        # B3: a data carrier (record/enum/annotation) is never a behavioural bean —
        # don't guess a component layer for it (kills "value record -> possibly a service").
        if r["kind"] in _DATA_CARRIER_KINDS and layer in _COMPONENT_LAYERS:
            continue
        # B4: a contract interface whose endpoints were all reattributed to a concrete
        # controller is already resolved — don't re-surface it as "possibly a controller"
        # (the openapi-generated *Api interfaces).
        if layer == "controller" and _endpoints_reattributed(conn, r["id"]):
            continue
        reason = f"name '{r['simple_name']}' suggests {nm[0]}" if nm else f"package suggests {pk[0]}"
        out.append(
            InferredFinding(
                finding_type="spring_layer",
                subject=r["fqn"],
                summary=f"Possibly a {layer} (no stereotype annotation)",
                evidence=[
                    Evidence(kind="naming", description=reason, file_path=r["file_path"], symbol=r["simple_name"])
                ],
                confidence=ConfidenceLevel.LOW,
                limitations=[LIMITATIONS["spring_proxies"]],
            )
        )
        if len(out) >= limit:
            break
    return out
