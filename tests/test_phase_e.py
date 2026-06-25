"""Phase E: richer inferred findings + same-class hop in trace + summary seam."""

from __future__ import annotations

import pytest

from analysis.explain import explain_class
from analysis.trace import trace_endpoint
from index.repository import get_class_by_fqn, init_db, insert_finding
from scanner.pipeline import build_index
from summarizer.class_summary import summarize_class

_SRC = "src/main/java/shop/pay"

_FILES: dict[str, str] = {
    "pom.xml": "<project><groupId>shop</groupId><artifactId>pay</artifactId><version>1.0</version></project>",
    f"{_SRC}/PayController.java": """
package shop.pay;

import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/pay")
@RequiredArgsConstructor
public class PayController {
    private final PayService payService;

    @PostMapping("/charge")
    public Receipt charge(@RequestBody ChargeRequest req) {
        return doCharge(req);          // delegates to a same-class helper
    }

    private Receipt doCharge(ChargeRequest req) {
        return payService.charge(req); // helper makes the service call
    }

    @GetMapping("/{id}")
    public Receipt get(@PathVariable Long id) {
        return payService.find(id);
    }
}
""",
    f"{_SRC}/PayService.java": """
package shop.pay;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class PayService {
    private final PayRepository repo;

    public PayService(PayRepository repo) {   // hand-written constructor injection
        this.repo = repo;
    }

    @Transactional
    public Receipt charge(ChargeRequest req) { return repo.save(new Receipt()); }

    public Receipt find(Long id) { return repo.findById(id); }
}
""",
    f"{_SRC}/PayRepository.java": """
package shop.pay;

import org.springframework.stereotype.Repository;

@Repository
public class PayRepository {
    public Receipt save(Receipt r) { return r; }
    public Receipt findById(Long id) { return new Receipt(); }
}
""",
    f"{_SRC}/Receipt.java": "package shop.pay;\npublic class Receipt { private Long id; }\n",
    f"{_SRC}/ChargeRequest.java": "package shop.pay;\npublic class ChargeRequest { private Long amount; }\n",
}


@pytest.fixture
def conn(tmp_path):
    root = tmp_path / "repo"
    for rel, content in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    c = init_db(root / ".reverse" / "index.sqlite3")
    build_index(c, str(root))
    try:
        yield c
    finally:
        c.close()


def test_trace_follows_same_class_helper_hop(conn):
    res = trace_endpoint(conn, http_method="POST", path_contains="/pay/charge")
    kinds = [s["kind"] for s in res["trace"]]
    symbols = [s["symbol"] for s in res["trace"]]
    # the handler does not call the service directly; trace must hop through doCharge()
    assert "controller_helper" in kinds
    assert any("PayService#charge" in s for s in symbols)
    assert any("PayRepository" in s for s in symbols)
    # every hop is a syntactic call -> the whole trace is high confidence
    assert res["confidence"] == "high"


def test_explain_reports_transaction_boundary(conn):
    res = explain_class(conn, "PayService")
    tx = [f for f in res["inferred_findings"] if f["finding_type"] == "transaction_boundary"]
    assert tx, "expected a transaction_boundary finding for the @Transactional service"
    assert tx[0]["confidence"] == "high"
    assert tx[0]["evidence"], "finding must carry evidence"


def test_explain_describes_endpoint_purpose(conn):
    res = explain_class(conn, "PayController")
    eps = {e["path"]: e for e in res["related_symbols"]["endpoints"]}
    assert "creates" in eps["/pay/charge"]["purpose"]
    assert "reads" in eps["/pay/{id}"]["purpose"]
    purposes = [f for f in res["inferred_findings"] if f["finding_type"] == "endpoint_purpose"]
    assert len(purposes) == 2


def test_explain_surfaces_structural_findings(conn):
    cls = get_class_by_fqn(conn, "shop.pay.PayController")
    insert_finding(
        conn, kind="large_controller", description="Controller has too many endpoints",
        severity="warning", class_id=cls["id"],
    )
    res = explain_class(conn, "PayController")
    structural = [f for f in res["inferred_findings"] if f["finding_type"] == "large_controller"]
    assert structural and structural[0]["evidence"]


def test_explain_renders_self_call_cleanly(conn):
    res = explain_class(conn, "PayController")
    self_calls = [c for c in res["related_symbols"]["called_methods"] if c["via_field"] is None]
    assert any(c["symbol"] == "PayController#doCharge" for c in self_calls)
    assert all("(same class)" in c["evidence"][0]["description"] for c in self_calls)
    # no "None." leaks into evidence text
    assert all("None." not in c["evidence"][0]["description"] for c in self_calls)


def test_summarize_class_seam_returns_text(conn):
    cls = get_class_by_fqn(conn, "shop.pay.PayController")
    summary = summarize_class(conn, cls["id"])
    assert summary and "PayController" in summary
