"""Static dependency extraction for Gradle and Maven modules.

Default path parses build files textually (no build execution): module->module
edges from ``project(':x')`` refs and external ``group:artifact[:version]``
coordinates. An optional ``resolve_versions_gradle`` runs ``gradle dependencies``
to fill in versions that are managed centrally (BOM) and thus absent statically.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from index import repository as repo
from scanner.java_indexer import _normalize
from scanner.repo_scanner import POM_NS

# Gradle configuration -> schema scope
_SCOPE_BY_CONFIG = {
    "implementation": "compile",
    "api": "compile",
    "compile": "compile",
    "testImplementation": "test",
    "testRuntimeOnly": "test",
    "testCompileOnly": "test",
    "testCompile": "test",
    "runtimeOnly": "runtime",
    "runtime": "runtime",
    "providedRuntime": "runtime",
    "compileOnly": "provided",
    "providedCompile": "provided",
    "annotationProcessor": "provided",
    "testAnnotationProcessor": "provided",
}
# longest-first so "testImplementation" wins over "implementation"
_CONFIGS_RE = re.compile(
    r"^(" + "|".join(sorted(_SCOPE_BY_CONFIG, key=len, reverse=True)) + r")\b"
)

_PROJECT_REF = re.compile(r"""project\s*\(\s*(?:path\s*:\s*)?['"](:?[\w:\-]+)['"]""")
_GAV = re.compile(r"""['"]([\w.\-]+:[\w.\-]+(?::[\w.\-]+)?)['"]""")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# gradle `gradle dependencies` tree lines, e.g. "+--- g:a:1.2" or "\--- g:a -> 1.2"
_TREE_GAV = re.compile(
    r"[\\+|`\- ]+([\w.\-]+):([\w.\-]+)(?::([\w.\-]+))?(?:\s*->\s*([\w.\-]+))?"
)


@dataclass
class ExternalDep:
    group_id: str
    artifact_id: str
    version: str | None
    scope: str


@dataclass
class ModuleDeps:
    module_refs: list[tuple[str, str]] = dc_field(default_factory=list)  # (gradle_path, scope)
    external: list[ExternalDep] = dc_field(default_factory=list)


@dataclass
class DepStats:
    module_edges: int = 0
    external_deps: int = 0
    unresolved_refs: list[str] = dc_field(default_factory=list)


# ------------------------------------------------------------
# text helpers
# ------------------------------------------------------------

def _strip_comments(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _balanced_blocks(text: str, opener: str) -> list[str]:
    """Return the brace-balanced bodies of every ``<opener> { ... }`` in text."""
    bodies: list[str] = []
    for m in re.finditer(rf"{opener}\s*\{{", text):
        depth = 0
        start = m.end() - 1  # at the '{'
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(text[start + 1 : i])
                    break
    return bodies


def _remove_blocks(text: str, opener: str) -> str:
    """Remove every ``<opener> { ... }`` block (brace-balanced) from text."""
    out = text
    while True:
        m = re.search(rf"{opener}\s*\{{", out)
        if not m:
            return out
        depth = 0
        start = m.start()
        brace = m.end() - 1
        for i in range(brace, len(out)):
            if out[i] == "{":
                depth += 1
            elif out[i] == "}":
                depth -= 1
                if depth == 0:
                    out = out[:start] + out[i + 1 :]
                    break
        else:
            return out  # unbalanced; bail


def _statements(block_body: str):
    """Yield (config, span_text) for each dependency statement in a block body."""
    lines = block_body.splitlines()
    current_config: str | None = None
    buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        m = _CONFIGS_RE.match(stripped)
        if m:
            if current_config is not None:
                yield current_config, "\n".join(buf)
            current_config = m.group(1)
            buf = [stripped]
        elif current_config is not None:
            buf.append(stripped)
    if current_config is not None:
        yield current_config, "\n".join(buf)


# ------------------------------------------------------------
# gradle
# ------------------------------------------------------------

def parse_gradle_module(module_dir: Path) -> ModuleDeps:
    deps = ModuleDeps()
    seen_ext: set[tuple] = set()
    seen_ref: set[tuple] = set()

    texts: list[str] = []
    build = module_dir / "build.gradle"
    if build.is_file():
        # drop buildscript { } (plugin deps, not app deps) before scanning
        texts.append(_remove_blocks(_strip_comments(build.read_text(encoding="utf-8", errors="ignore")), "buildscript"))
    extra = module_dir / "dependencies.gradle"
    if extra.is_file():
        texts.append(_strip_comments(extra.read_text(encoding="utf-8", errors="ignore")))

    for text in texts:
        for body in _balanced_blocks(text, "dependencies"):
            for config, span in _statements(body):
                scope = _SCOPE_BY_CONFIG.get(config, "compile")
                for gpath in _PROJECT_REF.findall(span):
                    key = (gpath, scope)
                    if key not in seen_ref:
                        seen_ref.add(key)
                        deps.module_refs.append((gpath, scope))
                # GAV strings, skipping any that are actually project() refs
                span_wo_proj = _PROJECT_REF.sub("", span)
                for gav in _GAV.findall(span_wo_proj):
                    parts = gav.split(":")
                    group_id, artifact_id = parts[0], parts[1]
                    version = parts[2] if len(parts) > 2 else None
                    key = (group_id, artifact_id, scope)
                    if key not in seen_ext:
                        seen_ext.add(key)
                        deps.external.append(ExternalDep(group_id, artifact_id, version, scope))
    return deps


# ------------------------------------------------------------
# maven
# ------------------------------------------------------------

def parse_maven_module(pom_path: Path) -> ModuleDeps:
    deps = ModuleDeps()
    try:
        root = ET.parse(pom_path).getroot()
    except ET.ParseError:
        return deps

    def _find(el, tag):
        child = el.find(f"m:{tag}", POM_NS)
        if child is None:
            child = el.find(tag)
        return child.text.strip() if child is not None and child.text else None

    # ONLY the project-level <dependencies> block: root.iter() would also sweep
    # <dependencyManagement> (managed versions, not actual deps), build-plugin
    # dependencies and profile blocks — a parent pom with 80 managed artifacts
    # would show 80 phantom external dependencies.
    deps_el = root.find("m:dependencies", POM_NS)
    if deps_el is None:
        deps_el = root.find("dependencies")
    if deps_el is None:
        return deps
    for dep in list(deps_el):
        if not dep.tag.endswith("dependency"):
            continue
        group_id = _find(dep, "groupId")
        artifact_id = _find(dep, "artifactId")
        if not group_id or not artifact_id:
            continue
        version = _find(dep, "version")
        scope = _find(dep, "scope") or "compile"
        deps.external.append(ExternalDep(group_id, artifact_id, version, scope))
    return deps


def _maven_artifact_id(pom_path: Path) -> str | None:
    """The pom's OWN artifactId (not a dependency's)."""
    try:
        root = ET.parse(pom_path).getroot()
    except (ET.ParseError, OSError):
        return None
    el = root.find("m:artifactId", POM_NS)
    if el is None:
        el = root.find("artifactId")
    return el.text.strip() if el is not None and el.text else None


# ------------------------------------------------------------
# orchestration
# ------------------------------------------------------------

def _gradle_path_to_rel(gradle_path: str) -> str:
    return _normalize(gradle_path.lstrip(":").replace(":", "/"))


def index_dependencies(conn, repo_path: str) -> DepStats:
    repo_root = Path(repo_path).resolve()
    repo.clear_dependencies(conn, commit=False)
    stats = DepStats()

    modules = repo.list_modules(conn)
    by_path = {_normalize(m["path"]): m["id"] for m in modules}
    by_name = {m["name"]: m["id"] for m in modules}

    def _module_dir(m):
        return repo_root if _normalize(m["path"]) in ("", ".") else repo_root / m["path"]

    # Maven has no project(':x') syntax — an inter-module dependency looks like an
    # ordinary GAV. Map every maven module's own artifactId up front so sibling
    # references become module edges instead of phantom external artifacts
    # (without this, module_dependency stays empty on pure-Maven repos and the
    # circular_dependency finding can never fire).
    artifact_to_module: dict[str, int] = {}
    for m in modules:
        if (m["build_file"] or "").lower() == "pom.xml":
            aid = _maven_artifact_id(_module_dir(m) / "pom.xml")
            if aid:
                artifact_to_module.setdefault(aid, m["id"])

    for m in modules:
        module_id = m["id"]
        module_dir = _module_dir(m)
        build_file = (m["build_file"] or "").lower()

        if build_file == "pom.xml":
            deps = parse_maven_module(module_dir / "pom.xml")
        else:
            deps = parse_gradle_module(module_dir)

        for gpath, scope in deps.module_refs:
            rel = _gradle_path_to_rel(gpath)
            to_id = by_path.get(rel) or by_name.get(rel.rsplit("/", 1)[-1])
            if to_id and to_id != module_id:
                repo.insert_module_dependency(conn, module_id, to_id, scope, commit=False)
                stats.module_edges += 1
            elif not to_id:
                stats.unresolved_refs.append(f"{m['name']} -> {gpath}")

        for ext in deps.external:
            to_id = (
                artifact_to_module.get(ext.artifact_id) if build_file == "pom.xml" else None
            )
            if to_id and to_id != module_id:
                repo.insert_module_dependency(conn, module_id, to_id, ext.scope, commit=False)
                stats.module_edges += 1
                continue
            repo.insert_external_dependency(
                conn, module_id, ext.group_id, ext.artifact_id, ext.version, ext.scope, commit=False
            )
            stats.external_deps += 1

        conn.commit()

    return stats


# ------------------------------------------------------------
# optional: resolve versions by running gradle
# ------------------------------------------------------------

def resolve_versions_gradle(conn, repo_path: str, progress=None) -> dict:
    """Best-effort: run `gradle <module>:dependencies` to fill missing versions."""
    repo_root = Path(repo_path).resolve()
    gradlew = repo_root / ("gradlew.bat" if (repo_root / "gradlew.bat").exists() else "gradlew")
    if not gradlew.exists():
        return {"status": "skipped", "reason": "no gradle wrapper found"}

    updated = 0
    failures: list[str] = []
    for m in repo.list_modules(conn):
        gradle_path = ":" + _normalize(m["path"]).replace("/", ":") if _normalize(m["path"]) not in ("", ".") else ""
        task = f"{gradle_path}:dependencies" if gradle_path else "dependencies"
        try:
            proc = subprocess.run(
                [str(gradlew), task, "--configuration", "runtimeClasspath", "-q"],
                cwd=repo_root, capture_output=True, text=True, timeout=300,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            failures.append(f"{m['name']}: {type(exc).__name__}")
            continue
        for line in proc.stdout.splitlines():
            mt = _TREE_GAV.search(line)
            if not mt:
                continue
            group_id, artifact_id, v1, v2 = mt.groups()
            version = v2 or v1
            if not version:
                continue
            cur = conn.execute(
                "UPDATE external_dependency SET version = ? "
                "WHERE module_id = ? AND group_id = ? AND artifact_id = ? AND version IS NULL",
                (version, m["id"], group_id, artifact_id),
            )
            updated += cur.rowcount
        conn.commit()
        if progress:
            progress(m["name"])

    return {"status": "done", "versions_updated": updated, "failures": failures}
