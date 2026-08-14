import asyncio
from datetime import date, timedelta

from stock_db import radar
from stock_db.performance import simulate_signal_execution
from stock_db.v12 import (
    V12Config,
    apply_execution_prior,
    predictive_quality_score,
    strategy_passes,
)


def _plan(action="BUY_ZONE"):
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
                "confirmation": "收盤確認",
            },
        },
    }


def _bars(*rows):
    start = date(2026, 8, 3)
    output = []
    for index, values in enumerate(rows):
        open_price, high, low, close = values
        output.append(
            {
                "trade_date": start + timedelta(days=index),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return output


def test_execution_backtest_does_not_buy_do_not_chase_signal():
    result = simulate_signal_execution(
        _plan("DO_NOT_CHASE"),
        _bars(
            (107, 110, 106, 109),
            (108, 111, 107, 110),
            (109, 112, 108, 111),
        ),
    )
    assert result["execution_status"] == "NO_TRADE"
    assert result["entry_date"] is None
    assert result["return_d1"] is None


def test_do_not_chase_can_fill_only_after_a_later_pullback():
    result = simulate_signal_execution(
        _plan("DO_NOT_CHASE"),
        _bars(
            (108, 110, 107, 109),
            (102, 103, 99, 101),
            (102, 104, 101, 103.5),
            (104, 106, 103, 105),
        ),
    )
    assert result["execution_status"] == "FILLED"
    assert result["entry_date"] == date(2026, 8, 4)
    assert result["aggressive_fill_price"] == 100.0


def test_execution_backtest_fills_two_stages_and_uses_weighted_cost():
    result = simulate_signal_execution(
        _plan(),
        _bars(
            (101, 102, 99, 101),
            (102, 104, 101, 103.5),
            (104, 106, 103, 105),
            (105, 107, 104, 106),
            (106, 108, 105, 107),
            (107, 109, 106, 108),
        ),
    )
    assert result["execution_status"] == "FILLED"
    assert result["aggressive_fill_price"] == 100.0
    assert result["confirmation_fill_price"] == 103.0
    assert result["filled_position_percent"] == 100
    assert 101.7 < result["weighted_entry_price"] < 101.9
    assert result["return_d3"] > 0


def test_execution_backtest_cancels_before_fill_on_closing_failure():
    result = simulate_signal_execution(
        _plan(),
        _bars((100, 101, 94, 95)),
    )
    assert result["execution_status"] == "CANCELLED"
    assert result["filled_position_percent"] == 0


def test_execution_backtest_exits_next_open_after_close_failure():
    result = simulate_signal_execution(
        _plan(),
        _bars(
            (101, 102, 99, 101),
            (97, 99, 94, 95),
            (94, 96, 93, 95),
        ),
    )
    assert result["execution_status"] == "EXITED"
    assert result["exit_price"] == 94.0
    assert result["exit_reason"] == "CLOSE_FAILURE"
    assert result["return_d5"] < 0


def test_fake_breakout_is_rejected_when_it_closes_away_from_day_high():
    row = {
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
    assert strategy_passes(row, "breakout", V12Config()) is False


def test_predictive_quality_penalises_exhausted_five_day_move():
    base = {
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
        {**base, "close5": 98}, "early_stage", V12Config()
    )
    exhausted, _, risks = predictive_quality_score(
        {**base, "close5": 85}, "early_stage", V12Config()
    )
    assert healthy > exhausted
    assert any("五日漲幅" in risk for risk in risks)


def test_execution_prior_is_capped_and_keeps_scores_separate():
    candidate = {
        "bullish_score": 80,
        "execution_score": 70,
        "ranking_score": 77.5,
        "total_score": 80,
    }
    updated = apply_execution_prior(
        candidate,
        {"adjustment": 99, "active": True},
        V12Config(execution_prior_max_adjustment=8),
    )
    assert updated["bullish_score"] == 88
    assert updated["execution_score"] == 78
    assert updated["total_score"] == 88
    assert updated["ranking_score"] == 85.5


def test_full_radar_top10_excludes_wait_and_do_not_chase(monkeypatch):
    async def snapshot():
        return [{"symbol": "source"}], 2, date(2026, 8, 14)

    async def priors(**kwargs):
        return {}

    def candidate(symbol, action, bullish, execution):
        return {
            "symbol": symbol,
            "name": symbol,
            "close": 100,
            "bullish_score": bullish,
            "execution_score": execution,
            "ranking_score": bullish * 0.75 + execution * 0.25,
            "total_score": bullish,
            "actionCode": action,
            "warnings": [],
            "liquidity": {"eligible": True},
            "tradingPlan": {
                "statusCode": action,
                "maximumRiskPercent": 4,
            },
        }

    def screen(*, strategy, **kwargs):
        if strategy == "early_stage":
            return [candidate("WATCH", "DO_NOT_CHASE", 98, 70)], {}
        if strategy == "pullback":
            return [candidate("BUY", "BUY_ZONE", 85, 93)], {}
        return [], {}

    monkeypatch.setattr(radar, "_fetch_v12_snapshot", snapshot)
    monkeypatch.setattr(radar, "execution_strategy_priors", priors)
    monkeypatch.setattr(radar, "screen_v12_rows", screen)

    result = asyncio.run(
        radar.run_full_bullish_radar_v12(
            limit_each=10,
            minimum_score=45,
            save_result=False,
        )
    )
    assert [item["symbol"] for item in result["top10"]] == ["BUY"]
    assert [item["symbol"] for item in result["watchlistCandidates"]] == [
        "WATCH"
    ]
    assert result["bullishTop10"][0]["symbol"] == "WATCH"
