"""Precision of the low-confidence layer findings (baseline report / inferred_findings).

Two misleading noise sources, each a risk for a code-writing agent that acts on the
guess (examples are invented, not from any real project):
  B3 — a value ``record`` in a ``*.service`` package is not "possibly a service".
  B4 — an openapi ``*Api`` contract interface whose endpoints were reattributed to a
       concrete controller is not "possibly a controller".
Both fixtures run the real pipeline end-to-end.
"""

from __future__ import annotations

from pathlib import Path

from analysis.layers import compute_low_confidence_findings
from index.repository import init_db
from scanner.pipeline import build_index

_POM = "<project><groupId>com.example</groupId><artifactId>app</artifactId><version>1</version></project>"


def _scan(root: Path, files: dict[str, str]):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    conn = init_db(root / ".reverse" / "index.sqlite3")
    build_index(conn, str(root))
    return conn


def _subjects(findings, needle: str) -> list[str]:
    return [f.subject for f in findings if needle in f.summary]


def test_value_record_in_service_pkg_is_not_flagged_as_service(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        # a value record living in a *.service package
        "src/main/java/com/example/app/service/billing/Money.java":
            "package com.example.app.service.billing;\n"
            "public record Money(String currency, long amount) {}\n",
        # positive control: a plain class in the same package with an unknown role
        # must STILL yield a "possibly a service" finding (no over-suppression)
        "src/main/java/com/example/app/service/billing/PricingEngine.java":
            "package com.example.app.service.billing;\n"
            "public class PricingEngine { public int price() { return 1; } }\n",
    })
    try:
        findings = compute_low_confidence_findings(conn)
        services = _subjects(findings, "Possibly a service")
        assert "com.example.app.service.billing.Money" not in services       # B3: record suppressed
        assert "com.example.app.service.billing.PricingEngine" in services   # control: class kept
    finally:
        conn.close()


def test_data_carrier_keeps_non_component_package_hint(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        # The name is misleading for a record, but the package still provides a
        # valid DTO hint that must not be discarded with the service hint.
        "src/main/java/com/example/app/dto/OrderService.java":
            "package com.example.app.dto;\n"
            "public record OrderService(String id) {}\n",
    })
    try:
        findings = compute_low_confidence_findings(conn)
        dtos = _subjects(findings, "Possibly a dto")
        assert "com.example.app.dto.OrderService" in dtos
        assert "com.example.app.dto.OrderService" not in _subjects(findings, "Possibly a service")
    finally:
        conn.close()


def test_reattributed_api_interface_is_not_flagged_as_controller(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        # openapi-generated contract interface carrying the mapping annotations
        "build/generated/openapi/src/main/java/com/example/app/controller/OrdersApi.java": """
package com.example.app.controller;

import org.springframework.web.bind.annotation.*;

public interface OrdersApi {
    @GetMapping("/orders/{id}")
    String getOrder(Long id);
}
""",
        # concrete controller: reattribution moves the endpoint here, superseding the
        # interface-level row
        "src/main/java/com/example/app/controller/OrderController.java": """
package com.example.app.controller;

import org.springframework.web.bind.annotation.*;

@RestController
public class OrderController implements OrdersApi {
    @Override
    public String getOrder(Long id) { return "order"; }
}
""",
    })
    try:
        # sanity: the interface's endpoints really were reattributed (superseded)
        sup = conn.execute(
            "SELECT COUNT(*) n FROM endpoint e JOIN class c ON c.id = e.controller_class_id "
            "WHERE c.simple_name = 'OrdersApi' AND e.superseded = 1"
        ).fetchone()["n"]
        assert sup == 1

        controllers = _subjects(compute_low_confidence_findings(conn), "Possibly a controller")
        assert "com.example.app.controller.OrdersApi" not in controllers  # B4: resolved contract suppressed
    finally:
        conn.close()
