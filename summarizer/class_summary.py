"""Generate markdown summaries for classes via batch LLM calls."""

from __future__ import annotations

import sqlite3


def summarize_class(conn: sqlite3.Connection, class_id: int) -> str:
    raise NotImplementedError("class summarization not implemented yet")


def summarize_classes_batch(conn: sqlite3.Connection, class_ids: list[int]) -> dict[int, str]:
    raise NotImplementedError("batch class summarization not implemented yet")
