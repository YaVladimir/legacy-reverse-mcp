"""Phase B: hand-written constructor injection (no Lombok) is detected."""

from __future__ import annotations

from analysis.trace import trace_endpoint
from index.repository import init_db
from scanner.java_parser import parse_source
from scanner.pipeline import build_index

_CONTROLLER = """
package shop;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @PostMapping("/create")
    public String create() {
        return orderService.create();
    }
}
"""


def test_parser_detects_ctor_assigned_field():
    pc = parse_source(_CONTROLLER.encode(), "OrderController.java").classes[0]
    assert "orderService" in pc.ctor_injected_fields


def _build(tmp_path):
    src = tmp_path / "src/main/java/shop"
    src.mkdir(parents=True)
    (src / "OrderController.java").write_text(_CONTROLLER, encoding="utf-8")
    (src / "OrderService.java").write_text(
        "package shop;\nimport org.springframework.stereotype.Service;\n"
        "@Service\npublic class OrderService {\n"
        "    private final OrderRepository repo;\n"
        "    public OrderService(OrderRepository repo) { this.repo = repo; }\n"
        "    public String create() { return repo.save(); }\n}\n",
        encoding="utf-8",
    )
    (src / "OrderRepository.java").write_text(
        "package shop;\nimport org.springframework.stereotype.Repository;\n"
        "@Repository\npublic class OrderRepository { public String save() { return \"x\"; } }\n",
        encoding="utf-8",
    )
    conn = init_db(tmp_path / ".reverse" / "index.sqlite3")
    build_index(conn, str(tmp_path))
    return conn


def test_ctor_injected_field_marked_injected(tmp_path):
    conn = _build(tmp_path)
    try:
        row = conn.execute(
            "SELECT is_injected FROM field WHERE name = 'orderService'"
        ).fetchone()
        assert row["is_injected"] == 1
    finally:
        conn.close()


def test_trace_reaches_service_and_repository_via_ctor_injection(tmp_path):
    conn = _build(tmp_path)
    try:
        res = trace_endpoint(conn, http_method="POST", path_contains="create")
        kinds = [s["kind"] for s in res["trace"]]
        # without ctor-injection detection this stopped at controller_method only
        assert "service_call" in kinds
        symbols = [s["symbol"] for s in res["trace"]]
        assert "OrderService#create" in symbols
        assert "OrderRepository#save" in symbols
    finally:
        conn.close()
