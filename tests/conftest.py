"""Shared pytest fixtures: a tiny Spring/JAX-RS repo scanned end-to-end."""

from __future__ import annotations

from pathlib import Path

import pytest

from index.repository import init_db
from scanner.pipeline import build_index

_SRC = "src/main/java/ru/bank/deposit"

_FILES: dict[str, str] = {
    "pom.xml": "<project><groupId>ru.bank</groupId><artifactId>deposit</artifactId><version>1.0</version></project>",
    f"{_SRC}/DepositController.java": """
package ru.bank.deposit;

import org.springframework.web.bind.annotation.*;
import lombok.RequiredArgsConstructor;

@RestController
@RequestMapping("/deposits")
@RequiredArgsConstructor
public class DepositController {
    private final DepositService depositService;

    @PostMapping("/create")
    public Deposit createDeposit(@RequestBody DepositRequest req) {
        return depositService.create(req);
    }

    @GetMapping("/{id}")
    public Deposit get(@PathVariable Long id) {
        return depositService.find(id);
    }
}
""",
    f"{_SRC}/DepositService.java": """
package ru.bank.deposit;

import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;

@Service
public class DepositService {
    @Autowired
    private DepositRepository repo;

    public Deposit create(DepositRequest req) { return repo.save(new Deposit()); }
    public Deposit find(Long id) { return repo.findById(id); }

    @Scheduled(fixedRate = 60000)
    public void sweep() { }
}
""",
    f"{_SRC}/DepositRepository.java": """
package ru.bank.deposit;

import org.springframework.stereotype.Repository;

@Repository
public class DepositRepository {
    public Deposit save(Deposit d) { return d; }
    public Deposit findById(Long id) { return new Deposit(); }
}
""",
    f"{_SRC}/Deposit.java": """
package ru.bank.deposit;

import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "m_deposit")
public class Deposit {
    private Long id;
    private Long amount;
}
""",
    f"{_SRC}/DepositRequest.java": "package ru.bank.deposit;\npublic class DepositRequest { private Long amount; }\n",
}


def write_fixture_repo(root: Path) -> Path:
    for rel, content in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def scan_summary_and_conn(tmp_path):
    """Scan the fixture repo; yield (summary_dict, sqlite_conn)."""
    repo_root = write_fixture_repo(tmp_path / "repo")
    db = repo_root / ".reverse" / "index.sqlite3"
    conn = init_db(db)
    summary = build_index(conn, str(repo_root))
    try:
        yield summary, conn
    finally:
        conn.close()
