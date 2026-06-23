"""Single scan pipeline shared by the CLI and the MCP server, so the two never drift.

Stages (in order): module detection -> java index -> class-dependency edges ->
build/external dependency graph -> deterministic summaries -> FTS search index ->
findings -> scan manifest.
"""

from __future__ import annotations

from index import repository as repo
from index.search import build_search_index
from scanner.dependency_scanner import index_dependencies
from scanner.java_indexer import index_class_dependencies, index_repo
from scanner.repo_scanner import scan_repo
from summarizer.class_summary import generate_class_summaries
from summarizer.package_summary import generate_package_summaries


def _noop(_msg: str) -> None:
    pass


def build_index(conn, repo_path: str, progress=None, progress_every: int = 0) -> dict:
    """Run the full indexing pipeline on an initialised connection. Returns a stats dict."""
    echo = progress or _noop

    result = scan_repo(repo_path)
    for m in result.modules:
        repo.insert_module(
            conn, name=m.name, path=m.path, build_file=m.build_file,
            group_id=m.group_id, artifact_id=m.artifact_id, version=m.version,
            packaging=m.packaging,
        )
    echo(f"Found {len(result.modules)} module(s), {result.total_java_files} .java file(s).")

    echo("Parsing Java sources ...")
    stats = index_repo(conn, repo_path, progress_every=progress_every)
    echo(
        f"Indexed {stats.classes} class(es), {stats.methods} method(s), "
        f"{stats.fields} field(s), {stats.endpoints} endpoint(s) from {stats.files_parsed} file(s)."
    )
    if stats.files_failed:
        echo(f"  ! {stats.files_failed} file(s) failed to parse.")

    echo("Linking class dependencies ...")
    class_edges = index_class_dependencies(conn)
    echo(f"Linked {class_edges} class-to-class edge(s).")

    echo("Scanning dependencies ...")
    dep_stats = index_dependencies(conn, repo_path)
    echo(
        f"Found {dep_stats.module_edges} inter-module edge(s), "
        f"{dep_stats.external_deps} external dependency declaration(s)."
    )

    echo("Generating summaries ...")
    class_summaries = generate_class_summaries(conn)
    package_summaries = generate_package_summaries(conn)
    echo(f"Summarized {class_summaries} class(es), {package_summaries} package(s).")

    echo("Building search index ...")
    search_rows = build_search_index(conn)
    echo(f"Indexed {search_rows} searchable entit(ies).")

    conn.execute(
        "INSERT INTO scan_manifest (repo_path, build_tool, total_files, total_classes, total_endpoints) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo_path, result.build_tool, result.total_files, stats.classes, stats.endpoints),
    )
    conn.commit()

    return {
        "build_tool": result.build_tool,
        "modules": len(result.modules),
        "classes": stats.classes,
        "methods": stats.methods,
        "fields": stats.fields,
        "endpoints": stats.endpoints,
        "class_edges": class_edges,
        "module_edges": dep_stats.module_edges,
        "external_deps": dep_stats.external_deps,
        "class_summaries": class_summaries,
        "package_summaries": package_summaries,
        "search_rows": search_rows,
        "parse_failures": stats.files_failed,
    }
