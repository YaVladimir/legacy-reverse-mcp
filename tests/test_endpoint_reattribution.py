"""The two headline behaviours of the codegen fix, previously untested:
indexing of build/generated/** (while the rest of build/ stays ignored) and
reattribution of endpoints from a codegen interface to the concrete controller.

Each test builds its own tiny repo modelled on the openapi-generator layout
(annotated API interface under build/generated, bare @RestController impl in src)
and runs the real pipeline end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from index.repository import init_db
from scanner.pipeline import build_index

_GEN = "build/generated/openapi/src/main/java/ru/bank/api"
_SRC = "src/main/java/ru/bank/api"

_POM = "<project><groupId>ru.bank</groupId><artifactId>api</artifactId><version>1</version></project>"

_DEALS_API = """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

public interface DealsApi {
    @GetMapping("/deals/{id}")
    String getDeal(Long id);
}
"""


def _scan(root: Path, files: dict[str, str]):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    summary = build_index(conn, str(root))
    return conn, summary


def test_endpoint_moves_from_generated_interface_to_controller(tmp_path):
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": _DEALS_API,
        f"{_SRC}/DealController.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1")
public class DealController implements DealsApi {
    @Override
    public String getDeal(Long id) { return "deal"; }
}
""",
    })
    try:
        rows = conn.execute(
            "SELECT * FROM v_endpoint_full WHERE http_method = 'GET'").fetchall()
        assert len(rows) == 1
        ep = rows[0]
        # attributed to the concrete controller, with ITS base path
        assert ep["controller_fqn"] == "ru.bank.api.DealController"
        assert ep["full_path"] == "/v1/deals/{id}"
        # provenance: the real mapping annotation lives on the generated interface
        assert ep["annotation_fqn"] == "ru.bank.api.DealsApi"
        assert ep["annotation_inherited"] == 1
        assert summary["reattributed_endpoints"] == 1
        assert summary["endpoints"] == 1  # manifest count is post-reattribution
    finally:
        conn.close()


def test_build_generated_indexed_but_rest_of_build_ignored(tmp_path):
    conn, _ = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": _DEALS_API,
        # compiled/copied sources under other build/ children must stay invisible
        "build/classes/java/main/ru/bank/api/Garbage.java":
            "package ru.bank.api;\npublic class Garbage { }\n",
        "build/Tmp.java": "package ru.bank.api;\npublic class Tmp { }\n",
    })
    try:
        fqns = {r["fqn"] for r in conn.execute("SELECT fqn FROM class")}
        assert "ru.bank.api.DealsApi" in fqns          # build/generated/** is real source
        assert "ru.bank.api.Garbage" not in fqns       # build/classes/** is not
        assert "ru.bank.api.Tmp" not in fqns           # files directly in build/ neither
    finally:
        conn.close()


def test_sibling_controllers_each_get_their_own_endpoint(tmp_path):
    impl = """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("%s")
public class %s implements DealsApi {
    @Override
    public String getDeal(Long id) { return "deal"; }
}
"""
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": _DEALS_API,
        f"{_SRC}/DealControllerA.java": impl % ("/a", "DealControllerA"),
        f"{_SRC}/DealControllerB.java": impl % ("/b", "DealControllerB"),
    })
    try:
        rows = conn.execute(
            "SELECT controller_fqn, full_path FROM v_endpoint_full ORDER BY full_path").fetchall()
        got = [(r["controller_fqn"], r["full_path"]) for r in rows]
        assert got == [
            ("ru.bank.api.DealControllerA", "/a/deals/{id}"),
            ("ru.bank.api.DealControllerB", "/b/deals/{id}"),
        ]
        # both siblings replaced it -> the interface-level row is gone
        assert summary["reattributed_endpoints"] == 2
    finally:
        conn.close()


def test_delegate_controller_keeps_interface_endpoint_row(tmp_path):
    """Delegate pattern: the controller inherits the interface's default method
    without overriding it. Reattribution can't link it to the controller, but the
    interface-level endpoint row must survive — not vanish from the index."""
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

public interface DealsApi {
    @GetMapping("/deals/{id}")
    default String getDeal(Long id) { return "default"; }
}
""",
        f"{_SRC}/DealController.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
public class DealController implements DealsApi {
}
""",
    })
    try:
        rows = conn.execute("SELECT controller_fqn FROM v_endpoint_full").fetchall()
        assert [r["controller_fqn"] for r in rows] == ["ru.bank.api.DealsApi"]
        assert summary["reattributed_endpoints"] == 0
    finally:
        conn.close()


def test_interface_class_level_request_mapping_is_inherited(tmp_path):
    """H3: the interface carries the type-level @RequestMapping('/api/v1') and the
    concrete controller is bare. Spring serves /api/v1/deals — the reattributed row
    must inherit the interface base path, not drop it to /deals."""
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RequestMapping("/api/v1")
public interface DealsApi {
    @GetMapping("/deals")
    String getDeals();
}
""",
        f"{_SRC}/DealController.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
public class DealController implements DealsApi {
    @Override
    public String getDeals() { return "deals"; }
}
""",
    })
    try:
        rows = conn.execute(
            "SELECT controller_fqn, full_path FROM v_endpoint_full").fetchall()
        got = [(r["controller_fqn"], r["full_path"]) for r in rows]
        assert got == [("ru.bank.api.DealController", "/api/v1/deals")]
    finally:
        conn.close()


def test_sibling_with_own_annotation_supersedes_interface_row(tmp_path):
    """H4: one sibling overrides without an annotation (reattributed), the other
    overrides *with its own* @GetMapping (indexed directly). Both represent the
    interface method, so the interface row must be superseded — not linger as a
    phantom third endpoint."""
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": _DEALS_API,
        f"{_SRC}/DealControllerA.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/a")
public class DealControllerA implements DealsApi {
    @Override
    public String getDeal(Long id) { return "deal"; }
}
""",
        f"{_SRC}/DealControllerB.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
public class DealControllerB implements DealsApi {
    @Override
    @GetMapping("/b/deals/{id}")
    public String getDeal(Long id) { return "deal"; }
}
""",
    })
    try:
        got = {(r["controller_fqn"], r["full_path"]) for r in conn.execute(
            "SELECT controller_fqn, full_path FROM v_endpoint_full")}
        assert got == {
            ("ru.bank.api.DealControllerA", "/a/deals/{id}"),  # reattributed
            ("ru.bank.api.DealControllerB", "/b/deals/{id}"),  # own annotation
        }
        # the interface row is not deleted, only hidden (superseded)
        interface_rows = conn.execute(
            "SELECT e.superseded FROM endpoint e JOIN class c ON c.id = e.controller_class_id "
            "WHERE c.fqn = 'ru.bank.api.DealsApi'").fetchall()
        assert [r["superseded"] for r in interface_rows] == [1]
    finally:
        conn.close()


def test_superseded_interface_row_is_kept_not_deleted(tmp_path):
    """Full sibling coverage supersedes (hides) the interface row but never deletes
    it: the raw endpoint table still holds it, so a heuristic mistake is recoverable."""
    conn, _ = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": _DEALS_API,
        f"{_SRC}/DealController.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1")
public class DealController implements DealsApi {
    @Override
    public String getDeal(Long id) { return "deal"; }
}
""",
    })
    try:
        total = conn.execute("SELECT COUNT(*) n FROM endpoint").fetchone()["n"]
        superseded = conn.execute(
            "SELECT COUNT(*) n FROM endpoint WHERE superseded = 1").fetchone()["n"]
        visible = conn.execute("SELECT COUNT(*) n FROM v_endpoint_full").fetchone()["n"]
        assert total == 2 and superseded == 1 and visible == 1
    finally:
        conn.close()


def test_partial_sibling_coverage_preserves_interface_row(tmp_path):
    """One sibling overrides (gets its endpoint), the other only inherits the
    default method: the interface row must be kept for the second sibling."""
    conn, summary = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_GEN}/DealsApi.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

public interface DealsApi {
    @GetMapping("/deals/{id}")
    default String getDeal(Long id) { return "default"; }
}
""",
        f"{_SRC}/DealControllerA.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/a")
public class DealControllerA implements DealsApi {
    @Override
    public String getDeal(Long id) { return "deal"; }
}
""",
        f"{_SRC}/DealControllerB.java": """
package ru.bank.api;

import org.springframework.web.bind.annotation.*;

@RestController
public class DealControllerB implements DealsApi {
}
""",
    })
    try:
        got = {(r["controller_fqn"], r["full_path"]) for r in conn.execute(
            "SELECT controller_fqn, full_path FROM v_endpoint_full")}
        assert ("ru.bank.api.DealControllerA", "/a/deals/{id}") in got
        # B produced no replacement -> interface row survives as its representation
        assert ("ru.bank.api.DealsApi", "/deals/{id}") in got
        assert len(got) == 2
    finally:
        conn.close()
