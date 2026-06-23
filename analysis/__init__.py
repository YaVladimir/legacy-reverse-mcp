"""Evidence-based analysis layer.

These modules turn the static index (classes, methods, fields, endpoints,
observed facts, intra-class calls) into honest, explainable answers: every
heuristic conclusion is an ``InferredFinding`` with evidence + confidence, and
every response carries its ``limitations``. The raw SQL stays in
``index.queries`` / ``index.repository``; this package owns the reasoning.
"""
