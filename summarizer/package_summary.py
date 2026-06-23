"""Generate markdown summaries for packages via batch LLM calls."""

from __future__ import annotations

import sqlite3


def summarize_package(conn: sqlite3.Connection, package_id: int) -> str:
    raise NotImplementedError("package summarization not implemented yet")
