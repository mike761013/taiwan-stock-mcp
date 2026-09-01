"""Offline-safe V12.4 verification; no database or provider calls."""

from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_v12_module():
    path = ROOT / "stock_db" / "v12.py"
    spec = importlib.util.spec_from_file_location("v12_4_verify_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load stock_db/v12.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    files = (
        "stock_db/v12.py",
        "stock_db/radar.py",
        "stock_db/factors.py",
        "stock_db/advanced_factors.py",
        "stock_db/performance.py",
        "server_v10_tools.py",
    )
    for relative in files:
        py_compile.compile(str(ROOT / relative), doraise=True)

    config = json.loads((ROOT / "v12_config.json").read_text(encoding="utf-8"))
    required = {
        "continuation_max_5day_change_pct",
        "continuation_max_distance_support_pct",
        "golden_triangle_compression_pct",
        "near_miss_max_failed_rules",
        "probe_max_distance_ma5_pct",
        "probe_max_position_percent",
        "forward_min_quality_probe",
        "factor_weight_event",
        "factor_weight_cross_market",
        "factor_weight_derivatives",
        "factor_weight_sector_driver",
        "factor_weight_sentiment",
    }
    assert required.issubset(config), "V12.4 config fields are incomplete"
    factor_weight_keys = {
        key for key in config if key.startswith("factor_weight_")
    }
    assert sum(float(config[key]) for key in factor_weight_keys) == 100

    factors_source = (ROOT / "stock_db" / "factors.py").read_text(
        encoding="utf-8"
    )
    performance_source = (ROOT / "stock_db" / "performance.py").read_text(
        encoding="utf-8"
    )
    assert 'V12_4_FACTOR_MODEL = "V12.4-COMPLETE-FACTORS-2"' in factors_source
    assert 'EXECUTION_MODEL_REVISION = "V12.4-NET-EXECUTION-1"' in (
        performance_source
    )
    assert '"1.000399"' in performance_source
    assert '"0.996601"' in performance_source

    v12 = load_v12_module()
    assert v12.V12_VERSION == "V12.4"
    assert v12.V12_ACCURACY_ENGINE == "V12.4"
    assert "reversal_continuation" in v12.V12_STRATEGIES
    assert "trend_support_probe" in v12.V12_STRATEGIES

    row = {
        "symbol": "6278", "name": "台表科", "trade_date": "2026-08-11",
        "open": 168.5, "high": 171.0, "low": 164.5, "close": 167.5,
        "prev_open": 156.0, "prev_high": 171.5, "prev_low": 155.0,
        "prev_close": 171.5, "prev2_low": 145.0,
        "ma5": 165.1, "ma10": 153.1, "ma20": 158.38, "ma60": 191.0,
        "prev_ma5": 161.0, "prev_ma10": 150.0,
        "prev_ma20": 158.63, "prev_ma60": 192.0,
        "volume": 9_000_000, "volume_ma20": 9_100_000,
        "volume_ratio": 0.99, "turnover": 1_500_000_000,
        "large_volume_low": 153.0, "bollinger_upper": 184.0,
        "technical_score": 75.0, "atr14": 10.9,
        "high20": 213.0, "low20": 128.0,
        "close5": 153.0, "close10": 145.0, "close20": 201.0,
    }
    passed, _, reasons, _, failures = v12.reversal_continuation_score(
        row, v12.V12Config()
    )
    assert passed and not failures
    assert "黃金三角形成中" in reasons

    row.update({
        "trade_date": "2026-08-26", "open": 180.0, "high": 199.5,
        "low": 179.0, "close": 199.5, "prev_close": 181.5,
        "ma5": 185.4, "ma10": 178.95, "ma20": 167.75, "ma60": 180.73,
        "prev_ma5": 180.4, "prev_ma10": 176.0, "prev_ma20": 164.6,
        "volume_ratio": 2.31, "close5": 174.5,
    })
    explanation = v12.explain_v12_row(row, v12.V12Config())
    assert explanation["decisionCode"] == "DO_NOT_CHASE"

    jingjin = {
        "symbol": "3049", "name": "精金", "market": "TWSE",
        "trade_date": "2026-08-31", "open": 13.60, "high": 14.30,
        "low": 13.40, "close": 13.55, "prev_close": 13.50,
        "prev_high": 13.95, "prev_low": 13.20, "prev2_low": 12.70,
        "volume": 31_545_040, "volume_ma20": 11_074_608,
        "volume_ratio": 2.85, "turnover": 433_456_937,
        "ma5": 13.32, "ma10": 12.34, "ma20": 11.57, "ma60": 12.31,
        "prev_ma5": 13.15, "prev_ma20": 11.39,
        "bollinger_upper": 13.83, "large_volume_low": 13.20,
        "technical_score": 40.0, "atr14": 0.7036,
        "close5": 12.70, "high20": 14.30, "low20": 10.00,
    }
    candidates, rejections = v12.screen_v12_rows(
        [jingjin], "trend_support_probe", 45, 20, v12.V12Config()
    )
    assert rejections == {
        "liquidityRejected": 0,
        "patternRejected": 0,
        "scoreRejected": 0,
    }
    assert [candidate["symbol"] for candidate in candidates] == ["3049"]
    assert candidates[0]["actionCode"] == "PROBE_ENTRY"
    assert candidates[0]["tradingPlan"]["positionPlan"] == {
        "aggressiveEntryPercent": 20,
        "confirmationEntryPercent": 20,
        "maximumPlannedPercent": 40,
        "description": "激進低接先買20%，確認後再買20%",
    }

    reclaimed = dict(jingjin)
    reclaimed.update({"low": 13.10, "close": 13.55})
    passed, _, reasons, warnings, failures, signals = (
        v12.trend_support_probe_score(reclaimed, v12.V12Config())
    )
    assert passed and not failures
    assert "盤中跌破MA5後收回" in reasons
    assert "收盤收復滾動大量低點13.2" in reasons
    assert "盤中曾跌破MA5，收盤已收回" in warnings
    assert "收盤收復MA5" in signals

    reclaimed_candidate = v12.build_v12_candidate(
        reclaimed,
        "trend_support_probe",
        v12.V12Config(),
    )
    assert reclaimed_candidate is not None
    probe = reclaimed_candidate["trendSupportProbe"]
    assert probe["candleHeldMA5"] is False
    assert probe["closeHeldMA5"] is True
    assert probe["intradayReclaimedMA5"] is True

    close_broken = dict(jingjin)
    close_broken.update({"low": 13.10, "close": 13.25})
    assert not v12.strategy_passes(
        close_broken,
        "trend_support_probe",
        v12.V12Config(),
    )

    print("V12.4 VERIFY PASSED")


if __name__ == "__main__":
    main()
