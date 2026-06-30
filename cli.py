"""legacy-reverse CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from analysis.flat_arch import export_flat, import_flat
from analysis.report import write_baseline
from index.repository import get_conn, init_db
from scanner.dependency_scanner import resolve_versions_gradle
from scanner.pipeline import build_index
from summarizer.describe import describe_repo
from summarizer.harness import generate_architecture

_DB_RELATIVE = Path(".reverse") / "index.sqlite3"


@click.group()
def cli() -> None:
    """legacy-reverse-mcp command-line interface."""


@cli.command()
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to the repository to scan.")
@click.option("--force", is_flag=True, default=False, help="Rebuild the index even if it already exists.")
@click.option("--resolve", is_flag=True, default=False, help="Run gradle to resolve exact dependency versions (slow).")
@click.option("--report", is_flag=True, default=False, help="Also write a baseline report under .reverse/reports/.")
def scan(repo: str, force: bool, resolve: bool, report: bool) -> None:
    """Scan a repository and build/update the .reverse index."""
    repo_path = Path(repo).resolve()
    db_path = repo_path / _DB_RELATIVE

    if db_path.exists() and not force:
        click.echo(f"Index already exists at {db_path}, use --force to rebuild.")
        if report:
            _write_report(get_conn(db_path), repo_path)
        return

    if db_path.exists() and force:
        db_path.unlink()

    click.echo(f"Scanning {repo_path} ...")
    conn = init_db(db_path)
    build_index(conn, str(repo_path), progress=click.echo, progress_every=1000)

    if resolve:
        click.echo("Resolving versions via gradle (this can take a while) ...")
        res = resolve_versions_gradle(conn, str(repo_path), progress=lambda n: None)
        if res["status"] == "done":
            click.echo(f"  versions updated: {res['versions_updated']}; failures: {len(res['failures'])}")
        else:
            click.echo(f"  resolve skipped: {res.get('reason')}")

    if report:
        _write_report(conn, repo_path)

    conn.close()
    click.echo(f"Index written to {db_path}")


@cli.command()
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to a repository that has already been scanned.")
@click.option("--force", is_flag=True, default=False, help="Ignore the description cache and regenerate everything.")
@click.option("--no-llm", is_flag=True, default=False, help="Skip the LLM; write deterministic fallback descriptions only.")
def describe(repo: str, force: bool, no_llm: bool) -> None:
    """Generate meaningful class/method/hierarchy descriptions over an existing index.

    Reads LLM settings from the LEGACY_REVERSE_LLM_* environment variables. With no
    LEGACY_REVERSE_LLM_BASE_URL (or --no-llm) it falls back to deterministic text.
    """
    repo_path = Path(repo).resolve()
    db_path = repo_path / _DB_RELATIVE
    if not db_path.exists():
        raise click.ClickException(f"No index at {db_path}. Run `legacy-reverse scan --repo {repo}` first.")
    conn = get_conn(db_path)
    try:
        stats = describe_repo(conn, str(repo_path), force=force, use_llm=not no_llm, progress=click.echo)
    finally:
        conn.close()
    click.echo(
        f"Done: {stats['classes']} classes, {stats['methods']} methods described "
        f"(llm={stats['from_llm']}, cache={stats['from_cache']}, fallback={stats['from_fallback']})."
    )


@cli.command()
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to a repository that has already been scanned.")
def report(repo: str) -> None:
    """Generate a baseline report from an existing .reverse index."""
    repo_path = Path(repo).resolve()
    db_path = repo_path / _DB_RELATIVE
    if not db_path.exists():
        raise click.ClickException(f"No index at {db_path}. Run `legacy-reverse scan --repo {repo}` first.")
    conn = get_conn(db_path)
    try:
        _write_report(conn, repo_path)
    finally:
        conn.close()


def _require_index(repo: str) -> tuple[Path, Path]:
    repo_path = Path(repo).resolve()
    db_path = repo_path / _DB_RELATIVE
    if not db_path.exists():
        raise click.ClickException(f"No index at {db_path}. Run `legacy-reverse scan --repo {repo}` first.")
    return repo_path, db_path


@cli.command(name="export-arch")
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to a scanned repository.")
@click.option("--out", required=True, type=click.Path(dir_okay=False), help="Output JSON file (flat architecture).")
def export_arch(repo: str, out: str) -> None:
    """Export the index as a flat architecture JSON (reference schema)."""
    repo_path, db_path = _require_index(repo)
    conn = get_conn(db_path)
    try:
        data = export_flat(conn, str(repo_path))
    finally:
        conn.close()
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(f"Wrote {data['total_classes']} class(es) to {out}")


@cli.command(name="import-arch")
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to a scanned repository.")
@click.option("--in", "in_path", required=True, type=click.Path(exists=True, dir_okay=False), help="Flat architecture JSON to import.")
def import_arch(repo: str, in_path: str) -> None:
    """Import descriptions from a flat architecture JSON into the index."""
    repo_path, db_path = _require_index(repo)
    try:
        data = json.loads(Path(in_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Could not read JSON {in_path}: {exc}")
    conn = get_conn(db_path)
    try:
        stats = import_flat(conn, str(repo_path), data)
    finally:
        conn.close()
    click.echo(
        f"Imported {stats['classes_matched']}/{stats['classes_total']} class(es), "
        f"{stats['methods_matched']} method(s); unmatched methods: {stats['methods_unmatched']}, "
        f"unmatched classes: {len(stats['unmatched_classes'])}."
    )


@cli.command(name="generate-arch")
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to a scanned repository.")
def generate_arch(repo: str) -> None:
    """Run gigacode's architecture-generator skill and import its flat JSON.

    Configured via LEGACY_REVERSE_GIGACODE_* env vars. If gigacode is unavailable,
    run the skill manually and use `import-arch --in <file>`.
    """
    repo_path, db_path = _require_index(repo)
    conn = get_conn(db_path)
    try:
        stats = generate_architecture(conn, str(repo_path))
    finally:
        conn.close()
    if stats.get("status") == "error":
        raise click.ClickException(f"{stats.get('error')}. {stats.get('hint', '')}".strip())
    click.echo(
        f"Imported from gigacode: {stats['classes_matched']}/{stats['classes_total']} class(es), "
        f"{stats['methods_matched']} method(s)."
    )


def _write_report(conn, repo_path: Path) -> None:
    out = write_baseline(conn, repo_path)
    click.echo(f"Baseline report written to:\n  {out['markdown_path']}\n  {out['json_path']}")


if __name__ == "__main__":
    cli()
