"""legacy-reverse CLI."""

from __future__ import annotations

from pathlib import Path

import click

from analysis.report import write_baseline
from index.repository import get_conn, init_db
from scanner.dependency_scanner import resolve_versions_gradle
from scanner.pipeline import build_index

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


def _write_report(conn, repo_path: Path) -> None:
    out = write_baseline(conn, repo_path)
    click.echo(f"Baseline report written to:\n  {out['markdown_path']}\n  {out['json_path']}")


if __name__ == "__main__":
    cli()
