"""Regression tests for round 2 of the 2026-07-21 full-review fixes — honesty of
the flagship answers: transitive change-impact endpoints, multi-service traces,
ambiguous interface implementations and incomplete-trace warnings. All classes
are invented."""

from __future__ import annotations

from pathlib import Path

from analysis.impact import change_impact
from analysis.trace import trace_endpoint
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


_SRC = "src/main/java/com/example/app"


# --- M9: change_impact reaches endpoints through the layered chain ------------

def test_change_impact_reaches_endpoints_transitively(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/WidgetController.java": """
package com.example.app;
import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;
@RestController
@RequestMapping("/widgets")
@RequiredArgsConstructor
public class WidgetController {
    private final WidgetService widgetService;
    @GetMapping("/{id}")
    public String get(Long id) { return widgetService.load(id); }
}
""",
        f"{_SRC}/WidgetService.java": """
package com.example.app;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
@Service
public class WidgetService {
    @Autowired
    private WidgetRepository repo;
    public String load(Long id) { return repo.find(id); }
}
""",
        f"{_SRC}/WidgetRepository.java": """
package com.example.app;
import org.springframework.stereotype.Repository;
@Repository
public class WidgetRepository {
    public String find(Long id) { return "w"; }
}
""",
    })
    try:
        result = change_impact(conn, "WidgetRepository")
        endpoints = [c for c in result["candidate_impacts"] if c["kind"] == "endpoint"]
        # the regression: 1-hop-only closure returned zero endpoints here
        assert endpoints, "endpoint of a transitively-dependent controller missing"
        ep = endpoints[0]
        assert "transitively" in ep["reason"]
        assert ep["confidence"] == "low"  # longer chain -> weaker candidate signal
    finally:
        conn.close()


# --- M10: several service-like calls -> follow first, but say so --------------

def test_trace_with_multiple_service_calls_warns_and_degrades(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/OrderController.java": """
package com.example.app;
import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;
@RestController
@RequestMapping("/orders")
@RequiredArgsConstructor
public class OrderController {
    private final AuditService auditService;
    private final OrderService orderService;
    @PostMapping("/create")
    public String create() {
        auditService.log();
        return orderService.create();
    }
}
""",
        f"{_SRC}/AuditService.java": """
package com.example.app;
import org.springframework.stereotype.Service;
@Service
public class AuditService {
    public void log() { }
}
""",
        f"{_SRC}/OrderService.java": """
package com.example.app;
import org.springframework.stereotype.Service;
@Service
public class OrderService {
    public String create() { return "ok"; }
}
""",
    })
    try:
        result = trace_endpoint(conn, http_method="POST", path_contains="create")
        svc_steps = [s for s in result["trace"] if s["kind"] == "service_call"]
        assert svc_steps and svc_steps[0]["symbol"] == "AuditService#log"  # first by line
        # the regression: this used to be "high" with zero warnings
        assert svc_steps[0]["confidence"] == "medium"
        assert result["confidence"] != "high"
        assert any("OrderService#create" in w and "Not followed" in w for w in result["warnings"])
    finally:
        conn.close()


# --- M11: arbitrary pick among several implementations is not "high" ----------

def test_trace_ambiguous_impl_degrades_downstream_confidence(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/PayController.java": """
package com.example.app;
import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;
@RestController
@RequestMapping("/pay")
@RequiredArgsConstructor
public class PayController {
    private final PaymentService paymentService;
    @PostMapping("/run")
    public String run() { return paymentService.pay(); }
}
""",
        f"{_SRC}/PaymentService.java": """
package com.example.app;
public interface PaymentService {
    String pay();
}
""",
        f"{_SRC}/LegacyPaymentGateway.java": """
package com.example.app;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
@Service
public class LegacyPaymentGateway implements PaymentService {
    @Autowired
    private PaymentRepository paymentRepository;
    public String pay() { return paymentRepository.save(); }
}
""",
        f"{_SRC}/ModernPaymentGateway.java": """
package com.example.app;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
@Service
public class ModernPaymentGateway implements PaymentService {
    @Autowired
    private PaymentRepository paymentRepository;
    public String pay() { return paymentRepository.save(); }
}
""",
        f"{_SRC}/PaymentRepository.java": """
package com.example.app;
import org.springframework.stereotype.Repository;
@Repository
public class PaymentRepository {
    public String save() { return "ok"; }
}
""",
    })
    try:
        result = trace_endpoint(conn, http_method="POST", path_contains="run")
        downstream = [s for s in result["trace"] if s["kind"] in ("repository_call", "persistence")]
        assert downstream, "expected a repository step derived from the picked impl"
        # the regression: an arbitrary impl pick still produced high/high
        assert all(s["confidence"] != "high" for s in downstream)
        assert result["confidence"] == "medium"
        assert any("implementations" in w for w in result["warnings"])
    finally:
        conn.close()


# --- incomplete trace says it may be incomplete -------------------------------

def test_trace_ending_at_service_warns_incomplete(tmp_path):
    conn = _scan(tmp_path / "repo", {
        "pom.xml": _POM,
        f"{_SRC}/NoticeController.java": """
package com.example.app;
import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;
@RestController
@RequestMapping("/notices")
@RequiredArgsConstructor
public class NoticeController {
    private final NoticeService noticeService;
    @PostMapping("/ping")
    public void ping() { noticeService.ping(); }
}
""",
        f"{_SRC}/NoticeService.java": """
package com.example.app;
import org.springframework.stereotype.Service;
@Service
public class NoticeService {
    public void ping() { }
}
""",
    })
    try:
        result = trace_endpoint(conn, http_method="POST", path_contains="ping")
        kinds = [s["kind"] for s in result["trace"]]
        assert "service_call" in kinds and "repository_call" not in kinds
        assert any("incomplete" in w for w in result["warnings"])
        codes = {lim["code"] for lim in result["limitations"]}
        assert "ctor_injection_without_lombok" in codes
    finally:
        conn.close()
