import asyncio
from datetime import date

import pytest

from stock_db import factors
from stock_db.v12 import V12Config


def test_official_monthly_revenue_exact_columns_are_parsed():
    row = factors._parse_official_revenue_row({
        "資料年月": "11507",
        "公司代號": "1101",
        "公司名稱": "台泥",
        "產業別": "水泥工業",
        "營業收入-當月營收": "13,744,103",
        "營業收入-上月比較增減(%)": "2.700477",
        "營業收入-去年同月增減(%)": "1.537936",
    }, "TWSE")

    assert row is not None
    assert row["symbol"] == "1101"
    assert row["month"] == date(2026, 7, 1)
    assert row["revenue"] == 13_744_103
    assert row["mom"] == pytest.approx(2.700477)
    assert row["yoy"] == pytest.approx(1.537936)
    assert row["industry"] == "水泥工業"


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


def test_fundamental_uses_yoy_and_mom_without_fake_acceleration():
    score, details = factors._fundamental_factor({
        "revenue_month": date(2026, 7, 1),
        "monthly_change_percent": 8,
        "yearly_change_percent": 20,
        "yearly_acceleration_percent": None,
    })

    assert score is not None
    assert score > 50
    assert details["revenueYoYPercent"] == 20
    assert details["revenueMoMPercent"] == 8
    assert details["yoyAccelerationPercent"] is None
    assert details["accelerationAvailable"] is False


def test_intraday_null_prices_are_missing_instead_of_neutral(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "date": "2026-08-21",
            "lastPrice": None,
            "openPrice": None,
            "highPrice": None,
            "lowPrice": None,
        }

    monkeypatch.setenv("FUGLE_API_KEY", "test-key")
    monkeypatch.setattr(factors, "_json_get", fake_get)
    score, details = asyncio.run(
        factors._intraday_factor("2330", date(2026, 8, 21))
    )

    assert score is None
    assert "不計分" in details["reason"]


def test_intraday_uses_official_outer_minus_inner_direction(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "date": "2026-08-21",
            "referencePrice": 100,
            "lastPrice": 104,
            "openPrice": 101,
            "highPrice": 105,
            "lowPrice": 100,
            "changePercent": 4,
            "total": {
                "tradeVolumeAtBid": 300,
                "tradeVolumeAtAsk": 700,
            },
        }

    monkeypatch.setenv("FUGLE_API_KEY", "test-key")
    monkeypatch.setattr(factors, "_json_get", fake_get)
    score, details = asyncio.run(
        factors._intraday_factor("2330", date(2026, 8, 21))
    )

    assert score is not None
    assert details["innerVolume"] == 300
    assert details["outerVolume"] == 700
    assert details["bidAskImbalance"] == pytest.approx(0.4)


def test_intraday_rejects_quote_from_wrong_trading_day(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "date": "2026-08-20",
            "lastPrice": 104,
            "openPrice": 101,
            "highPrice": 105,
            "lowPrice": 100,
        }

    monkeypatch.setenv("FUGLE_API_KEY", "test-key")
    monkeypatch.setattr(factors, "_json_get", fake_get)
    score, details = asyncio.run(
        factors._intraday_factor("2330", date(2026, 8, 21))
    )

    assert score is None
    assert details["quoteDate"] == "2026-08-20"


def test_missing_factor_is_reweighted_not_scored_zero(monkeypatch):
    async def stored(symbols):
        return {}, {}

    async def chip(symbol, trade_date):
        return None, {"reason": "missing"}

    async def intraday(symbol, trade_date):
        return None, {"reason": "missing"}

    async def theme_market():
        return {}

    monkeypatch.setattr(factors, "_stored_factors", stored)
    monkeypatch.setattr(factors, "_chip_factor", chip)
    monkeypatch.setattr(factors, "_intraday_factor", intraday)
    monkeypatch.setattr(factors, "_theme_market_context", theme_market)
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


def test_official_industry_theme_uses_market_heat(monkeypatch):
    async def stored(symbols):
        return {
            "1101": {
                "revenue_month": date(2026, 7, 1),
                "monthly_change_percent": 5,
                "yearly_change_percent": 10,
                "yearly_acceleration_percent": None,
            },
        }, {"1101": ["水泥工業"]}

    async def theme_market():
        return {
            "水泥工業": {
                "memberCount": 8,
                "aboveMA20Percent": 75,
                "advancingPercent": 62.5,
                "activeVolumePercent": 50,
                "averageTechnicalScore": 68,
                "heatScore": 67.1,
            },
        }

    async def chip(symbol, trade_date):
        return 60, {}

    async def intraday(symbol, trade_date):
        return 55, {}

    monkeypatch.setattr(factors, "_stored_factors", stored)
    monkeypatch.setattr(factors, "_theme_market_context", theme_market)
    monkeypatch.setattr(factors, "_chip_factor", chip)
    monkeypatch.setattr(factors, "_intraday_factor", intraday)
    factors._REMOTE_CACHE.clear()
    rows = asyncio.run(factors.enrich_candidates_v12_3(
        [{
            "symbol": "1101",
            "bullish_score": 75,
            "historical_execution_adjustment": 0,
            "forwardQualified": True,
            "warnings": [],
        }],
        date(2026, 8, 21),
        {"regime": "NEUTRAL", "industries": {}},
        V12Config(),
    ))

    assert rows[0]["themeScore"] == 67.1
    assert "theme" not in rows[0]["missingFactors"]
    assert rows[0]["factorFeatures"]["theme"]["themes"] == ["水泥工業"]


def test_final_score_below_formal_threshold_is_watch_only(monkeypatch):
    async def stored(symbols):
        return {}, {}

    async def theme_market():
        return {}

    async def chip(symbol, trade_date):
        return None, {"reason": "missing"}

    async def intraday(symbol, trade_date):
        return None, {"reason": "missing"}

    monkeypatch.setattr(factors, "_stored_factors", stored)
    monkeypatch.setattr(factors, "_theme_market_context", theme_market)
    monkeypatch.setattr(factors, "_chip_factor", chip)
    monkeypatch.setattr(factors, "_intraday_factor", intraday)
    factors._REMOTE_CACHE.clear()
    rows = asyncio.run(factors.enrich_candidates_v12_3(
        [{
            "symbol": "2884",
            "bullish_score": 60,
            "historical_execution_adjustment": 0,
            "forwardQualified": True,
            "forwardQualification": {"failedRules": []},
            "warnings": [],
        }],
        date(2026, 8, 21),
        {"regime": "NEUTRAL", "industries": {}},
        V12Config(),
    ))

    assert rows[0]["finalScore"] == 57.5
    assert rows[0]["forwardQualified"] is False
    assert any(
        "低於正式門檻" in rule
        for rule in rows[0]["forwardQualification"]["failedRules"]
    )
