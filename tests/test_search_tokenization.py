"""FTS tokenization: Cyrillic query terms and CamelCase subword matching.

Regression tests for two silent-recall bugs:
- the query tokenizer used to be ASCII-only, so a Russian topic query
  (the whole point of ru descriptions) produced an empty MATCH and zero results;
- identifiers are single FTS tokens, so mid-name terms (`request` in
  `DepositRequest`) never matched until subword expansion at index time.
"""

from __future__ import annotations

from index import queries
from index.search import _to_match_query, _with_subwords, build_search_index, search


# ------------------------------------------------------------
# unit: query tokenizer
# ------------------------------------------------------------

def test_match_query_keeps_cyrillic_terms():
    assert _to_match_query("банкротство") == '"банкротство"*'


def test_match_query_mixed_language_keeps_both():
    q = _to_match_query("открытие вклада deposit")
    assert '"открытие"*' in q and '"вклада"*' in q and '"deposit"*' in q


def test_match_query_expands_camelcase_to_subword_phrase():
    q = _to_match_query("DepositAccount")
    assert '"DepositAccount"*' in q and '"Deposit Account"*' in q


def test_empty_query_still_yields_valid_match():
    assert _to_match_query("???") == '""'


# ------------------------------------------------------------
# unit: index-time subword expansion
# ------------------------------------------------------------

def test_with_subwords_splits_camelcase():
    assert _with_subwords("DepositAccountService") == (
        "DepositAccountService Deposit Account Service"
    )
    assert _with_subwords("HTTPClientFactory") == "HTTPClientFactory HTTP Client Factory"


def test_with_subwords_leaves_single_words_alone():
    assert _with_subwords("Deposit") == "Deposit"
    assert _with_subwords("банкротство") == "банкротство"


# ------------------------------------------------------------
# integration: over the scanned fixture repo
# ------------------------------------------------------------

def test_search_index_rows_carry_subwords(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    row = conn.execute(
        "SELECT name FROM search_index WHERE entity_type = 'class' AND name LIKE 'DepositRequest%'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "DepositRequest Deposit Request"


def test_midword_term_finds_class(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    hits = search(conn, "request", entity_type="class")
    assert any(h["fqn"] == "ru.bank.deposit.DepositRequest" for h in hits)


def test_find_feature_answers_russian_topic(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    # simulate a described index: ru description on a class (describe/import would write it)
    conn.execute(
        "UPDATE class SET summary = ? WHERE simple_name = 'DepositService'",
        ("Сервис открытия вкладов; проверяет клиента на банкротство перед открытием.",),
    )
    conn.commit()
    build_search_index(conn)

    res = queries.find_feature(conn, "банкротство", limit=10)
    assert res["count"] >= 1
    assert any(c["name"] == "DepositService" for c in res["classes"])


def test_find_code_areas_russian_query(scan_summary_and_conn):
    _summary, conn = scan_summary_and_conn
    conn.execute(
        "UPDATE class SET summary = ? WHERE simple_name = 'DepositService'",
        ("Сервис открытия вкладов; проверяет клиента на банкротство перед открытием.",),
    )
    conn.commit()
    build_search_index(conn)

    res = queries.find_code_areas(conn, "банкротство", limit=10)
    assert any(c["simple_name"] == "DepositService" for c in res["classes"])
