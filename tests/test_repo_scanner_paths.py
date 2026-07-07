"""H1: ignore rules (build/, target/, out/, ...) must apply to paths *relative to
the repo root*, not to the absolute directories the repo happens to sit under. A
repo cloned into ``.../build/myrepo`` — or a repo whose own root is named ``build``
— must index normally instead of silently producing zero classes."""

from __future__ import annotations

from pathlib import Path

from index.repository import init_db
from scanner.pipeline import build_index
from scanner.repo_scanner import _is_ignored_path, scan_repo

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"
_FOO = "package ru.bank;\npublic class Foo {}\n"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_is_ignored_path_uses_repo_relative_parts(tmp_path):
    repo_root = tmp_path / "ci" / "build" / "myrepo"
    inside = repo_root / "src" / "main" / "java" / "ru" / "bank" / "Foo.java"
    # the 'build' above the repo root must not trigger the ignore rule
    assert _is_ignored_path(inside, repo_root) is False
    # build/generated inside the repo is still allowed; build/classes still ignored
    assert _is_ignored_path(repo_root / "build" / "generated" / "X.java", repo_root) is False
    assert _is_ignored_path(repo_root / "build" / "classes" / "X.java", repo_root) is True


def test_repo_cloned_under_build_dir_indexes(tmp_path):
    root = tmp_path / "ci" / "build" / "myrepo"
    _write(root, {"pom.xml": _POM, "src/main/java/ru/bank/Foo.java": _FOO})
    conn = init_db(root / ".reverse" / "index.sqlite3")
    try:
        build_index(conn, str(root))
        fqns = {r["fqn"] for r in conn.execute("SELECT fqn FROM class")}
        assert "ru.bank.Foo" in fqns
    finally:
        conn.close()


def test_repo_root_named_build_keeps_src(tmp_path):
    root = tmp_path / "build"
    _write(root, {"pom.xml": _POM, "src/main/java/ru/bank/Foo.java": _FOO})
    # the walk must not prune src/ away just because the root is named 'build'
    result = scan_repo(str(root))
    assert result.total_java_files >= 1
    conn = init_db(root / ".reverse" / "index.sqlite3")
    try:
        build_index(conn, str(root))
        fqns = {r["fqn"] for r in conn.execute("SELECT fqn FROM class")}
        assert "ru.bank.Foo" in fqns
    finally:
        conn.close()
