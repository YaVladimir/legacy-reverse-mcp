"""Full-text search helpers over the search_index FTS5 virtual table."""

from __future__ import annotations

import sqlite3


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    raise NotImplementedError("search_index population and querying not implemented yet")
