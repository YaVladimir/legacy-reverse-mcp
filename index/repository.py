"""SQLite repository layer: schema bootstrap + basic CRUD for module/package/class/method/endpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    commit: bool = True,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO endpoint (
            http_method, path, full_path, controller_class_id, handler_method_id,
            produces, consumes, request_dto_fqn, response_dto_fqn, deprecated
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
