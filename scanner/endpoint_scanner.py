"""REST endpoint extraction for JAX-RS and Spring MVC.

Pure functions: given a class's annotations (for the base path) and a method's
annotations, produce zero or more endpoints. Matches *exact* annotation names
-- e.g. it must never treat MapStruct's @Mapping as a Spring @*Mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# JAX-RS verb annotations carry no path of their own.
_JAXRS_VERBS = {"@GET", "@POST", "@PUT", "@DELETE", "@HEAD", "@OPTIONS", "@PATCH"}

# Spring shortcut mappings -> HTTP verb. (@PatchMapping distinct from JAX-RS @PATCH.)
_SPRING_VERB_MAPPINGS = {
    "@GetMapping": "GET",
    "@PostMapping": "POST",
    "@PutMapping": "PUT",
    "@DeleteMapping": "DELETE",
    "@PatchMapping": "PATCH",
}

_PATH_ANNOTATION = "@Path"
_REQUEST_MAPPING = "@RequestMapping"
_PRODUCES = {"@Produces"}
_CONSUMES = {"@Consumes"}

_STRING_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"')
_REQUEST_METHOD = re.compile(r"RequestMethod\.(\w+)")


@dataclass
class ExtractedEndpoint:
    http_method: str
    sub_path: str | None
    produces: str | None = None
    consumes: str | None = None


def _first_string(attrs: str | None) -> str | None:
    if not attrs:
        return None
    m = _STRING_LITERAL.search(attrs)
    return m.group(1) if m else None


def _attr_string(attrs: str | None, key: str) -> str | None:
    """Extract the string literal assigned to ``key =`` in an attribute list."""
    if not attrs:
        return None
    m = re.search(rf"{key}\s*=\s*\"((?:[^\"\\]|\\.)*)\"", attrs)
    return m.group(1) if m else None


def _path_value(attrs: str | None) -> str | None:
    """Path from value=/path= attribute, else the first positional string."""
    return _attr_string(attrs, "value") or _attr_string(attrs, "path") or _first_string(attrs)


def join_paths(base: str | None, sub: str | None) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def class_base_path(class_annotations: list[tuple[str, str | None]]) -> str | None:
    """base path from a class-level @Path (JAX-RS) or @RequestMapping (Spring)."""
    for name, attrs in class_annotations:
        if name == _PATH_ANNOTATION:
            return _path_value(attrs)
    for name, attrs in class_annotations:
        if name == _REQUEST_MAPPING:
            return _path_value(attrs)
    return None


def _media_type(annotations: list[tuple[str, str | None]], wanted: set[str]) -> str | None:
    for name, attrs in annotations:
        if name in wanted:
            # keep the raw token list, trimmed of the surrounding parens
            if attrs:
                return attrs.strip().lstrip("(").rstrip(")").strip() or None
    return None


def extract_endpoints(
    method_annotations: list[tuple[str, str | None]],
) -> list[ExtractedEndpoint]:
    """Return endpoints declared on a single method (usually 0 or 1)."""
    names = [a[0] for a in method_annotations]
    attrs_by_name = {name: attrs for name, attrs in method_annotations}

    produces = _media_type(method_annotations, _PRODUCES)
    consumes = _media_type(method_annotations, _CONSUMES)
    method_path = _path_value(attrs_by_name.get(_PATH_ANNOTATION))

    endpoints: list[ExtractedEndpoint] = []

    # JAX-RS: verb annotation + separate @Path
    for verb in _JAXRS_VERBS:
        if verb in names:
            endpoints.append(
                ExtractedEndpoint(verb.lstrip("@"), method_path, produces, consumes)
            )

    # Spring shortcut mappings: path lives inside the annotation itself
    for ann, http in _SPRING_VERB_MAPPINGS.items():
        if ann in names:
            sub = _path_value(attrs_by_name.get(ann))
            endpoints.append(ExtractedEndpoint(http, sub, produces, consumes))

    # Spring @RequestMapping with explicit method=
    if _REQUEST_MAPPING in names:
        attrs = attrs_by_name.get(_REQUEST_MAPPING)
        sub = _path_value(attrs)
        verbs = _REQUEST_METHOD.findall(attrs or "")
        if verbs:
            for v in verbs:
                endpoints.append(ExtractedEndpoint(v.upper(), sub, produces, consumes))
        else:
            # no method attribute -> maps to all verbs; record as ANY
            endpoints.append(ExtractedEndpoint("ANY", sub, produces, consumes))

    return endpoints
