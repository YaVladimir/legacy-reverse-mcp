"""Spring / JAX-RS role classification.

Pure functions that turn a class's annotation set (and a few naming hints)
into a schema ``class.role`` value, plus constructor-injection detection.
Kept dependency-free so it can be unit-tested and reused by the indexer.

role vocabulary (matches schema):
    controller | service | repository | entity | dto | config | component | util | unknown
"""

from __future__ import annotations

# Annotation -> role, checked in priority order (first match wins).
# Controllers first: a JAX-RS resource is usually also @Component, and a
# Spring controller may also carry stereotype meta-annotations.
_ROLE_BY_ANNOTATION: list[tuple[frozenset[str], str]] = [
    (frozenset({"@RestController", "@Controller"}), "controller"),
    (frozenset({"@Path"}), "controller"),  # JAX-RS resource
    (frozenset({"@Service"}), "service"),
    (frozenset({"@Repository"}), "repository"),
    (frozenset({"@Entity", "@Table", "@MappedSuperclass", "@Embeddable"}), "entity"),
    (frozenset({"@Configuration", "@ConfigurationProperties"}), "config"),
    (frozenset({"@Component", "@Provider"}), "component"),
]

# Lombok / explicit constructor annotations that wire final fields as deps.
_CONSTRUCTOR_DI_ANNOTATIONS = frozenset({"@RequiredArgsConstructor", "@AllArgsConstructor"})
_FIELD_DI_ANNOTATIONS = frozenset({"@Autowired", "@Inject", "@Resource"})

_DTO_SUFFIXES = ("Dto", "DTO", "Request", "Response", "Data", "Payload", "Form")


def classify_role(annotation_names: set[str], simple_name: str, kind: str = "class") -> str:
    """Return the Spring-stack role for a class given its annotations + name."""
    for ann_set, role in _ROLE_BY_ANNOTATION:
        if annotation_names & ann_set:
            return role

    # naming-based heuristics when no stereotype annotation is present
    if simple_name.endswith(_DTO_SUFFIXES):
        return "dto"
    if simple_name.endswith(("Util", "Utils", "Helper", "Constants")):
        return "util"
    if kind in ("interface", "enum", "annotation", "record"):
        # repositories are very often plain interfaces extending JpaRepository,
        # but without @Repository we can't be sure -> leave as unknown here.
        return "unknown"
    return "unknown"


def class_uses_constructor_di(annotation_names: set[str]) -> bool:
    """True if the class wires its final fields via a generated/declared constructor."""
    return bool(annotation_names & _CONSTRUCTOR_DI_ANNOTATIONS)


def field_is_injected(
    field_annotation_names: set[str],
    *,
    is_final: bool,
    is_static: bool,
    class_uses_ctor_di: bool,
    ctor_assigned: bool = False,
) -> bool:
    """Decide whether a field is a dependency injection point.

    ``ctor_assigned`` covers hand-written constructor injection (``this.x = x``)
    without Lombok / field annotations.
    """
    if is_static:
        return False
    if field_annotation_names & _FIELD_DI_ANNOTATIONS:
        return True
    if class_uses_ctor_di and is_final:
        return True
    if ctor_assigned:
        return True
    return False
