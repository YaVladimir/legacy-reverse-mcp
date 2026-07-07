"""Single scan pipeline shared by the CLI and the MCP server, so the two never drift.

Stages (in order): module detection -> java index -> class-dependency edges ->
build/external dependency graph -> deterministic summaries -> FTS search index ->
findings -> scan manifest.
"""

from __future__ import annotations

from index import repository as repo
from index.findings import detect_findings
from index.search import build_search_index
from scanner.config_scanner import index_config
from scanner.dependency_scanner import index_dependencies
from scanner.java_indexer import (
    index_class_dependencies,
    index_repo,
    reattribute_interface_endpoints,
    resolve_same_package_types,
)
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
    echo(
        f"Recorded {stats.observed_facts} observed fact(s) with evidence, "
        f"{stats.method_calls} intra-class method call(s)."
    )
    if stats.files_failed:
        echo(f"  ! {stats.files_failed} file(s) failed to parse.")

    # resolve same-package types to FQNs before edges are derived from them, so a
    # sibling-class reference links precisely instead of over-approximating by name.
    same_pkg_resolved = resolve_same_package_types(conn)
    if same_pkg_resolved:
        echo(f"Resolved {same_pkg_resolved} same-package type reference(s) to FQN.")

    echo("Linking class dependencies ...")
    class_edges = index_class_dependencies(conn)
    echo(f"Linked {class_edges} class-to-class edge(s).")

    reattributed_endpoints = reattribute_interface_endpoints(conn)
    if reattributed_endpoints:
        echo(f"Reattributed {reattributed_endpoints} endpoint(s) from interface to implementing controller.")
    # reattribution can both create (per concrete controller) and delete (claimed
    # interface-level) endpoint rows, so the parse-time stats.endpoints count is
    # stale afterwards -- recompute for the manifest and the returned summary.
    total_endpoints = conn.execute("SELECT COUNT(*) FROM endpoint WHERE superseded = 0").fetchone()[0]

    echo("Scanning dependencies ...")
    dep_stats = index_dependencies(conn, repo_path)
    echo(
        f"Found {dep_stats.module_edges} inter-module edge(s), "
        f"{dep_stats.external_deps} external dependency declaration(s)."
    )

    echo("Indexing configuration ...")
    config_stats = index_config(conn, repo_path)
    echo(
        f"Indexed {config_stats.config_files} config file(s), "
        f"{config_stats.config_properties} propert(ies) across "
        f"{len(config_stats.profiles)} profile(s); {config_stats.secrets} secret-bearing."
    )

    echo("Generating summaries ...")
    class_summaries = generate_class_summaries(conn)
    package_summaries = generate_package_summaries(conn)
    echo(f"Summarized {class_summaries} class(es), {package_summaries} package(s).")

    # a rebuilt index loses previously applied describe/import output; the durable
    # store (descriptions.sqlite3) survives, so restore what is still fresh before
    # the FTS build picks summaries up. No LLM. Local import: keeps scanner free of
    # a summarizer-LLM dependency at module load.
    from summarizer.describe import reapply_imported

    restored = reapply_imported(conn, repo_path)
    if restored["classes"] or restored["methods"]:
        msg = (
            f"Restored {restored['classes']} imported class / {restored['methods']} "
            f"method description(s) from the durable store"
        )
        if restored["stale"]:
            msg += f"; {restored['stale']} stale import(s) skipped"
        echo(msg + ".")

    echo("Building search index ...")
    search_rows = build_search_index(conn)
    echo(f"Indexed {search_rows} searchable entit(ies).")

    echo("Detecting findings ...")
    findings = detect_findings(conn)
    echo(f"Findings: {findings or 'none'}.")

    # persist low-confidence inferred findings (layer guesses) so the baseline
    # report and future tools can read them. Local import: keeps scanner free of
    # an analysis dependency at module load.
    from analysis.layers import compute_low_confidence_findings

    repo.clear_inferred_findings(conn, commit=False)
    inferred = compute_low_confidence_findings(conn)
    for f in inferred:
        repo.insert_inferred_finding(conn, f, commit=False)
    conn.commit()
    echo(f"Persisted {len(inferred)} inferred finding(s).")

    conn.execute(
        "INSERT INTO scan_manifest (repo_path, build_tool, total_files, total_classes, total_endpoints) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo_path, result.build_tool, result.total_files, stats.classes, total_endpoints),
    )
    conn.commit()

    return {
        "build_tool": result.build_tool,
        "modules": len(result.modules),
        "classes": stats.classes,
        "methods": stats.methods,
        "fields": stats.fields,
        "endpoints": total_endpoints,
        "observed_facts": stats.observed_facts,
        "method_calls": stats.method_calls,
        "class_edges": class_edges,
        "reattributed_endpoints": reattributed_endpoints,
        "module_edges": dep_stats.module_edges,
        "external_deps": dep_stats.external_deps,
        "config_files": config_stats.config_files,
        "config_properties": config_stats.config_properties,
        "config_profiles": sorted(config_stats.profiles),
        "config_secrets": config_stats.secrets,
        "class_summaries": class_summaries,
        "package_summaries": package_summaries,
        "search_rows": search_rows,
        "findings": findings,
        "inferred_findings": len(inferred),
        "restored_descriptions": restored,
        "parse_failures": stats.files_failed,
    }
