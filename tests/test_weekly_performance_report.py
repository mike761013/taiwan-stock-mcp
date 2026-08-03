import unittest
from datetime import date

from stock_db.performance import (
    _normalise_version,
    _parse_date,
    _version_where,
    build_weekly_report,
)


def _row(
    *,
    run_id,
    run_date,
    strategy,
    symbol,
    name,
    score,
    d1=None,
    d3=None,
    d5=None,
):
    return {
        "radar_run_id": run_id,
        "run_date": run_date,
        "strategy": strategy,
        "symbol": symbol,
        "name": name,
        "total_score": score,
        "entry_date": run_date,
        "entry_close": 100,
        "return_d1": d1,
        "return_d3": d3,
        "return_d5": d5,
        "return_d10": None,
        "return_d20": None,
        "max_favorable_percent": 8,
        "max_adverse_percent": -4,
    }


class WeeklyPerformanceReportTests(unittest.TestCase):
    def test_deduplicates_same_stock_day_but_keeps_strategies(self):
        rows = [
            _row(
                run_id=1,
                run_date=date(2026, 7, 31),
                strategy="v12_breakout",
                symbol="2006",
                name="東和鋼鐵",
                score=90,
                d1=3,
            ),
            _row(
                run_id=2,
                run_date=date(2026, 7, 31),
                strategy="v12_pullback",
                symbol="2006",
                name="東和鋼鐵",
                score=85,
                d1=3,
            ),
            # Re-running the same strategy on the same day must not add a sample.
            _row(
                run_id=3,
                run_date=date(2026, 7, 31),
                strategy="v12_breakout",
                symbol="2006",
                name="東和鋼鐵",
                score=88,
                d1=3,
            ),
            _row(
                run_id=4,
                run_date=date(2026, 7, 31),
                strategy="v12_early_stage",
                symbol="2610",
                name="華航",
                score=80,
                d1=-1,
            ),
        ]

        report = build_weekly_report(
            rows,
            version="V12",
            start_date=date(2026, 7, 27),
            end_date=date(2026, 7, 31),
            top_n=10,
            latest_market_date=date(2026, 7, 31),
        )

        self.assertEqual(report["rawSignals"], 4)
        self.assertEqual(report["strategySignalsAfterDedup"], 3)
        self.assertEqual(report["uniqueSignals"], 2)
        self.assertEqual(report["duplicatesRemoved"], 2)
        self.assertEqual(report["overall"]["d1"]["samples"], 2)
        self.assertEqual(report["overall"]["d1"]["averagePercent"], 1.0)
        self.assertEqual(report["overall"]["d1"]["winRatePercent"], 50.0)
        self.assertEqual(
            report["best"]["d1"][0]["strategies"],
            ["breakout", "pullback"],
        )

    def test_pending_horizon_is_not_counted_as_a_loss(self):
        rows = [
            _row(
                run_id=1,
                run_date=date(2026, 7, 27),
                strategy="v12_pullback",
                symbol="1808",
                name="潤隆",
                score=90,
                d1=1,
                d3=2,
                d5=4,
            ),
            _row(
                run_id=2,
                run_date=date(2026, 7, 31),
                strategy="v12_pullback",
                symbol="2610",
                name="華航",
                score=85,
                d1=None,
                d3=None,
                d5=None,
            ),
        ]

        report = build_weekly_report(
            rows,
            version="V12",
            start_date=date(2026, 7, 27),
            end_date=date(2026, 7, 31),
        )

        d5 = report["overall"]["d5"]
        self.assertEqual(d5["samples"], 1)
        self.assertEqual(d5["pending"], 1)
        self.assertEqual(d5["averagePercent"], 4.0)
        self.assertEqual(d5["winRatePercent"], 100.0)

    def test_version_filter_targets_real_v12_strategy_names(self):
        clause = _version_where(_normalise_version("v12"))
        self.assertIn("v12_", clause)
        self.assertIn("postgres-v12", clause)

    def test_date_validation_uses_iso_format(self):
        self.assertEqual(
            _parse_date("2026-07-31", "start_date"),
            date(2026, 7, 31),
        )
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            _parse_date("2026/07/31", "start_date")


if __name__ == "__main__":
    unittest.main()
