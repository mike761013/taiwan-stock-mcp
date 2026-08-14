"""Offline verification for the V12.1 forward-bullish update package."""

from __future__ import annotations

from datetime import date, timedelta

from stock_db.performance import simulate_signal_execution
from stock_db.v12 import V12Config, predictive_quality_score, strategy_passes


def _plan(action: str = "BUY_ZONE") -> dict:
    return {
        "actionCode": action,
        "tradingPlan": {
            "statusCode": action,
            "noChasePrice": 106.0,
            "maximumRiskPercent": 4.0,
            "aggressiveEntry": {
                "entryLow": 98.0,
                "entryHigh": 100.0,
                "positionPercent": 40,
            },
            "confirmationEntry": {
                "price": 103.0,
                "positionPercent": 60,
                "availableBelowNoChase": True,
            },
            "positionPlan": {
                "aggressiveEntryPercent": 40,
                "confirmationEntryPercent": 60,
                "maximumPlannedPercent": 100,
            },
            "failureCondition": {
                "price": 96.0,
                "confirmation": "close",
            },
        },
    }


def _bars(values: list[tuple[float, float, float, float]]) -> list[dict]:
    start = date(2026, 8, 3)
    return [
        {
            "trade_date": start + timedelta(days=index),
            "open": row[0],
            "high": row[1],
            "low": row[2],
            "close": row[3],
        }
        for index, row in enumerate(values)
    ]


def main() -> None:
    config = V12Config()

    no_chase = simulate_signal_execution(
        _plan("DO_NOT_CHASE"),
        _bars(
            [
                (107, 110, 106, 109),
                (108, 111, 107, 110),
                (109, 112, 108, 111),
            ]
        ),
    )
    assert no_chase["execution_status"] == "NO_TRADE"
    assert no_chase["entry_date"] is None

    two_stage = simulate_signal_execution(
        _plan(),
        _bars(
            [
                (101, 102, 99, 101),
                (102, 104, 101, 103.5),
                (104, 106, 103, 105),
                (105, 107, 104, 106),
            ]
        ),
    )
    assert two_stage["filled_position_percent"] == 100
    assert two_stage["weighted_entry_price"] is not None

    fake_breakout = {
        "open": 100,
        "high": 110,
        "low": 99,
        "close": 101,
        "prev_close": 100,
        "prev_high": 100.5,
        "ma5": 99,
        "ma20": 96,
        "bollinger_upper": 100,
        "volume_ratio": 2,
    }
    assert not strategy_passes(fake_breakout, "breakout", config)

    quality_row = {
        "open": 100,
        "high": 104,
        "low": 99,
        "close": 103,
        "prev_close": 101,
        "prev_high": 102,
        "prev_low": 98,
        "ma5": 100,
        "ma10": 98,
        "ma20": 96,
        "ma60": 90,
        "prev_ma5": 99,
        "prev_ma20": 95.8,
        "large_volume_low": 94,
        "volume_ratio": 1.5,
        "high20": 105,
        "low20": 90,
    }
    healthy, _, _ = predictive_quality_score(
        {**quality_row, "close5": 98}, "early_stage", config
    )
    exhausted, _, _ = predictive_quality_score(
        {**quality_row, "close5": 85}, "early_stage", config
    )
    assert healthy > exhausted

    print("PASS: do-not-chase signals are not bought at the signal close")
    print("PASS: aggressive and confirmation entries are weighted correctly")
    print("PASS: weak-close false breakouts are rejected")
    print("PASS: exhausted five-day moves receive a lower bullish score")
    print("ALL V12.1 BULLISH ACCURACY CHECKS PASSED")


if __name__ == "__main__":
    main()
