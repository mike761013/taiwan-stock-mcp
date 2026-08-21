import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "stock_db" / "v12.py"
SPEC = importlib.util.spec_from_file_location("v12_forward_test_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
V12 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = V12
SPEC.loader.exec_module(V12)

V12_ACCURACY_ENGINE = V12.V12_ACCURACY_ENGINE
V12Config = V12.V12Config
apply_market_context = V12.apply_market_context
build_market_context = V12.build_market_context
build_v12_candidate = V12.build_v12_candidate
pullback_v2_score = V12.pullback_v2_score
strategy_passes = V12.strategy_passes


class V12ForwardPersistenceTests(unittest.TestCase):
    def test_breakout_quality_gates(self):
        healthy = {
            "open": 101,
            "high": 106,
            "low": 100,
            "close": 104,
            "prev_close": 102,
            "prev_high": 103,
            "ma5": 101,
            "ma20": 96,
            "bollinger_upper": 103,
            "volume_ratio": 2.0,
        }
        self.assertTrue(strategy_passes(healthy, "breakout", V12Config()))
        self.assertFalse(
            strategy_passes(
                {**healthy, "close": 103.5, "high": 107},
                "breakout",
                V12Config(),
            )
        )
        self.assertFalse(
            strategy_passes(
                {**healthy, "volume_ratio": 3.3},
                "breakout",
                V12Config(),
            )
        )

    @staticmethod
    def pullback_row(close=101.0):
        return {
            "open": 102.0,
            "high": 102.0,
            "low": 100.0,
            "close": close,
            "prev_close": 102.0,
            "prev_high": 103.0,
            "prev_low": 99.5,
            "ma5": 100.0,
            "ma10": 99.0,
            "ma20": 97.0,
            "ma60": 90.0,
            "prev_ma5": 99.9,
            "prev_ma20": 96.95,
            "volume_ratio": 0.7,
            "large_volume_low": 94.0,
        }

    def test_down_pullback_requires_stabilisation(self):
        passed, _, reasons, _, _ = pullback_v2_score(
            self.pullback_row(),
            V12Config(),
        )
        self.assertTrue(passed)
        self.assertTrue(any("量縮、低點未下移" in value for value in reasons))

        weak, _, _, warnings, _ = pullback_v2_score(
            self.pullback_row(close=100.2),
            V12Config(),
        )
        self.assertFalse(weak)
        self.assertTrue(any("不先假設是健康整理" in value for value in warnings))

    def test_extended_signal_is_watch_only(self):
        row = {
            "symbol": "1234",
            "name": "測試股",
            "market": "TWSE",
            "industry": "電子",
            "trade_date": "2026-08-20",
            "open": 104.0,
            "high": 106.0,
            "low": 103.5,
            "close": 105.0,
            "prev_close": 103.0,
            "prev_high": 104.0,
            "prev_low": 101.0,
            "ma5": 103.0,
            "ma10": 101.5,
            "ma20": 100.0,
            "ma60": 95.0,
            "prev_ma5": 102.5,
            "prev_ma20": 99.9,
            "volume": 3_000_000,
            "volume_ma20": 2_000_000,
            "volume_ratio": 1.5,
            "turnover": 315_000_000,
            "technical_score": 80.0,
            "large_volume_low": 98.0,
            "close5": 90.0,
            "high20": 106.0,
            "low20": 90.0,
            "atr14": 2.0,
        }
        candidate = build_v12_candidate(row, "early_stage", V12Config())
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["accuracyEngine"], V12_ACCURACY_ENGINE)
        self.assertFalse(candidate["forwardQualified"])
        self.assertEqual(
            candidate["tradingPlan"]["failureCondition"]["confirmation"],
            "收盤確認",
        )

    def test_weak_market_context_is_conservative(self):
        rows = [
            {
                "symbol": f"10{index:02d}",
                "industry": "測試產業",
                "close": 90.0,
                "ma5": 92.0,
                "ma20": 100.0,
                "close5": 91.0,
            }
            for index in range(6)
        ]
        config = V12Config()
        context = build_market_context(rows, config)
        candidate = {
            "symbol": "1000",
            "industry": "測試產業",
            "strategy": "breakout",
            "bullish_score": 80.0,
            "execution_score": 80.0,
            "ranking_score": 80.0,
            "predictive_quality_score": 72.0,
            "forwardQualified": True,
            "forwardQualification": {"qualified": True, "failedRules": []},
            "warnings": [],
        }
        updated = apply_market_context(candidate, context, config)
        self.assertEqual(context["regime"], "WEAK")
        self.assertEqual(updated["bullish_score"], 76.0)
        self.assertFalse(updated["forwardQualified"])


if __name__ == "__main__":
    unittest.main()
