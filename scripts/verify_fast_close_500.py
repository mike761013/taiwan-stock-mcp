from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOKENS = {
    "stock_db/pipeline.py": (
        "min(batch_size, 500)",
        "calculate_latest_indicators_bulk",
        '"indicatorCalculationMode": "bulk_latest_61_bars"',
    ),
    "stock_db/service.py": (
        "calculate_latest_indicators_bulk",
        "get_recent_daily_bars_for_symbols",
        "bulk_upsert_indicators(rows)",
    ),
    "stock_db/repository.py": (
        "get_recent_daily_bars_for_symbols",
        "CROSS JOIN LATERAL",
        "LIMIT $2",
    ),
    "stock_db/maintenance.py": (
        "batch_size: int = 500",
    ),
    "server_v10_tools.py": (
        "batch_size: int = 500",
    ),
}


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def check_files_and_syntax() -> None:
    for relative_path, tokens in EXPECTED_TOKENS.items():
        path = ROOT / relative_path
        if not path.exists():
            fail(f"missing {relative_path}")
        source = path.read_text(encoding="utf-8-sig")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            fail(f"{relative_path} syntax error: {exc}")
        for token in tokens:
            if token not in source:
                fail(f"{relative_path} missing marker: {token}")
        print(f"OK: {relative_path}")


def load_indicator_module():
    path = ROOT / "stock_db" / "indicators.py"
    spec = importlib.util.spec_from_file_location(
        "v12_fast_close_indicators",
        path,
    )
    if spec is None or spec.loader is None:
        fail("cannot load stock_db/indicators.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_latest_61_equivalence() -> None:
    module = load_indicator_module()
    start = date(2026, 1, 1)
    rows = [
        {
            "symbol": "2330",
            "trade_date": start + timedelta(days=index),
            "close": 100 + index,
            "low": 98 + index,
            "volume": 1_000_000 + index * 1_000,
        }
        for index in range(90)
    ]
    full_latest = module.calculate_indicators(rows)[-1]
    recent_latest = module.calculate_indicators(rows[-61:])[-1]
    fields = (
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "volume_ma5",
        "volume_ma20",
        "bollinger_mid",
        "bollinger_upper",
        "bollinger_lower",
        "volume_ratio",
        "volatility_20",
        "large_volume_low",
        "technical_score",
    )
    for field in fields:
        if full_latest[field] != recent_latest[field]:
            fail(
                f"latest-61 mismatch for {field}: "
                f"{full_latest[field]} != {recent_latest[field]}"
            )
    print("OK: latest 61 bars reproduce the existing latest indicators")


if __name__ == "__main__":
    check_files_and_syntax()
    check_latest_61_equivalence()
    print()
    print("V12 FAST CLOSE 500 VERIFICATION PASSED")
