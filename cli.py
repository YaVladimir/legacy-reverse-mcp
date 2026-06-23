"""legacy-reverse CLI."""

from __future__ import annotations

from pathlib import Path

import click

from index.repository import init_db
from scanner.dependency_scanner import resolve_versions_gradle
from scanner.pipeline import build_index


@click.group()
def cli() -> None:
    """legacy-reverse-mcp command-line interface."""


@cli.command()
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to the repository to scan.")
@click.option("--force", is_flag=True, default=False, help="Rebuild the index even if it already exists.")
@click.option("--resolve", is_flag=True, default=False, help="Run gradle to resolve exact dependency versions (slow).")
def scan(repo: str, force: bool, resolve: bool) -> None:
    """Scan a repository and build/update the .reverse index."""
    repo_path = Path(repo).resolve()
    db_path = repo_path / ".reverse" / "index.sqlite3"

    if db_path.exists() and not force:
        click.echo(f"Index already exists at {db_path}, use --force to rebuild.")
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

    conn.close()
    click.echo(f"Index written to {db_path}")


if __name__ == "__main__":
    cli()
