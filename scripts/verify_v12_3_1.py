"""Offline-safe verification for the V12.3.1 fix package."""

from __future__ import annotations

import json
import py_compile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "server_v10_tools.py",
    "v12_config.json",
    "stock_db/factors.py",
    "stock_db/v12.py",
    "stock_db/radar.py",
    "stock_db/performance.py",
    "stock_db/schema.sql",
    "stock_db/maintenance.py",
    "stock_db/pipeline.py",
    "stock_db/repository.py",
]


def main() -> int:
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        print("MISSING:", ", ".join(missing))
        return 1
    for relative in FILES:
        path = ROOT / relative
        if path.suffix == ".py":
            py_compile.compile(str(path), doraise=True)
    config = json.loads((ROOT / "v12_config.json").read_text(encoding="utf-8"))
    required = {
        "factor_weight_technical",
        "factor_weight_chip",
        "factor_weight_fundamental",
        "factor_weight_theme",
        "factor_weight_intraday",
        "factor_weight_market",
        "factor_weight_history",
        "factor_minimum_confidence",
        "forward_min_bullish_score",
    }
    absent = sorted(required - set(config))
    if absent:
        print("CONFIG MISSING:", ", ".join(absent))
        return 1
    weights = [key for key in required if key.startswith("factor_weight_")]
    if abs(sum(float(config[key]) for key in weights) - 100) > 0.001:
        print("ERROR: factor weights must total 100")
        return 1
    markers = {
        "stock_db/v12.py": "V12.3.1_SEVEN_FACTOR_FIX",
        "stock_db/factors.py": "_parse_official_revenue_row",
        "stock_db/schema.sql": "VALUES (1231,",
        "stock_db/performance.py": "V12.3.1_DUAL_ENTRY_20D",
    }
    for relative, marker in markers.items():
        if marker not in (ROOT / relative).read_text(encoding="utf-8"):
            print(f"MARKER MISSING: {relative} -> {marker}")
            return 1
    print("V12.3.1 VERIFY PASSED")
    print("You can commit and push these files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
