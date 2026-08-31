from stock_db.v12 import (
    V12_ACTIONABLE_STATUS_CODES,
    V12Config,
    build_v12_candidate,
    explain_v12_row,
    screen_v12_rows,
    strategy_passes,
    trend_support_probe_score,
    validate_v12_candidates,
)


def jingjin_2026_08_31() -> dict:
    """Point-in-time snapshot that previously disappeared from every V12 list."""
    return {
        "symbol": "3049",
        "name": "精金",
        "market": "TWSE",
        "industry": "光電業",
        "trade_date": "2026-08-31",
        "open": 13.60,
        "high": 14.30,
        "low": 13.40,
        "close": 13.55,
        "prev_close": 13.50,
        "prev_high": 13.95,
        "prev_low": 13.20,
        "prev2_low": 12.70,
        "volume": 31_545_040,
        "volume_ma20": 11_074_608,
        "volume_ratio": 2.85,
        "turnover": 433_456_937,
        "ma5": 13.32,
        "ma10": 12.34,
        "ma20": 11.57,
        "ma60": 12.31,
        "prev_ma5": 13.15,
        "prev_ma10": 12.03,
        "prev_ma20": 11.39,
        "prev_ma60": 12.28,
        "bollinger_upper": 13.83,
        "bollinger_lower": 9.31,
        "large_volume_low": 13.20,
        "technical_score": 40.0,
        "atr14": 0.7036,
        "close5": 12.70,
        "close10": 11.20,
        "close20": 10.15,
        "high20": 14.30,
        "low20": 10.00,
        "high60": 17.40,
        "low60": 9.21,
        "change_percent": 0.05,
    }


def test_jingjin_enters_probe_without_weakening_strict_pullback():
    row = jingjin_2026_08_31()
    config = V12Config()

    assert strategy_passes(row, "pullback", config) is False
    assert strategy_passes(row, "trend_support_probe", config) is True

    passed, score, reasons, warnings, failed, signals = (
        trend_support_probe_score(row, config)
    )
    assert passed is True
    assert score >= 70
    assert failed == []
    assert "MA5>MA10>MA20短期黃金三角成立" in reasons
    assert "守住滾動大量低點13.2" in reasons
    assert "量能高於健康拉回，只能列試單" in warnings
    assert "低點未再下移" in signals


def test_jingjin_probe_is_reduced_position_and_never_formal_actionable():
    candidate = build_v12_candidate(
        jingjin_2026_08_31(),
        "trend_support_probe",
        V12Config(),
    )

    assert candidate is not None
    assert candidate["actionCode"] == "PROBE_ENTRY"
    assert candidate["actionCode"] not in V12_ACTIONABLE_STATUS_CODES
    assert candidate["forwardQualified"] is False
    assert candidate["rolling_massive_volume_low"] == 13.20
    assert candidate["trendSupportProbe"]["shortBullishAlignment"] is True
    assert candidate["trendSupportProbe"]["fullBullishAlignment"] is False

    plan = candidate["tradingPlan"]
    assert plan["signalDefensePrice"] == 13.20
    assert plan["aggressiveEntry"]["positionPercent"] == 20
    assert plan["confirmationEntry"]["positionPercent"] == 20
    assert plan["positionPlan"]["maximumPlannedPercent"] == 40
    assert validate_v12_candidates([candidate]) == []

    explanation = explain_v12_row(jingjin_2026_08_31(), V12Config())
    assert explanation["decisionCode"] == "PROBE_ENTRY"
    assert "trend_support_probe" in explanation["passedStrategies"]


def test_jingjin_survives_the_real_minimum_score_screen():
    results, rejections = screen_v12_rows(
        rows=[jingjin_2026_08_31()],
        strategy="trend_support_probe",
        minimum_score=45,
        limit=20,
        config=V12Config(),
    )

    assert [candidate["symbol"] for candidate in results] == ["3049"]
    assert results[0]["total_score"] == 68.25
    assert rejections == {
        "liquidityRejected": 0,
        "patternRejected": 0,
        "scoreRejected": 0,
    }


def test_probe_rejects_a_real_break_of_ma5_or_massive_volume_low():
    config = V12Config()
    below_ma5 = jingjin_2026_08_31()
    below_ma5.update({"low": 13.10, "close": 13.25})
    assert strategy_passes(below_ma5, "trend_support_probe", config) is False

    below_massive_low = jingjin_2026_08_31()
    below_massive_low.update({"low": 13.05, "close": 13.15})
    assert strategy_passes(
        below_massive_low,
        "trend_support_probe",
        config,
    ) is False
