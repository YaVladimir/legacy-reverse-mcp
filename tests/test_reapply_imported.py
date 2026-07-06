"""A forced rescan rebuilds index.sqlite3 and used to silently wipe imported
descriptions until the next manual describe/import-arch run; the pipeline now
restores fresh ones from the durable store (descriptions.sqlite3) automatically."""

from __future__ import annotations

from analysis.flat_arch import import_flat
from index.repository import init_db
from scanner.pipeline import build_index
from tests.conftest import write_fixture_repo

_FQN = "ru.bank.deposit.DepositService"
_CLASS_DESC = "Сервис создания и поиска депозитов; вся запись идёт через DepositRepository."
_METHOD_DESC = "Создаёт депозит и сохраняет его в репозитории."

_FLAT = {
    "project": "fixture",
    "classes": [
        {
            "id": "src/main/java/ru/bank/deposit/DepositService",
            "pkg": "ru.bank.deposit",
            "name": "DepositService",
            "fqn": _FQN,
            "description": _CLASS_DESC,
            "methods": [
                {"sig": "create(DepositRequest req): Deposit", "description": _METHOD_DESC},
            ],
        }
    ],
}


def _rescan(repo_root):
    """Simulate `scan --force`: drop the index file and rebuild from scratch."""
    db = repo_root / ".reverse" / "index.sqlite3"
    db.unlink()
    conn = init_db(db)
    summary = build_index(conn, str(repo_root))
    return conn, summary


def _scan_and_import(tmp_path):
    repo_root = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo_root / ".reverse" / "index.sqlite3")
    build_index(conn, str(repo_root))
    stats = import_flat(conn, str(repo_root), _FLAT, source="test")
    assert stats["classes_matched"] == 1
    conn.close()
    return repo_root


def test_force_rescan_restores_imported_descriptions(tmp_path):
    repo_root = _scan_and_import(tmp_path)

    conn, summary = _rescan(repo_root)
    try:
        row = conn.execute("SELECT summary FROM class WHERE fqn = ?", (_FQN,)).fetchone()
        assert row["summary"] == _CLASS_DESC
        mrow = conn.execute(
            "SELECT m.summary FROM method m JOIN class c ON c.id = m.class_id "
            "WHERE c.fqn = ? AND m.name = 'create'",
            (_FQN,),
        ).fetchone()
        assert mrow["summary"] == _METHOD_DESC
        assert summary["restored_descriptions"]["classes"] == 1
        assert summary["restored_descriptions"]["stale"] == 0
    finally:
        conn.close()


def test_stale_import_not_restored_after_code_change(tmp_path):
    repo_root = _scan_and_import(tmp_path)

    src = repo_root / "src/main/java/ru/bank/deposit/DepositService.java"
    src.write_text(
        src.read_text(encoding="utf-8").replace(
            "public Deposit find(Long id)", "public Deposit findActive(Long id)"
        ),
        encoding="utf-8",
    )

    conn, summary = _rescan(repo_root)
    try:
        row = conn.execute("SELECT summary FROM class WHERE fqn = ?", (_FQN,)).fetchone()
        # deterministic scan summary, not the (now stale) imported text
        assert row["summary"] != _CLASS_DESC
        assert summary["restored_descriptions"]["stale"] == 1
        assert summary["restored_descriptions"]["classes"] == 0
    finally:
        conn.close()


def test_scan_without_cache_restores_nothing(tmp_path):
    repo_root = write_fixture_repo(tmp_path / "repo")
    conn = init_db(repo_root / ".reverse" / "index.sqlite3")
    try:
        summary = build_index(conn, str(repo_root))
        assert summary["restored_descriptions"] == {
            "classes": 0, "methods": 0, "stale": 0, "unmatched": 0,
        }
        # no empty descriptions.sqlite3 must appear as a side effect of scan
        assert not (repo_root / ".reverse" / "descriptions.sqlite3").exists()
    finally:
        conn.close()
