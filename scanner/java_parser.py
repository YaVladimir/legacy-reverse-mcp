"""Java source parser built on tree-sitter-java.

Extracts classes/interfaces/enums/records with their methods, fields and
annotations. Framework-agnostic: it records raw annotation names so that
spring_scanner can classify Spring (@RestController, @Service) *and* JAX-RS
(@Path, @GET) the same way.

tree-sitter API note: pinned against tree-sitter 0.25.x / tree-sitter-java
0.23.x, where ``Parser(Language(...))`` takes the language in the constructor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_java

# Node types that introduce a top-level type and their schema `kind` value.
TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}

_VISIBILITY_KEYWORDS = ("public", "private", "protected")


@dataclass
class ParsedAnnotation:
    name: str                      # with leading '@', e.g. "@Path"
    attributes: str | None = None  # raw argument list, e.g. '("/v1/glaccounts")'
    line: int | None = None        # 1-based source line of the annotation


@dataclass
class ParsedParameter:
    position: int
    name: str | None
    type_fqn: str | None


@dataclass
class ParsedCall:
    """A method invocation seen syntactically inside a method body."""

    name: str                       # callee method name, e.g. "create"
    receiver: str | None = None     # last identifier of the receiver, e.g. "depositService"; None = implicit this
    line: int | None = None         # 1-based source line of the call


@dataclass
class ParsedField:
    name: str
    type_fqn: str | None
    visibility: str = "private"
    is_static: bool = False
    is_final: bool = False
    is_injected: bool = False
    line: int | None = None
    annotations: list[ParsedAnnotation] = field(default_factory=list)


@dataclass
class ParsedMethod:
    name: str
    signature: str
    return_type: str | None
    visibility: str = "public"
    is_static: bool = False
    is_constructor: bool = False
    line_start: int | None = None
    line_end: int | None = None
    annotations: list[ParsedAnnotation] = field(default_factory=list)
    parameters: list[ParsedParameter] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)


@dataclass
class ParsedClass:
    simple_name: str
    fqn: str
    kind: str
    package: str | None
    file_path: str
    package_line: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    is_abstract: bool = False
    visibility: str = "public"
    superclass_fqn: str | None = None
    interfaces: list[str] = field(default_factory=list)
    annotations: list[ParsedAnnotation] = field(default_factory=list)
    methods: list[ParsedMethod] = field(default_factory=list)
    fields: list[ParsedField] = field(default_factory=list)
    # fields assigned from a constructor parameter (this.x = x) — DI without Lombok
    ctor_injected_fields: set[str] = field(default_factory=set)


@dataclass
class ParsedFile:
    file_path: str
    package: str | None = None
    imports: list[str] = field(default_factory=list)
    classes: list[ParsedClass] = field(default_factory=list)


@lru_cache(maxsize=1)
def _get_parser() -> Parser:
    return Parser(Language(tree_sitter_java.language()))


def _text(node: Node | None) -> str | None:
    return node.text.decode("utf-8", errors="replace") if node is not None else None


def _modifiers_node(decl: Node) -> Node | None:
    return next((c for c in decl.children if c.type == "modifiers"), None)


def _collect_annotations(modifiers: Node | None) -> list[ParsedAnnotation]:
    if modifiers is None:
        return []
    out: list[ParsedAnnotation] = []
    for child in modifiers.children:
        if child.type == "marker_annotation":
            name = _text(child.child_by_field_name("name")) or _text(
                next((c for c in child.children if c.type == "identifier"), None)
            )
            if name:
                out.append(ParsedAnnotation(name=f"@{name}", line=child.start_point[0] + 1))
        elif child.type == "annotation":
            name = _text(child.child_by_field_name("name")) or _text(
                next((c for c in child.children if c.type == "identifier"), None)
            )
            args = next(
                (c for c in child.children if c.type == "annotation_argument_list"), None
            )
            if name:
                out.append(
                    ParsedAnnotation(
                        name=f"@{name}", attributes=_text(args), line=child.start_point[0] + 1
                    )
                )
    return out


def _visibility(modifiers: Node | None) -> str:
    if modifiers is None:
        return "package-private"
    keywords = {_text(c) for c in modifiers.children if c.type in _VISIBILITY_KEYWORDS}
    for kw in _VISIBILITY_KEYWORDS:
        if kw in keywords:
            return kw
    return "package-private"


def _has_modifier(modifiers: Node | None, keyword: str) -> bool:
    if modifiers is None:
        return False
    return any(c.type == keyword or _text(c) == keyword for c in modifiers.children)


def _injection_annotations() -> set[str]:
    return {"@Autowired", "@Inject", "@Resource"}


def _parse_field(decl: Node) -> list[ParsedField]:
    modifiers = _modifiers_node(decl)
    annotations = _collect_annotations(modifiers)
    type_fqn = _text(decl.child_by_field_name("type"))
    visibility = _visibility(modifiers)
    is_static = _has_modifier(modifiers, "static")
    is_final = _has_modifier(modifiers, "final")
    line = decl.start_point[0] + 1
    ann_names = {a.name for a in annotations}
    # baseline: explicit injection annotations. Constructor-injection (Lombok
    # @RequiredArgsConstructor over final fields) needs class context and is
    # upgraded later by spring_scanner.
    is_injected = bool(ann_names & _injection_annotations())

    fields: list[ParsedField] = []
    for declarator in (c for c in decl.children if c.type == "variable_declarator"):
        name = _text(declarator.child_by_field_name("name"))
        if name:
            fields.append(
                ParsedField(
                    name=name,
                    type_fqn=type_fqn,
                    visibility=visibility,
                    is_static=is_static,
                    is_final=is_final,
                    is_injected=is_injected,
                    line=line,
                    annotations=annotations,
                )
            )
    return fields


def _parse_parameters(method: Node) -> list[ParsedParameter]:
    params_node = next(
        (c for c in method.children if c.type == "formal_parameters"), None
    )
    if params_node is None:
        return []
    out: list[ParsedParameter] = []
    pos = 0
    for p in params_node.children:
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        out.append(
            ParsedParameter(
                position=pos,
                name=_text(p.child_by_field_name("name")),
                type_fqn=_text(p.child_by_field_name("type")),
            )
        )
        pos += 1
    return out


def _receiver_name(obj: Node | None) -> str | None:
    """Last identifier of a call receiver: ``depositService`` from ``this.depositService``."""
    if obj is None:
        return None
    if obj.type == "identifier":
        return _text(obj)
    if obj.type == "this":
        return "this"
    if obj.type == "field_access":
        fld = obj.child_by_field_name("field")
        return _text(fld) if fld is not None else None
    return None


def _collect_calls(decl: Node) -> list[ParsedCall]:
    """All method invocations inside a method body (syntactic; no type resolution)."""
    out: list[ParsedCall] = []
    stack = list(decl.children)
    while stack:
        node = stack.pop()
        if node.type == "method_invocation":
            name = _text(node.child_by_field_name("name"))
            if name:
                out.append(
                    ParsedCall(
                        name=name,
                        receiver=_receiver_name(node.child_by_field_name("object")),
                        line=node.start_point[0] + 1,
                    )
                )
        stack.extend(node.children)
    return out


def _assignment_target_field(left: Node | None) -> str | None:
    """Field name targeted by an assignment LHS: ``this.x`` or a bare ``x``."""
    if left is None:
        return None
    if left.type == "field_access":
        obj = left.child_by_field_name("object")
        if obj is not None and obj.type == "this":
            return _text(left.child_by_field_name("field"))
        return None
    if left.type == "identifier":
        return _text(left)
    return None


def _ctor_assigned_fields(ctor: Node) -> set[str]:
    """Fields assigned directly from a constructor parameter (``this.x = x``)."""
    params: set[str] = set()
    fp = next((c for c in ctor.children if c.type == "formal_parameters"), None)
    if fp is not None:
        for p in fp.children:
            if p.type in ("formal_parameter", "spread_parameter"):
                nm = _text(p.child_by_field_name("name"))
                if nm:
                    params.add(nm)
    if not params:
        return set()

    out: set[str] = set()
    stack = list(ctor.children)
    while stack:
        node = stack.pop()
        if node.type == "assignment_expression":
            right = node.child_by_field_name("right")
            if right is not None and right.type == "identifier" and _text(right) in params:
                fname = _assignment_target_field(node.child_by_field_name("left"))
                if fname:
                    out.add(fname)
        stack.extend(node.children)
    return out


def _collect_ctor_injected_fields(body: Node | None) -> set[str]:
    """Union of constructor-parameter-assigned fields across all constructors."""
    if body is None:
        return set()
    out: set[str] = set()
    for member in body.children:
        if member.type == "constructor_declaration":
            out |= _ctor_assigned_fields(member)
    return out


def _parse_method(decl: Node, class_name: str) -> ParsedMethod:
    modifiers = _modifiers_node(decl)
    is_constructor = decl.type == "constructor_declaration"
    name = _text(decl.child_by_field_name("name")) or class_name
    return_type = None if is_constructor else _text(decl.child_by_field_name("type"))
    params = _parse_parameters(decl)
    param_types = ", ".join(p.type_fqn or "?" for p in params)
    signature = f"{name}({param_types})"
    if return_type:
        signature += f": {return_type}"
    return ParsedMethod(
        name=name,
        signature=signature,
        return_type=return_type,
        visibility=_visibility(modifiers),
        is_static=_has_modifier(modifiers, "static"),
        is_constructor=is_constructor,
        line_start=decl.start_point[0] + 1,
        line_end=decl.end_point[0] + 1,
        annotations=_collect_annotations(modifiers),
        parameters=params,
        calls=_collect_calls(decl),
    )


def _superclass_fqn(decl: Node) -> str | None:
    sup = decl.child_by_field_name("superclass")
    if sup is not None:
        # superclass node is `superclass` -> wraps a type; strip leading "extends"
        text = _text(sup) or ""
        return text.replace("extends", "").strip() or None
    return None


def _interfaces(decl: Node) -> list[str]:
    out: list[str] = []
    for child in decl.children:
        if child.type in ("super_interfaces", "extends_interfaces"):
            type_list = next(
                (c for c in child.children if c.type == "type_list"), None
            )
            if type_list is not None:
                out.extend(
                    _text(t) for t in type_list.children if t.is_named and _text(t)
                )
    return out


def _class_body(decl: Node) -> Node | None:
    for child in decl.children:
        if child.type in ("class_body", "interface_body", "enum_body", "annotation_type_body"):
            return child
    return None


def _parse_type_declaration(
    decl: Node, package: str | None, file_path: str, package_line: int | None = None
) -> ParsedClass:
    modifiers = _modifiers_node(decl)
    simple_name = _text(decl.child_by_field_name("name")) or "<anonymous>"
    fqn = f"{package}.{simple_name}" if package else simple_name

    parsed = ParsedClass(
        simple_name=simple_name,
        fqn=fqn,
        kind=TYPE_DECLARATIONS.get(decl.type, "class"),
        package=package,
        file_path=file_path,
        package_line=package_line,
        line_start=decl.start_point[0] + 1,
        line_end=decl.end_point[0] + 1,
        is_abstract=_has_modifier(modifiers, "abstract"),
        visibility=_visibility(modifiers),
        superclass_fqn=_superclass_fqn(decl),
        interfaces=_interfaces(decl),
        annotations=_collect_annotations(modifiers),
    )

    body = _class_body(decl)
    if body is not None:
        for member in body.children:
            if member.type == "field_declaration":
                parsed.fields.extend(_parse_field(member))
            elif member.type in ("method_declaration", "constructor_declaration"):
                parsed.methods.append(_parse_method(member, simple_name))
        parsed.ctor_injected_fields = _collect_ctor_injected_fields(body)
    return parsed


def parse_source(source: bytes, file_path: str) -> ParsedFile:
    tree = _get_parser().parse(source)
    root = tree.root_node

    package = None
    package_line: int | None = None
    imports: list[str] = []
    for node in root.children:
        if node.type == "package_declaration":
            # package_declaration -> "package" <scoped_identifier> ";"
            ident = next((c for c in node.children if c.is_named), None)
            package = _text(ident)
            package_line = node.start_point[0] + 1
        elif node.type == "import_declaration":
            ident = next((c for c in node.children if c.is_named), None)
            if ident is not None and _text(ident):
                imports.append(_text(ident))

    result = ParsedFile(file_path=file_path, package=package, imports=imports)
    # type declarations can be nested under the program node directly
    for node in root.children:
        if node.type in TYPE_DECLARATIONS:
            result.classes.append(
                _parse_type_declaration(node, package, file_path, package_line)
            )
    return result


def parse_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    source = path.read_bytes()
    return parse_source(source, str(path))
