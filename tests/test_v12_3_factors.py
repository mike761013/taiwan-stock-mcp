import asyncio
from datetime import date

from stock_db import factors
from stock_db.v12 import V12Config


def test_fundamental_acceleration_increases_score():
    fast, _ = factors._fundamental_factor({
        "revenue_month": date(2026, 7, 1),
        "monthly_change_percent": 4,
        "yearly_change_percent": 25,
        "yearly_acceleration_percent": 12,
    })
    slow, _ = factors._fundamental_factor({
        "revenue_month": date(2026, 7, 1),
        "monthly_change_percent": -2,
        "yearly_change_percent": 5,
        "yearly_acceleration_percent": -8,
    })
    assert fast > slow


def test_missing_factor_is_reweighted_not_scored_zero(monkeypatch):
    async def stored(symbols):
        return {}, {}

    async def chip(symbol, trade_date):
        return None, {"reason": "missing"}

    async def intraday(symbol):
        return None, {"reason": "missing"}

    monkeypatch.setattr(factors, "_stored_factors", stored)
    monkeypatch.setattr(factors, "_chip_factor", chip)
    monkeypatch.setattr(factors, "_intraday_factor", intraday)
    factors._REMOTE_CACHE.clear()
    rows = asyncio.run(factors.enrich_candidates_v12_3(
        [{
            "symbol": "2330",
            "bullish_score": 80,
            "historical_execution_adjustment": 0,
            "forwardQualified": True,
            "warnings": [],
        }],
        date(2026, 8, 21),
        {"regime": "NEUTRAL", "industries": {}},
        V12Config(),
    ))
    item = rows[0]
    assert item["factorScores"]["chip"] is None
    assert item["missingFactors"] == ["chip", "fundamental", "theme", "intraday"]
    # (80*30 + 50*5 + 50*5) / 40 = 72.5, not a zero-filled 29.
    assert item["finalScore"] == 72.5
    assert item["dataConfidence"] == 40
