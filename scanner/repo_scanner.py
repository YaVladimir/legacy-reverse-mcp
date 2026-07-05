"""Repository scanner: walks a repo, finds Maven/Gradle modules and counts Java sources."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
IGNORED_DIRS = {".git", "target", "out", "node_modules", ".idea", ".reverse"}
# "build" is a build-tool output directory (classes, tmp, reports, libs, ...) and is
# ignored like the others, EXCEPT for build/generated: codegen plugins (e.g. the
# openapi-generator gradle plugin) write real, annotated Java sources there that don't
# exist anywhere else in the repo until the project is built.
_BUILD_DIR = "build"
_BUILD_ALLOWED_CHILD = "generated"


def _is_ignored_path(path: Path) -> bool:
    parts = path.parts
    for i, part in enumerate(parts):
        if part in IGNORED_DIRS:
            return True
        if part == _BUILD_DIR:
            nxt = parts[i + 1] if i + 1 < len(parts) else None
            if nxt != _BUILD_ALLOWED_CHILD:
                return True
    return False


def prune_dirnames(dirpath: Path, dirnames: list[str]) -> None:
    """In-place ``os.walk`` dirnames filter: drop ignored dirs, and inside a ``build``
    dir keep only ``generated`` (so codegen output is visible, other build output isn't)."""
    if dirpath.name == _BUILD_DIR:
        dirnames[:] = [d for d in dirnames if d == _BUILD_ALLOWED_CHILD]
    else:
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]

POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


@dataclass
class ModuleInfo:
    name: str
    path: str
    build_file: str | None = None
    build_tool: str = "unknown"
    group_id: str | None = None
    artifact_id: str | None = None
    version: str | None = None
    packaging: str | None = None
    java_file_count: int = 0


@dataclass
class ScanResult:
    repo_path: str
    build_tool: str = "unknown"
    modules: list[ModuleInfo] = field(default_factory=list)
    total_files: int = 0
    total_java_files: int = 0


def _parse_pom(pom_path: Path) -> dict:
    info: dict = {}
    try:
        tree = ET.parse(pom_path)
        root = tree.getroot()

        def text(tag: str) -> str | None:
            el = root.find(f"m:{tag}", POM_NS)
            if el is None:
                el = root.find(tag)
            return el.text.strip() if el is not None and el.text else None

        info["artifact_id"] = text("artifactId")
        info["group_id"] = text("groupId")
        info["version"] = text("version")
        info["packaging"] = text("packaging") or "jar"
    except ET.ParseError:
        pass
    return info


def _parse_gradle_name(build_file: Path) -> dict:
    info: dict = {"packaging": "jar"}
    try:
        content = build_file.read_text(encoding="utf-8", errors="ignore")
        depth = 0
        for raw_line in content.splitlines():
            line = raw_line.strip()
            # only consider top-level (depth 0) assignments, skip nested blocks
            # such as publishing { ... version "..." ... }
            if depth == 0:
                if line.startswith("group ") or line.startswith("group="):
                    info["group_id"] = line.split("=", 1)[-1].strip().strip("'\"")
                elif line.startswith("version ") or line.startswith("version="):
                    info["version"] = line.split("=", 1)[-1].strip().strip("'\"")
            depth += line.count("{") - line.count("}")
    except OSError:
        pass
    return info


def _count_java_files(module_dir: Path) -> int:
    count = 0
    for path in module_dir.rglob("*.java"):
        if _is_ignored_path(path):
            continue
        count += 1
    return count


def scan_repo(repo_path: str) -> ScanResult:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError(f"repo path does not exist or is not a directory: {root}")

    result = ScanResult(repo_path=str(root))
    modules: list[ModuleInfo] = []
    total_files = 0

    for dirpath in [root, *_walk_dirs(root)]:
        if dirpath != root and _BUILD_DIR in dirpath.relative_to(root).parts:
            # codegen scaffolding under build/generated/** (e.g. openapi-generator writing
            # a throwaway pom.xml per spec) is a source of Java files, not a real module.
            continue
        build_file = next((dirpath / bf for bf in BUILD_FILES if (dirpath / bf).is_file()), None)
        if build_file is None:
            continue

        build_tool = "maven" if build_file.name == "pom.xml" else "gradle"
        info: dict = {}
        if build_tool == "maven":
            info = _parse_pom(build_file)
        else:
            info = _parse_gradle_name(build_file)

        module_name = info.get("artifact_id") or dirpath.name
        module = ModuleInfo(
            name=module_name,
            path=str(dirpath.relative_to(root)) if dirpath != root else ".",
            build_file=build_file.name,
            build_tool=build_tool,
            group_id=info.get("group_id"),
            artifact_id=info.get("artifact_id"),
            version=info.get("version"),
            packaging=info.get("packaging"),
            java_file_count=_count_java_files(dirpath),
        )
        modules.append(module)

    for path in root.rglob("*"):
        if path.is_file() and not _is_ignored_path(path):
            total_files += 1

    if modules:
        result.build_tool = modules[0].build_tool if len(modules) == 1 else _majority_build_tool(modules)
    result.modules = modules
    result.total_files = total_files
    result.total_java_files = sum(m.java_file_count for m in modules) if modules else _count_java_files(root)

    return result


def _walk_dirs(root: Path):
    for dirpath, dirnames, _ in __import__("os").walk(root):
        p = Path(dirpath)
        prune_dirnames(p, dirnames)
        if p == root:
            continue
        yield p


def _majority_build_tool(modules: list[ModuleInfo]) -> str:
    counts: dict[str, int] = {}
    for m in modules:
        counts[m.build_tool] = counts.get(m.build_tool, 0) + 1
    return max(counts, key=counts.get)
