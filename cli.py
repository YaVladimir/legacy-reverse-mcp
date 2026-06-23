"""legacy-reverse CLI."""

from __future__ import annotations

from pathlib import Path

import click

from index.repository import init_db, insert_module
from scanner.java_indexer import index_repo
from scanner.repo_scanner import scan_repo


@click.group()
def cli() -> None:
    """legacy-reverse-mcp command-line interface."""


@cli.command()
@click.option("--repo", required=True, type=click.Path(exists=True, file_okay=False), help="Path to the repository to scan.")
@click.option("--force", is_flag=True, default=False, help="Rebuild the index even if it already exists.")
def scan(repo: str, force: bool) -> None:
    """Scan a repository and build/update the .reverse index."""
    repo_path = Path(repo).resolve()
    db_path = repo_path / ".reverse" / "index.sqlite3"

    if db_path.exists() and not force:
        click.echo(f"Index already exists at {db_path}, use --force to rebuild.")
        return

    if db_path.exists() and force:
        db_path.unlink()

    click.echo(f"Scanning {repo_path} ...")
    result = scan_repo(str(repo_path))

    conn = init_db(db_path)
    for module in result.modules:
        insert_module(
            conn,
            name=module.name,
            path=module.path,
            build_file=module.build_file,
            group_id=module.group_id,
            artifact_id=module.artifact_id,
            version=module.version,
            packaging=module.packaging,
        )
    click.echo(f"Found {len(result.modules)} module(s), {result.total_java_files} .java file(s).")

    click.echo("Parsing Java sources ...")
    stats = index_repo(conn, str(repo_path), progress_every=1000)
    click.echo(
        f"Indexed {stats.classes} class(es), {stats.methods} method(s), "
        f"{stats.fields} field(s), {stats.endpoints} endpoint(s) from "
        f"{stats.files_parsed} file(s)."
    )
    if stats.files_failed:
        click.echo(f"  ⚠ {stats.files_failed} file(s) failed to parse.")

    conn.execute(
        """
        INSERT INTO scan_manifest (repo_path, build_tool, total_files, total_classes, total_endpoints)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(repo_path), result.build_tool, result.total_files, stats.classes, stats.endpoints),
    )
    conn.commit()
    conn.close()

    click.echo(f"Index written to {db_path}")


if __name__ == "__main__":
    cli()
