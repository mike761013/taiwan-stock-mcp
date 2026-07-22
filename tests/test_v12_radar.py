from stock_db.v12 import V12Config, build_v12_candidate, liquidity_result


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
        "technical_score": 70,
        "atr14": 4.34,
    }


def test_reversal_reclaim_finds_asia_electronic_one_day_earlier():
    candidate = build_v12_candidate(
        asia_electronic_2026_07_15(),
        "reversal_reclaim",
        V12Config(),
    )
    assert candidate is not None
    assert candidate["strategy"] == "reversal_reclaim"
    assert candidate["total_score"] >= 80
    assert candidate["action"] == "EARLY_ENTRY_SMALL_POSITION"
    assert candidate["tradingPlan"]["signalPrice"] == 60.8
    assert candidate["tradingPlan"]["signalDefensePrice"] == 56.2
    assert candidate["tradingPlan"]["hardStopPrice"] < 56.2
    assert candidate["tradingPlan"]["maximumBuyPrice"] < 63.0
    assert candidate["tradingPlan"]["noChasePrice"] < 65.0


def test_v7_liquidity_gate_rejects_thin_stock():
    row = asia_electronic_2026_07_15()
    row.update({"volume": 36_000, "volume_ma20": 30_000, "turnover": 571_250})
    result = liquidity_result(row, V12Config())
    assert result["eligible"] is False
    assert len(result["failedRules"]) == 3
