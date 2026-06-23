"""Shared, framework-agnostic data contracts for legacy-reverse-mcp.

The ``evidence`` module defines the provability layer used across the tool:
``Evidence``, ``ConfidenceLevel``, ``Limitation``, ``ObservedFact`` and
``InferredFinding``. These are the single contract that scanners, the SQLite
repository and (later) the MCP tools all speak, so a consumer can always see
*what* a result is based on, *how* confident the tool is, and *what* it does
not know.
"""

from models.evidence import (
    LIMITATIONS,
    ConfidenceLevel,
    Evidence,
    InferredFinding,
    Limitation,
    ObservedFact,
    limitation,
)

__all__ = [
    "ConfidenceLevel",
    "Evidence",
    "InferredFinding",
    "Limitation",
    "ObservedFact",
    "LIMITATIONS",
    "limitation",
]
