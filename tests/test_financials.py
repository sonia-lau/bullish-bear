'''Fact table, number checking and peers.'''

from datetime import timedelta

import pytest

import db
import financials
from conftest import METRICS, QUOTE


def test_build_facts_order_ids_and_fallback_keys():
    facts = financials.build_facts({"peBasicExclExtraTTM": 28.0, "beta": 1.2, "psTTM": "n/a"}, QUOTE)
    assert [f["label"] for f in facts] == ["Share price", "Price change today", "P/E ratio", "Beta"]
    assert [f["id"] for f in facts] == ["F1", "F2", "F3", "F4"]
    assert facts[2]["value"] == 28.0


def test_build_facts_without_quote():
    facts = financials.build_facts(METRICS, {})
    assert facts[0]["label"] == "P/E ratio" and facts[0]["id"] == "F1"


@pytest.mark.parametrize("fact, text", [
    ({"value": 3500000.0, "unit": "$M"}, "$3.50T"),
    ({"value": 2500.0, "unit": "$M"}, "$2.5B"),
    ({"value": 230.5, "unit": "$"}, "$230.50"),
    ({"value": -1.234, "unit": "%"}, "-1.2%"),
    ({"value": 30, "unit": "x"}, "30.00x"),
    ({"value": 1.2, "unit": ""}, "1.20"),
])
def test_format_value(fact, text):
    assert financials.format_value(fact) == text


def test_format_facts():
    facts = financials.build_facts({"peTTM": 32.456}, {})
    assert financials.format_facts(facts) == "F1: P/E ratio (TTM) = 32.46x"


@pytest.mark.parametrize("text, numbers", [
    ("P/E of 32 [F3]", [32.0]),
    ("market cap of $3.5 trillion", [3.5e12]),
    ("revenue fell in Q3 2025", []),
    ("growth of 2024% is odd", [2024.0]),
    ("1,234.5 units", [1234.5]),
])
def test_extract_numbers(text, numbers):
    assert financials.extract_numbers(text) == numbers


FACTS = financials.build_facts(METRICS, QUOTE)
F = {f["label"]: f["id"] for f in FACTS}


@pytest.mark.parametrize("text, label, expected", [
    ("P/E of 30 is high", "P/E ratio", "verified"),
    ("P/E of 30.4", "P/E ratio", "mismatch"),        # 30.4 isn't 30.0 at the precision written
    ("Apple has a 30x P/E", "P/E ratio", "verified"),  # "a 30" isn't read as a citation
    ("Apple's 24.3% net margin", "Net margin", "verified"),
    ("a 24% net margin", "Net margin", "verified"),
    ("Q3 24.3% net margin", "Net margin", "verified"),
    ("P/E of 29-31x", "P/E ratio", "verified"),
    ("P/E of 34-36x", "P/E ratio", "mismatch"),
    ("Revenue growth was \u22121.2%", "Revenue growth", "verified"),
    ("Debt/equity of 2x", "Debt/equity", "verified"),      # 1.5 rounds to 2
    ("Debt/equity of 1.9x", "Debt/equity", "mismatch"),    # small ratios aren't given a flat ±0.5
    ("52-week low of $164.08 in 2025", "52-week low", "verified"),
    ("P/E is 45", "P/E ratio", "mismatch"),
    ("Valuation looks stretched", "P/E ratio", None),
    ("Revenue shrank 1.2%", "Revenue growth", "verified"),
    ("Revenue grew 1.2%", "Revenue growth", "mismatch"),
    ("Revenue growth of -1.2%", "Revenue growth", "verified"),
    ("Market cap is $3.5 trillion", "Market cap", "verified"),
    ("Market cap is $2 trillion", "Market cap", "mismatch"),
    ("Shares trade at $230", "Share price", "verified"),
])
def test_check_numbers(text, label, expected):
    assert financials.check_numbers(text, [F[label]], FACTS) == expected


def test_dollar_amount_that_looks_like_a_year():
    facts = [{"id": "F1", "label": "52-week high", "value": 2050.12, "unit": "$", "period": "52 weeks"}]
    assert financials.check_numbers("52-week high of $2050.12", ["F1"], facts) == "verified"


@pytest.mark.parametrize("text, expected", [("P/E of 30 and debt/equity of 1.5x", "verified"),
                                            ("P/E of 30 and debt/equity of 9x", "mismatch")])
def test_check_numbers_every_cited_fact_must_match(text, expected):
    assert financials.check_numbers(text, [F["P/E ratio"], F["Debt/equity"]], FACTS) == expected


def test_check_numbers_unknown_fact_id():
    assert financials.check_numbers("P/E of 30", ["F99"], FACTS) is None


def test_get_facts_is_cached(world):
    first = financials.get_facts("AAPL")
    world.metrics["peTTM"] = 99.0
    assert financials.get_facts("AAPL") == first
    assert world.counts["metrics"] == 1


def test_get_facts_refreshes_when_stale(world):
    financials.get_facts("AAPL")
    db.financials.update_one({"ticker": "AAPL"},
                             {"$set": {"fetchedAt": db.now() - timedelta(hours=financials.FACTS_HOURS + 1)}})
    world.metrics["peTTM"] = 99.0
    facts = financials.get_facts("AAPL")
    assert next(f for f in facts if f["label"] == "P/E ratio")["value"] == 99.0


def test_get_facts_outage_is_not_fatal(monkeypatch):
    def boom(*a):
        raise RuntimeError("finnhub 429")
    monkeypatch.setattr(financials, "get_company_basic_financials", boom)
    assert financials.get_facts("AAPL") == []
    assert db.financials.count_documents({}) == 0     # failure isn't cached


def test_get_facts_outage_uses_stale_table(world, monkeypatch):
    first = financials.get_facts("AAPL")
    db.financials.update_one({}, {"$set": {"fetchedAt": db.now() - timedelta(days=3)}})
    monkeypatch.setattr(financials, "get_quote", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert financials.get_facts("AAPL") == first


def test_get_peers_excludes_self_and_caches(world):
    assert financials.get_peers("AAPL") == ["MSFT", "GOOGL"]
    world.peers = ["AAPL", "XYZ"]
    assert financials.get_peers("AAPL") == ["MSFT", "GOOGL"]
    assert world.counts["peers"] == 1


def test_get_peers_capped(world):
    world.peers = [f"P{i}" for i in range(15)]
    assert len(financials.get_peers("AAPL")) == financials.MAX_PEERS


def test_get_peers_outage_uses_stale_list(world, monkeypatch):
    financials.get_peers("AAPL")
    db.companies.update_one({}, {"$set": {"fetchedAt": db.now() - timedelta(days=financials.PEERS_DAYS + 1)}})
    monkeypatch.setattr(financials, "get_company_peers", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    assert financials.get_peers("AAPL") == ["MSFT", "GOOGL"]


def test_get_peers_failure_is_not_fatal(monkeypatch):
    def boom(ticker):
        raise RuntimeError("finnhub down")
    monkeypatch.setattr(financials, "get_company_peers", boom)
    assert financials.get_peers("AAPL") == []
    assert db.companies.count_documents({}) == 0   # failure isn't cached
