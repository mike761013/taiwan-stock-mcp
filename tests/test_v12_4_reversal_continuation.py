from stock_db.v12 import (
    V12_ACCURACY_ENGINE,
    V12_VERSION,
    V12Config,
    build_v12_candidate,
    explain_v12_row,
    reversal_continuation_score,
)


def _taiflex_base() -> dict:
    return {
        "symbol": "6278",
        "name": "台表科",
        "market": "TWSE",
        "industry": "電子零組件業",
        "volume": 9_000_000,
        "volume_ma20": 9_100_000,
        "turnover": 1_500_000_000,
        "large_volume_low": 153.0,
        "bollinger_upper": 184.0,
        "technical_score": 75.0,
        "atr14": 10.9,
        "high20": 213.0,
        "low20": 128.0,
        "prev_open": 156.0,
        "prev_high": 171.5,
        "prev_low": 155.0,
        "prev_close": 171.5,
        "prev2_low": 145.0,
        "prev2_close": 156.0,
        "close5": 153.0,
        "close10": 145.0,
        "close20": 201.0,
    }


def taiflex_2026_08_11() -> dict:
    row = _taiflex_base()
    row.update(
        {
            "trade_date": "2026-08-11",
            "open": 168.5,
            "high": 171.0,
            "low": 164.5,
            "close": 167.5,
            "ma5": 165.1,
            "ma10": 153.1,
            "ma20": 158.38,
            "ma60": 191.0,
            "prev_ma5": 161.0,
            "prev_ma10": 150.0,
            "prev_ma20": 158.63,
            "prev_ma60": 192.0,
            "volume_ratio": 0.99,
        }
    )
    return row


def test_version_is_one_public_v12_4_label():
    assert V12_VERSION == "V12.4"
    assert V12_ACCURACY_ENGINE == "V12.4"
    assert not V12_VERSION.startswith("V12.4.")


def test_taiflex_first_healthy_pause_enters_reversal_continuation_bridge():
    row = taiflex_2026_08_11()
    passed, score, reasons, warnings, failed = reversal_continuation_score(
        row, V12Config()
    )
    assert passed is True
    assert score >= 65
    assert failed == []
    assert "黃金三角形成中" in reasons

    candidate = build_v12_candidate(row, "reversal_continuation", V12Config())
    assert candidate is not None
    assert candidate["accuracyEngine"] == "V12.4"
    assert candidate["goldenTriangle"]["emerging"] is True
    assert candidate["actionCode"] in {"BUY_ZONE", "PRICE_CONFIRMATION_REQUIRED"}


def test_taiflex_limit_up_day_is_never_a_buy_signal():
    row = taiflex_2026_08_11()
    row.update(
        {
            "trade_date": "2026-08-26",
            "open": 180.0,
            "high": 199.5,
            "low": 179.0,
            "close": 199.5,
            "prev_close": 181.5,
            "ma5": 185.4,
            "ma10": 178.95,
            "ma20": 167.75,
            "ma60": 180.73,
            "prev_ma5": 180.4,
            "prev_ma10": 176.0,
            "prev_ma20": 164.6,
            "volume_ratio": 2.31,
            "close5": 174.5,
        }
    )
    explanation = explain_v12_row(row, V12Config())
    assert explanation["decisionCode"] == "DO_NOT_CHASE"
    assert explanation["distanceFromMA20Percent"] > 12

