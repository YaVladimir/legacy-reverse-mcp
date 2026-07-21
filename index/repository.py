"""SQLite repository layer: schema bootstrap + basic CRUD for module/package/class/method/endpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models.evidence import Evidence, InferredFinding, Limitation, ObservedFact

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_endpoint_schema(conn)
    return conn


# expected columns of the current endpoint table / v_endpoint_full view; their
# absence in an already-built index means it predates a schema addition and must
# be migrated forward on open (read tools happily read old indexes without a
# rescan, so a stale endpoint view would otherwise raise mid-query instead).
_ENDPOINT_ADDED_COLUMNS = (
    ("annotation_class_id", "INTEGER"),
    ("annotation_method_id", "INTEGER"),
    ("request_dto_fqn", "TEXT"),
    ("superseded", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_endpoint_schema(conn: sqlite3.Connection) -> None:
    """Forward-migrate an older ``endpoint`` table + ``v_endpoint_full`` view in
    place. Idempotent and a near-free no-op once the schema is current (only two
    PRAGMA reads). Fixes reads of pre-supersede / pre-provenance indexes that the
    read tools accept without a rescan."""
    ep_cols = {r[1] for r in conn.execute("PRAGMA table_info(endpoint)")}
    if not ep_cols:
        return  # not an index database (or brand new) — nothing to migrate
    view_cols = {r[1] for r in conn.execute("PRAGMA table_info(v_endpoint_full)")}
    if "superseded" in ep_cols and "annotation_inherited" in view_cols:
        return  # already current
    for col, ddl in _ENDPOINT_ADDED_COLUMNS:
        if col not in ep_cols:
            conn.execute(f"ALTER TABLE endpoint ADD COLUMN {col} {ddl}")
    # CREATE VIEW IF NOT EXISTS won't replace a stale definition — drop, then let
    # the canonical schema recreate it (every other statement is idempotent).
    conn.execute("DROP VIEW IF EXISTS v_endpoint_full")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# ------------------------------------------------------------
# module
# ------------------------------------------------------------

def insert_module(
    conn: sqlite3.Connection,
    name: str,
    path: str,
    build_file: str | None = None,
    group_id: str | None = None,
    artifact_id: str | None = None,
    version: str | None = None,
    packaging: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO module (name, path, build_file, group_id, artifact_id, version, packaging)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            path = excluded.path,
            build_file = excluded.build_file,
            group_id = excluded.group_id,
            artifact_id = excluded.artifact_id,
            version = excluded.version,
            packaging = excluded.packaging
        RETURNING id
        """,
        (name, path, build_file, group_id, artifact_id, version, packaging),
    )
    module_id = cur.fetchone()["id"]
    conn.commit()
    return module_id


def get_module(conn: sqlite3.Connection, module_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM module WHERE id = ?", (module_id,)).fetchone()


def list_modules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM module ORDER BY name").fetchall()


def module_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM module WHERE path = ?", (path,)).fetchone()


def module_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM module WHERE name = ?", (name,)).fetchone()


# ------------------------------------------------------------
# package
# ------------------------------------------------------------

def insert_package(
    conn: sqlite3.Connection,
    fqn: str,
    module_id: int | None = None,
    path: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO package (fqn, module_id, path)
        VALUES (?, ?, ?)
        ON CONFLICT(fqn) DO UPDATE SET module_id = excluded.module_id, path = excluded.path
        RETURNING id
        """,
        (fqn, module_id, path),
    )
    package_id = cur.fetchone()["id"]
    if commit:
        conn.commit()
    return package_id


def get_package(conn: sqlite3.Connection, package_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM package WHERE id = ?", (package_id,)).fetchone()


def list_packages(conn: sqlite3.Connection, module_id: int | None = None) -> list[sqlite3.Row]:
    if module_id is not None:
        return conn.execute(
            "SELECT * FROM package WHERE module_id = ? ORDER BY fqn", (module_id,)
        ).fetchall()
    return conn.execute("SELECT * FROM package ORDER BY fqn").fetchall()


# ------------------------------------------------------------
# class
# ------------------------------------------------------------

def insert_class(
    conn: sqlite3.Connection,
    fqn: str,
    simple_name: str,
    file_path: str,
    package_id: int | None = None,
    module_id: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    kind: str = "class",
    role: str = "unknown",
    is_abstract: bool = False,
    visibility: str = "public",
    superclass_fqn: str | None = None,
    summary: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO class (
            fqn, simple_name, package_id, module_id, file_path, line_start, line_end,
            kind, role, is_abstract, visibility, superclass_fqn, summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fqn) DO UPDATE SET
            simple_name = excluded.simple_name,
            package_id = excluded.package_id,
            module_id = excluded.module_id,
            file_path = excluded.file_path,
            line_start = excluded.line_start,
            line_end = excluded.line_end,
            kind = excluded.kind,
            role = excluded.role,
            is_abstract = excluded.is_abstract,
            visibility = excluded.visibility,
            superclass_fqn = excluded.superclass_fqn,
            summary = excluded.summary
        RETURNING id
        """,
        (
            fqn,
            simple_name,
            package_id,
            module_id,
            file_path,
            line_start,
            line_end,
            kind,
            role,
            int(is_abstract),
            visibility,
            superclass_fqn,
            summary,
        ),
    )
    class_id = cur.fetchone()["id"]
    if commit:
        conn.commit()
    return class_id


def get_class(conn: sqlite3.Connection, class_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM class WHERE id = ?", (class_id,)).fetchone()


def get_class_by_fqn(conn: sqlite3.Connection, fqn: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM class WHERE fqn = ?", (fqn,)).fetchone()


def list_classes(
    conn: sqlite3.Connection, module_id: int | None = None, role: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM class WHERE 1=1"
    params: list = []
    if module_id is not None:
        query += " AND module_id = ?"
        params.append(module_id)
    if role is not None:
        query += " AND role = ?"
        params.append(role)
    query += " ORDER BY fqn"
    return conn.execute(query, params).fetchall()


# ------------------------------------------------------------
# method
# ------------------------------------------------------------

def insert_method(
    conn: sqlite3.Connection,
    class_id: int,
    name: str,
    signature: str,
    return_type: str | None = None,
    visibility: str = "public",
    is_static: bool = False,
    is_constructor: bool = False,
    line_start: int | None = None,
    line_end: int | None = None,
    summary: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO method (
            class_id, name, signature, return_type, visibility,
            is_static, is_constructor, line_start, line_end, summary
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            class_id,
            name,
            signature,
            return_type,
            visibility,
            int(is_static),
            int(is_constructor),
            line_start,
            line_end,
            summary,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------
# class/method annotations, fields, parameters, interfaces
# ------------------------------------------------------------

def clear_class_members(conn: sqlite3.Connection, class_id: int, commit: bool = True) -> None:
    """Remove methods/fields/annotations/interfaces of a class before re-indexing.

    method_annotation / method_parameter rows cascade from method via FK.
    """
    conn.execute("DELETE FROM method_call WHERE caller_class_id = ?", (class_id,))
    # endpoints must go BEFORE methods: deleting methods first flips their
    # handler_method_id to NULL (FK SET NULL) and, when the same FQN appears in
    # two files in one scan, leaves orphaned non-superseded rows that duplicate
    # the re-indexed endpoints in v_endpoint_full
    conn.execute("DELETE FROM endpoint WHERE controller_class_id = ?", (class_id,))
    conn.execute("DELETE FROM method WHERE class_id = ?", (class_id,))
    conn.execute("DELETE FROM field WHERE class_id = ?", (class_id,))
    conn.execute("DELETE FROM class_annotation WHERE class_id = ?", (class_id,))
    conn.execute("DELETE FROM class_interface WHERE class_id = ?", (class_id,))
    if commit:
        conn.commit()


def insert_class_annotation(
    conn: sqlite3.Connection,
    class_id: int,
    name: str,
    attributes: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO class_annotation (class_id, name, attributes) VALUES (?, ?, ?)",
        (class_id, name, attributes),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def insert_class_interface(
    conn: sqlite3.Connection, class_id: int, interface_fqn: str, commit: bool = True
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO class_interface (class_id, interface_fqn) VALUES (?, ?)",
        (class_id, interface_fqn),
    )
    if commit:
        conn.commit()


def insert_method_annotation(
    conn: sqlite3.Connection,
    method_id: int,
    name: str,
    attributes: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO method_annotation (method_id, name, attributes) VALUES (?, ?, ?)",
        (method_id, name, attributes),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def insert_method_parameter(
    conn: sqlite3.Connection,
    method_id: int,
    position: int,
    name: str | None,
    type_fqn: str | None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO method_parameter (method_id, position, name, type_fqn) VALUES (?, ?, ?, ?)",
        (method_id, position, name, type_fqn),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def clear_class_dependencies(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.execute("DELETE FROM class_dependency")
    if commit:
        conn.commit()


def insert_class_dependency(
    conn: sqlite3.Connection,
    from_class_id: int,
    to_class_id: int,
    kind: str = "unknown",
    commit: bool = True,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO class_dependency (from_class_id, to_class_id, kind) "
        "VALUES (?, ?, ?)",
        (from_class_id, to_class_id, kind),
    )
    if commit:
        conn.commit()


def insert_method_call(
    conn: sqlite3.Connection,
    caller_method_id: int,
    caller_class_id: int,
    callee_name: str,
    receiver_field: str | None = None,
    receiver_type_fqn: str | None = None,
    line: int | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO method_call "
        "(caller_method_id, caller_class_id, callee_name, receiver_field, receiver_type_fqn, line) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (caller_method_id, caller_class_id, callee_name, receiver_field, receiver_type_fqn, line),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def insert_field(
    conn: sqlite3.Connection,
    class_id: int,
    name: str,
    type_fqn: str | None = None,
    visibility: str = "private",
    is_static: bool = False,
    is_injected: bool = False,
    annotation_names: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO field (
            class_id, name, type_fqn, visibility, is_static, is_injected, annotation_names
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (class_id, name, type_fqn, visibility, int(is_static), int(is_injected), annotation_names),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_method(conn: sqlite3.Connection, method_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM method WHERE id = ?", (method_id,)).fetchone()


def list_methods(conn: sqlite3.Connection, class_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM method WHERE class_id = ? ORDER BY line_start", (class_id,)
    ).fetchall()


# ------------------------------------------------------------
# endpoint
# ------------------------------------------------------------

def insert_endpoint(
    conn: sqlite3.Connection,
    http_method: str,
    path: str,
    full_path: str | None = None,
    controller_class_id: int | None = None,
    handler_method_id: int | None = None,
    produces: str | None = None,
    consumes: str | None = None,
    request_dto_fqn: str | None = None,
    response_dto_fqn: str | None = None,
    deprecated: bool = False,
    annotation_class_id: int | None = None,
    annotation_method_id: int | None = None,
    commit: bool = True,
) -> int:
    """``annotation_class_id``/``annotation_method_id`` record where the mapping
    annotation itself lives; default to ``controller_class_id``/``handler_method_id``
    (the common case: annotation is directly on the controller)."""
    cur = conn.execute(
        """
        INSERT INTO endpoint (
            http_method, path, full_path, controller_class_id, handler_method_id,
            produces, consumes, request_dto_fqn, response_dto_fqn, deprecated,
            annotation_class_id, annotation_method_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            http_method,
            path,
            full_path,
            controller_class_id,
            handler_method_id,
            produces,
            consumes,
            request_dto_fqn,
            response_dto_fqn,
            int(deprecated),
            annotation_class_id if annotation_class_id is not None else controller_class_id,
            annotation_method_id if annotation_method_id is not None else handler_method_id,
        ),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def get_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM endpoint WHERE id = ?", (endpoint_id,)).fetchone()


def list_endpoints(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM v_endpoint_full ORDER BY full_path").fetchall()


# ------------------------------------------------------------
# dependencies (module -> module, module -> external artifact)
# ------------------------------------------------------------

def clear_dependencies(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.execute("DELETE FROM module_dependency")
    conn.execute("DELETE FROM external_dependency")
    if commit:
        conn.commit()


def insert_module_dependency(
    conn: sqlite3.Connection,
    from_module_id: int,
    to_module_id: int,
    scope: str | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO module_dependency (from_module_id, to_module_id, scope) "
        "VALUES (?, ?, ?)",
        (from_module_id, to_module_id, scope),
    )
    if commit:
        conn.commit()


def insert_external_dependency(
    conn: sqlite3.Connection,
    module_id: int | None,
    group_id: str,
    artifact_id: str,
    version: str | None = None,
    scope: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO external_dependency (module_id, group_id, artifact_id, version, scope) "
        "VALUES (?, ?, ?, ?, ?)",
        (module_id, group_id, artifact_id, version, scope),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_module_dependencies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM module_dependency").fetchall()


# ------------------------------------------------------------
# config files / properties (application.yml / .properties)
# ------------------------------------------------------------

def clear_config(conn: sqlite3.Connection, commit: bool = True) -> None:
    # config_property rows cascade from config_file via FK ON DELETE CASCADE
    conn.execute("DELETE FROM config_file")
    if commit:
        conn.commit()


def insert_config_file(
    conn: sqlite3.Connection,
    file_path: str,
    kind: str = "unknown",
    module_id: int | None = None,
    profile: str | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO config_file (module_id, file_path, kind, profile)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(file_path) DO UPDATE SET
            module_id = excluded.module_id,
            kind = excluded.kind,
            profile = excluded.profile
        RETURNING id
        """,
        (module_id, file_path, kind, profile),
    )
    config_file_id = cur.fetchone()["id"]
    if commit:
        conn.commit()
    return config_file_id


def insert_config_property(
    conn: sqlite3.Connection,
    config_file_id: int,
    key: str,
    value: str | None = None,
    is_secret: bool = False,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO config_property (config_file_id, key, value, is_secret) "
        "VALUES (?, ?, ?, ?)",
        (config_file_id, key, value, int(is_secret)),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_config_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT cf.id, cf.file_path, cf.kind, cf.profile, mo.name AS module_name, "
        "       COUNT(cp.id) AS property_count "
        "FROM config_file cf "
        "LEFT JOIN module mo ON mo.id = cf.module_id "
        "LEFT JOIN config_property cp ON cp.config_file_id = cf.id "
        "GROUP BY cf.id ORDER BY cf.file_path"
    ).fetchall()


def list_config_properties(
    conn: sqlite3.Connection,
    key_contains: str | None = None,
    profile: str | None = None,
    include_secret_values: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Read config properties joined to their file/profile. Secret values are
    masked unless ``include_secret_values`` is set (the index keeps them raw)."""
    query = (
        "SELECT cp.key, cp.value, cp.is_secret, cf.file_path, cf.profile "
        "FROM config_property cp JOIN config_file cf ON cf.id = cp.config_file_id "
        "WHERE 1=1"
    )
    params: list = []
    if key_contains:
        query += " AND cp.key LIKE ?"
        params.append(f"%{key_contains}%")
    if profile is not None:
        query += " AND cf.profile IS ?"
        params.append(profile)
    query += " ORDER BY cf.file_path, cp.key LIMIT ?"
    params.append(limit)

    out: list[dict] = []
    for r in conn.execute(query, params):
        d = dict(r)
        if d["is_secret"] and not include_secret_values:
            d["value"] = "***"
        out.append(d)
    return out


def count_config_properties(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM config_property").fetchone()[0]


# ------------------------------------------------------------
# summaries
# ------------------------------------------------------------

def set_class_summary(conn: sqlite3.Connection, class_id: int, summary: str, commit: bool = True) -> None:
    conn.execute("UPDATE class SET summary = ? WHERE id = ?", (summary, class_id))
    if commit:
        conn.commit()


def set_method_summary(conn: sqlite3.Connection, method_id: int, summary: str, commit: bool = True) -> None:
    conn.execute("UPDATE method SET summary = ? WHERE id = ?", (summary, method_id))
    if commit:
        conn.commit()


def insert_summary(
    conn: sqlite3.Connection,
    kind: str,
    ref_id: int | None,
    content: str,
    model: str | None = None,
    token_count: int | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO summary (kind, ref_id, content, model, token_count) VALUES (?, ?, ?, ?, ?)",
        (kind, ref_id, content, model, token_count),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def clear_summaries(conn: sqlite3.Connection, kind: str | None = None, commit: bool = True) -> None:
    if kind:
        conn.execute("DELETE FROM summary WHERE kind = ?", (kind,))
    else:
        conn.execute("DELETE FROM summary")
    if commit:
        conn.commit()


# ------------------------------------------------------------
# findings
# ------------------------------------------------------------

def clear_findings(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.execute("DELETE FROM finding")
    if commit:
        conn.commit()


def insert_finding(
    conn: sqlite3.Connection,
    kind: str,
    description: str,
    severity: str = "info",
    class_id: int | None = None,
    method_id: int | None = None,
    module_id: int | None = None,
    commit: bool = True,
) -> int:
    cur = conn.execute(
        "INSERT INTO finding (kind, severity, class_id, method_id, module_id, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, severity, class_id, method_id, module_id, description),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------
# provability layer: observed facts / inferred findings / evidence / limitations
# ------------------------------------------------------------

OWNER_OBSERVED_FACT = "observed_fact"
OWNER_INFERRED_FINDING = "inferred_finding"


def _insert_evidence(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, ev: Evidence
) -> None:
    conn.execute(
        "INSERT INTO evidence "
        "(owner_type, owner_id, kind, description, file_path, line_start, line_end, symbol, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            owner_type,
            owner_id,
            ev.kind,
            ev.description,
            ev.file_path,
            ev.line_start,
            ev.line_end,
            ev.symbol,
            ev.source,
        ),
    )


def _insert_limitation(
    conn: sqlite3.Connection, owner_type: str, owner_id: int, lim: Limitation
) -> None:
    conn.execute(
        "INSERT INTO limitations (owner_type, owner_id, code, description) VALUES (?, ?, ?, ?)",
        (owner_type, owner_id, lim.code, lim.description),
    )


def insert_observed_fact(
    conn: sqlite3.Connection, fact: ObservedFact, commit: bool = True
) -> int:
    """Persist an ObservedFact and its evidence rows. Returns the new fact id."""
    cur = conn.execute(
        "INSERT INTO observed_facts (fact_type, subject, predicate, object, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (fact.fact_type, fact.subject, fact.predicate, fact.object, fact.confidence.value),
    )
    fact_id = cur.lastrowid
    for ev in fact.evidence:
        _insert_evidence(conn, OWNER_OBSERVED_FACT, fact_id, ev)
    if commit:
        conn.commit()
    return fact_id


def insert_inferred_finding(
    conn: sqlite3.Connection, finding: InferredFinding, commit: bool = True
) -> int:
    """Persist an InferredFinding with its evidence + limitations. Returns the new id.

    The pydantic model already guarantees at least one evidence item.
    """
    cur = conn.execute(
        "INSERT INTO inferred_findings (finding_type, subject, summary, confidence) "
        "VALUES (?, ?, ?, ?)",
        (finding.finding_type, finding.subject, finding.summary, finding.confidence.value),
    )
    finding_id = cur.lastrowid
    for ev in finding.evidence:
        _insert_evidence(conn, OWNER_INFERRED_FINDING, finding_id, ev)
    for lim in finding.limitations:
        _insert_limitation(conn, OWNER_INFERRED_FINDING, finding_id, lim)
    if commit:
        conn.commit()
    return finding_id


def clear_observed_facts(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.execute("DELETE FROM evidence WHERE owner_type = ?", (OWNER_OBSERVED_FACT,))
    conn.execute("DELETE FROM limitations WHERE owner_type = ?", (OWNER_OBSERVED_FACT,))
    conn.execute("DELETE FROM observed_facts")
    if commit:
        conn.commit()


def clear_inferred_findings(conn: sqlite3.Connection, commit: bool = True) -> None:
    conn.execute("DELETE FROM evidence WHERE owner_type = ?", (OWNER_INFERRED_FINDING,))
    conn.execute("DELETE FROM limitations WHERE owner_type = ?", (OWNER_INFERRED_FINDING,))
    conn.execute("DELETE FROM inferred_findings")
    if commit:
        conn.commit()


def _evidence_for(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT kind, description, file_path, line_start, line_end, symbol, source "
            "FROM evidence WHERE owner_type = ? AND owner_id = ? ORDER BY id",
            (owner_type, owner_id),
        )
    ]


def _limitations_for(conn: sqlite3.Connection, owner_type: str, owner_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT code, description FROM limitations WHERE owner_type = ? AND owner_id = ? ORDER BY id",
            (owner_type, owner_id),
        )
    ]


def list_observed_facts(
    conn: sqlite3.Connection,
    subject: str | None = None,
    fact_type: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Read observed facts (each with its evidence) as plain dicts, newest-id first."""
    query = "SELECT * FROM observed_facts WHERE 1=1"
    params: list = []
    if subject is not None:
        query += " AND subject = ?"
        params.append(subject)
    if fact_type is not None:
        query += " AND fact_type = ?"
        params.append(fact_type)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)

    out: list[dict] = []
    for r in conn.execute(query, params):
        d = dict(r)
        d["evidence"] = _evidence_for(conn, OWNER_OBSERVED_FACT, r["id"])
        out.append(d)
    return out


def list_inferred_findings(
    conn: sqlite3.Connection,
    subject: str | None = None,
    finding_type: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Read inferred findings (each with evidence + limitations) as plain dicts."""
    query = "SELECT * FROM inferred_findings WHERE 1=1"
    params: list = []
    if subject is not None:
        query += " AND subject = ?"
        params.append(subject)
    if finding_type is not None:
        query += " AND finding_type = ?"
        params.append(finding_type)
    query += " ORDER BY id LIMIT ?"
    params.append(limit)

    out: list[dict] = []
    for r in conn.execute(query, params):
        d = dict(r)
        d["evidence"] = _evidence_for(conn, OWNER_INFERRED_FINDING, r["id"])
        d["limitations"] = _limitations_for(conn, OWNER_INFERRED_FINDING, r["id"])
        out.append(d)
    return out


def count_observed_facts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM observed_facts").fetchone()[0]


def observed_facts_for_class(
    conn: sqlite3.Connection, fqn: str, limit: int = 1000
) -> list[dict]:
    """Facts about the class itself and its members (subjects ``fqn``, ``fqn#m``, ``fqn.f``).

    Uses prefix matching via substr (not LIKE) so names with ``_`` don't over-match.
    """
    prefix_len = len(fqn) + 1
    rows = conn.execute(
        "SELECT * FROM observed_facts WHERE subject = ? "
        "OR substr(subject, 1, ?) = ? OR substr(subject, 1, ?) = ? "
        "ORDER BY id LIMIT ?",
        (fqn, prefix_len, fqn + "#", prefix_len, fqn + ".", limit),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["evidence"] = _evidence_for(conn, OWNER_OBSERVED_FACT, r["id"])
        out.append(d)
    return out
