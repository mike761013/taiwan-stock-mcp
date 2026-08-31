import asyncio

from stock_db import radar
from stock_db.v12 import (
    V12Config,
    build_trading_plan,
    build_v12_candidate,
    liquidity_result,
    split_v12_price_tiers,
    validate_v12_candidates,
)


def asia_electronic_2026_07_15():
    return {
        "symbol": "4939",
        "name": "亞電",
        "market": "OTC",
        "trade_date": "2026-07-15",
        "open": 56.9,
        "high": 60.8,
        "low": 56.2,
        "close": 60.8,
        "volume": 3_703_634,
        "turnover": 219_394_545,
        "prev_open": 57.0,
        "prev_high": 59.2,
        "prev_low": 53.2,
        "prev_close": 55.3,
        "ma5": 57.50,
        "ma10": 59.47,
        "ma20": 61.06,
        "ma60": 55.0,
        "volume_ma20": 6_500_000,
        "volume_ratio": 0.57,
        "bollinger_upper": 68.0,
        "large_volume_low": 53.2,
        "high20": 72.0,
        "low20": 53.2,
        "prev2_low": 52.8,
        "technical_score": 70,
        "atr14": 4.34,
    }


def test_reversal_reclaim_records_asia_electronic_without_chasing():
    row = asia_electronic_2026_07_15()
    row["change_percent"] = 5.5  # Official feeds store the price change amount.
    candidate = build_v12_candidate(
        row,
        "reversal_reclaim",
        V12Config(),
    )
    assert candidate is not None
    assert candidate["strategy"] == "reversal_reclaim"
    assert candidate["total_score"] >= 80
    assert candidate["action"] == "不追價"
    assert candidate["actionCode"] == "DO_NOT_CHASE"
    assert candidate["tradingPlan"]["signalPrice"] == 60.8
    assert candidate["tradingPlan"]["signalDefensePrice"] == 56.2
    assert candidate["tradingPlan"]["hardStopPrice"] < 56.2
    assert candidate["tradingPlan"]["maximumBuyPrice"] < 63.0
    assert candidate["tradingPlan"]["noChasePrice"] < 65.0
    assert candidate["change_amount"] == 5.5
    assert candidate["change_percent"] == 9.9458
    assert candidate["dailyChangePercent"] == 9.95
    assert validate_v12_candidates([candidate]) == []


def test_v7_liquidity_gate_rejects_thin_stock():
    row = asia_electronic_2026_07_15()
    row.update({"volume": 36_000, "volume_ma20": 30_000, "turnover": 571_250})
    result = liquidity_result(row, V12Config())
    assert result["eligible"] is False
    assert len(result["failedRules"]) == 3


def test_ta_chia_2026_07_22_is_rejected_by_tighter_liquidity_rules():
    row = asia_electronic_2026_07_15()
    row.update(
        {
            "symbol": "2221",
            "name": "大甲",
            "close": 51.6,
            "volume": 1_627_943,
            "volume_ma20": 770_000,
            "turnover": 84_113_214,
        }
    )
    result = liquidity_result(row, V12Config())
    assert result["eligible"] is False
    assert result["dailyVolumeLots"] == 1627.94
    assert result["averageVolume20Lots"] == 770.0
    assert len(result["failedRules"]) == 3


def test_price_tiers_keep_under_200_main_and_require_strict_high_price_quality():
    candidates = [
        {
            "symbol": "1001",
            "close": 200,
            "total_score": 70,
            "actionCode": "WAIT_PULLBACK",
            "warnings": ["等待拉回"],
            "liquidity": {"eligible": True},
            "tradingPlan": {
                "statusCode": "WAIT_PULLBACK",
                "maximumRiskPercent": 8,
            },
        },
        {
            "symbol": "2001",
            "close": 250,
            "total_score": 90,
            "actionCode": "BUY_ZONE",
            "warnings": [],
            "liquidity": {"eligible": True},
            "tradingPlan": {
                "statusCode": "BUY_ZONE",
                "maximumRiskPercent": 5,
            },
        },
        {
            "symbol": "2002",
            "close": 300,
            "total_score": 84,
            "actionCode": "BUY_ZONE",
            "warnings": [],
            "liquidity": {"eligible": True},
            "tradingPlan": {
                "statusCode": "BUY_ZONE",
                "maximumRiskPercent": 5,
            },
        },
        {
            "symbol": "2003",
            "close": 400,
            "total_score": 95,
            "actionCode": "WAIT_PULLBACK",
            "warnings": [],
            "liquidity": {"eligible": True},
            "tradingPlan": {
                "statusCode": "WAIT_PULLBACK",
                "maximumRiskPercent": 4,
            },
        },
    ]
    tiers = split_v12_price_tiers(candidates, V12Config())
    assert [item["symbol"] for item in tiers["main"]] == ["1001"]
    assert [item["symbol"] for item in tiers["highPrice"]] == ["2001"]
    assert [item["symbol"] for item in tiers["rejectedHighPrice"]] == [
        "2002",
        "2003",
    ]
    assert tiers["highPrice"][0]["priceTierLabel"] == "200元以上強勢例外"


def test_pullback_plan_normalises_reversed_entry_bounds_and_waits_above_maximum():
    row = {
        "trade_date": "2026-07-22",
        "low": 41.55,
        "close": 41.9,
        "ma5": 41.25,
        "ma20": 41.25,
        "atr14": 1.1428571428571429,
    }
    plan = build_trading_plan(row, "pullback", V12Config())
    assert plan["idealEntryLow"] <= plan["idealEntryHigh"]
    assert plan["idealEntryHigh"] <= plan["maximumBuyPrice"]
    assert plan["maximumBuyPrice"] <= plan["noChasePrice"]
    assert plan["signalPrice"] > plan["maximumBuyPrice"]
    assert plan["status"] == "等待拉回"
    assert plan["statusCode"] == "WAIT_PULLBACK"
    assert plan["initialPositionPercent"] == 0


def test_pullback_plan_keeps_buy_zone_when_signal_is_inside_normalised_range():
    row = {
        "trade_date": "2026-07-22",
        "low": 104.0,
        "close": 104.0,
        "ma5": 101.745,
        "ma20": 101.745,
        "atr14": 3.3142857142857143,
    }
    plan = build_trading_plan(row, "pullback", V12Config())
    assert plan["idealEntryLow"] == 102.5
    assert plan["idealEntryHigh"] == 104.0
    assert plan["maximumBuyPrice"] == 104.5
    assert plan["status"] == "買進區"
    assert plan["statusCode"] == "BUY_ZONE"


def test_dual_entry_plan_exposes_aggressive_confirmed_split_and_failure():
    row = {
        "trade_date": "2026-08-12",
        "open": 85.4,
        "high": 85.4,
        "low": 82.9,
        "close": 83.8,
        "prev_high": 86.5,
        "ma5": 85.98,
        "ma20": 77.42,
        "large_volume_low": 78.0,
        "atr14": 3.5,
    }
    plan = build_trading_plan(row, "pullback", V12Config())

    assert plan["aggressiveEntry"]["entryLow"] == 82.9
    assert plan["aggressiveEntry"]["entryHigh"] == 83.2
    assert plan["aggressiveEntry"]["positionPercent"] == 40
    assert plan["confirmationEntry"]["price"] == 85.3
    assert plan["confirmationEntry"]["positionPercent"] == 60
    assert plan["confirmationEntry"]["availableBelowNoChase"] is True
    assert plan["positionPlan"] == {
        "aggressiveEntryPercent": 40,
        "confirmationEntryPercent": 60,
        "maximumPlannedPercent": 100,
        "description": "激進低接先買40%，確認後再買60%",
    }
    assert plan["failureCondition"]["price"] == 82.9
    assert plan["failureCondition"]["confirmation"] == "收盤確認"


def test_reversal_watch_disables_aggressive_entry_until_confirmation():
    row = asia_electronic_2026_07_15()
    row.update({"close": 58.0, "prev_close": 57.0, "ma20": 61.0})
    plan = build_trading_plan(row, "reversal_reclaim", V12Config())

    assert plan["statusCode"] == "BOTTOM_REVERSAL_WATCH"
    assert plan["aggressiveEntry"]["positionPercent"] == 0
    assert plan["confirmationEntry"]["positionPercent"] == 30
    assert plan["positionPlan"]["maximumPlannedPercent"] == 30


def test_semantic_validator_rejects_invalid_v12_output():
    bad_candidate = {
        "symbol": "5515",
        "close": 41.9,
        "prev_close": 44.25,
        "change_percent": -2.35,
        "tradingPlan": {
            "status": "BUY_ZONE",
            "signalPrice": 41.9,
            "idealEntryLow": 41.55,
            "idealEntryHigh": 41.5,
            "maximumBuyPrice": 41.7,
            "noChasePrice": 42.15,
        },
    }
    codes = {
        issue["code"] for issue in validate_v12_candidates([bad_candidate])
    }
    assert "CHANGE_PERCENT_MISMATCH" in codes
    assert "INVALID_PRICE_BAND_ORDER" in codes
    assert "TRADABLE_STATUS_ABOVE_MAXIMUM_BUY" in codes


def test_full_v12_radar_initialises_high_price_rejection_tracking(
    monkeypatch,
):
    async def empty_snapshot():
        return [], 0, None

    monkeypatch.setattr(radar, "_fetch_v12_snapshot", empty_snapshot)

    result = asyncio.run(
        radar.run_full_bullish_radar_v12(
            limit_each=10,
            minimum_score=45,
            save_result=False,
        )
    )

    assert result["ok"] is True
    assert result["candidateCount"] == 0
    assert result["excludedHighPriceCount"] == 0


def test_formal_actionable_candidate_beats_higher_scoring_probe_duplicate():
    formal = {
        "symbol": "3049",
        "strategy": "pullback",
        "actionCode": "BUY_ZONE",
        "forwardQualified": True,
        "ranking_score": 60,
    }
    probe = {
        "symbol": "3049",
        "strategy": "trend_support_probe",
        "actionCode": "PROBE_ENTRY",
        "forwardQualified": False,
        "ranking_score": 90,
    }

    assert radar._candidate_bucket(formal) > radar._candidate_bucket(probe)
    assert radar._is_formal_actionable(formal) is True
    assert radar._is_probe_candidate(probe) is True
