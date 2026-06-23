"""Shared helpers for the analysis layer: confidence math, evidence/limitation
builders, JSON dumping and structured errors."""

from __future__ import annotations

from typing import Iterable

from models import LIMITATIONS, ConfidenceLevel, Evidence, Limitation

# weakest -> strongest, for aggregating an overall confidence
_CONF_RANK = {
    ConfidenceLevel.UNKNOWN: 0,
    ConfidenceLevel.LOW: 1,
    ConfidenceLevel.MEDIUM: 2,
    ConfidenceLevel.HIGH: 3,
}
_RANK_CONF = {v: k for k, v in _CONF_RANK.items()}


def _as_level(value) -> ConfidenceLevel:
    if isinstance(value, ConfidenceLevel):
        return value
    return ConfidenceLevel(value)


def min_confidence(values: Iterable) -> ConfidenceLevel:
    """Overall confidence = the weakest link. Empty -> unknown."""
    ranks = [_CONF_RANK[_as_level(v)] for v in values]
    if not ranks:
        return ConfidenceLevel.UNKNOWN
    return _RANK_CONF[min(ranks)]


def max_confidence(values: Iterable) -> ConfidenceLevel:
    ranks = [_CONF_RANK[_as_level(v)] for v in values]
    if not ranks:
        return ConfidenceLevel.UNKNOWN
    return _RANK_CONF[max(ranks)]


def ev(
    kind: str,
    description: str,
    *,
    file_path: str | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    symbol: str | None = None,
    source: str = "source",
) -> dict:
    """Build a JSON-able evidence item."""
    return Evidence(
        kind=kind,
        description=description,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        symbol=symbol,
        source=source,
    ).model_dump(mode="json")


def limitations(*codes: str, extra: list[Limitation] | None = None) -> list[dict]:
    """Resolve reusable limitation codes (+ any custom ones) to JSON-able dicts."""
    out = [LIMITATIONS[c].model_dump(mode="json") for c in codes]
    for lim in extra or []:
        out.append(lim.model_dump(mode="json"))
    return out


def conf_str(value) -> str:
    return _as_level(value).value


def meta(result: dict, *, confidence=None, limitation_codes=(), warnings=None) -> dict:
    """Attach the standard cross-cutting fields to a tool result (idempotent).

    Used to lift the simpler list/overview tools to the same envelope as the
    evidence-based tools: confidence + limitations + warnings, never plain text.
    """
    if confidence is not None and "confidence" not in result:
        result["confidence"] = conf_str(confidence)
    if "limitations" not in result:
        result["limitations"] = limitations(*limitation_codes)
    if "warnings" not in result:
        result["warnings"] = list(warnings or [])
    return result


def not_found(kind: str, query, suggestions: list) -> dict:
    """Uniform structured 'not found' error with suggestions for next steps."""
    return {
        "error": "not_found",
        "kind": kind,
        "query": query,
        "message": f"No {kind} matched {query!r}.",
        "suggestions": suggestions,
    }
