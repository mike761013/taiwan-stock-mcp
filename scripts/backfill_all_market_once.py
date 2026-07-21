"""Rate-limited, resumable full-market backfill for GitHub Actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stock_db.data_sources import fetch_security_master
from stock_db.pipeline import backfill_symbols
from stock_db.service import stock_database_service

CHECKPOINT_PATH = Path("backfill_checkpoint.json")
FAILURES_PATH = Path("backfill_failures.json")


def _write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--request-delay", type=float, default=6.5)
    parser.add_argument("--start-after", type=str, default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    return parser.parse_args()


def _is_forbidden(error: str) -> bool:
    value = error.lower()
    return "403" in value or "forbidden" in value or "ip banned" in value


async def main() -> int:
    args = _parse_args()
    years = max(1, min(args.years, 10))
    request_delay = max(6.2, min(args.request_delay, 60.0))
    start_after = args.start_after.strip()
    max_symbols = max(0, args.max_symbols)

    initialized = await stock_database_service.initialize()
    if not initialized.get("ok"):
        print(json.dumps(initialized, ensure_ascii=False, default=str))
        return 1

    master = await fetch_security_master()
    symbols = [str(row["symbol"]).strip() for row in master if row.get("symbol")]

    if start_after:
        if start_after not in symbols:
            print(f"ERROR: start_after={start_after!r} 不在股票清單內。", flush=True)
            return 1
        symbols = symbols[symbols.index(start_after) + 1 :]

    if max_symbols > 0:
        symbols = symbols[:max_symbols]

    total = len(symbols)
    processed = failed = bars_written = 0
    failures: list[dict[str, Any]] = []
    last_successful = start_after or None

    print(json.dumps({
        "event": "backfill_started",
        "years": years,
        "requestDelaySeconds": request_delay,
        "startAfter": start_after or None,
        "symbolCount": total,
    }, ensure_ascii=False), flush=True)

    for index, symbol in enumerate(symbols):
        result = await backfill_symbols([symbol], years=years, concurrency=1)
        symbol_failures = result.get("failures") or []

        if int(result.get("processedSymbols", 0)) > 0:
            processed += 1
            bars_written += int(result.get("barsWritten", 0))
            last_successful = symbol
        else:
            failed += 1
            failures.extend(symbol_failures)
            error_text = " | ".join(str(x.get("error") or "") for x in symbol_failures)
            if _is_forbidden(error_text):
                remaining = total - index
                checkpoint = {
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "status": "paused_finmind_403",
                    "blockedAtSymbol": symbol,
                    "lastSuccessfulSymbol": last_successful,
                    "nextStartAfter": last_successful,
                    "hasMore": True,
                    "remainingSymbols": remaining,
                    "processedSymbols": processed,
                    "failedSymbols": failed,
                    "barsWritten": bars_written,
                    "requestDelaySeconds": request_delay,
                }
                _write_json(CHECKPOINT_PATH, checkpoint)
                _write_json(FAILURES_PATH, failures)
                print(json.dumps(checkpoint, ensure_ascii=False, indent=2), flush=True)
                return 75

        remaining = total - index - 1
        checkpoint = {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "status": "running" if remaining else "completed",
            "lastSuccessfulSymbol": last_successful,
            "nextStartAfter": last_successful if remaining else None,
            "hasMore": remaining > 0,
            "remainingSymbols": remaining,
            "processedSymbols": processed,
            "failedSymbols": failed,
            "barsWritten": bars_written,
            "requestDelaySeconds": request_delay,
        }
        _write_json(CHECKPOINT_PATH, checkpoint)
        _write_json(FAILURES_PATH, failures)

        if processed % 20 == 0 or remaining == 0:
            print(json.dumps({
                "event": "progress",
                "processedSymbols": processed,
                "failedSymbols": failed,
                "barsWritten": bars_written,
                "remainingSymbols": remaining,
                "nextStartAfter": checkpoint["nextStartAfter"],
            }, ensure_ascii=False), flush=True)

        if remaining:
            await asyncio.sleep(request_delay)

    statistics = await stock_database_service.statistics()
    final = {
        "ok": failed == 0,
        "processedSymbols": processed,
        "failedSymbols": failed,
        "barsWritten": bars_written,
        "lastSuccessfulSymbol": last_successful,
        "statistics": statistics,
    }
    print(json.dumps(final, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if final["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted. Resume with nextStartAfter in checkpoint.", file=sys.stderr)
        raise SystemExit(130)
