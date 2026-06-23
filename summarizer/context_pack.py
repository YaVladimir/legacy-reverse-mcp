"""Build compact, task-relevant context packs for agents."""

from __future__ import annotations

import sqlite3


def generate_context_pack(conn: sqlite3.Connection, task: str, max_tokens: int = 4000) -> dict:
    raise NotImplementedError("context pack generation not implemented yet")
