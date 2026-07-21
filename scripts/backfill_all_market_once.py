"""One-click full-market stock-history backfill for GitHub Actions.

This script runs outside the MCP HTTP request, so Render's gateway timeout
does not interrupt the work. It writes a checkpoint after every batch.
"""

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
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--start-after", type=str, default="")
    parser.add_argument("--max-symbols", type=int, default=0)
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    years = max(1, min(args.years, 10))
    batch_size = max(1, min(args.batch_size, 100))
    concurrency = max(1, min(args.concurrency, 6))
    start_after = args.start_after.strip()
    max_symbols = max(0, args.max_symbols)

    initialized = await stock_database_service.initialize()
    if not initialized.get("ok"):
        print(json.dumps(initialized, ensure_ascii=False, default=str))
        return 1

    master = await fetch_security_master()
    all_symbols = [str(row["symbol"]).strip() for row in master if row.get("symbol")]

    start_after_found = not start_after
    if start_after:
        if start_after in all_symbols:
            all_symbols = all_symbols[all_symbols.index(start_after) + 1 :]
            start_after_found = True
        else:
            print(
                f"WARNING: start_after={start_after!r} 不在股票清單內，"
                "將從第一檔開始。",
                flush=True,
            )

    if max_symbols > 0:
        all_symbols = all_symbols[:max_symbols]

    total_symbols = len(all_symbols)
    total_processed = 0
    total_failed = 0
    total_bars = 0
    failures: list[dict[str, Any]] = []
    last_symbol: str | None = start_after or None

    print(
        json.dumps(
            {
                "event": "backfill_started",
                "years": years,
                "batchSize": batch_size,
                "concurrency": concurrency,
                "startAfter": start_after or None,
                "startAfterFound": start_after_found,
                "symbolCount": total_symbols,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    for offset in range(0, total_symbols, batch_size):
        batch = all_symbols[offset : offset + batch_size]
        result = await backfill_symbols(
            batch,
            years=years,
            concurrency=concurrency,
        )

        total_processed += int(result.get("processedSymbols", 0))
        total_failed += int(result.get("failedSymbols", 0))
        total_bars += int(result.get("barsWritten", 0))
        failures.extend(result.get("failures") or [])
        last_symbol = batch[-1] if batch else last_symbol

        remaining = max(total_symbols - (offset + len(batch)), 0)
        checkpoint = {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "years": years,
            "batchSize": batch_size,
            "concurrency": concurrency,
            "startAfter": start_after or None,
            "lastSymbol": last_symbol,
            "nextStartAfter": last_symbol if remaining > 0 else None,
            "hasMore": remaining > 0,
            "remainingSymbols": remaining,
            "totalSymbolsThisRun": total_symbols,
            "processedSymbols": total_processed,
            "failedSymbols": total_failed,
            "barsWritten": total_bars,
        }
        _write_json(CHECKPOINT_PATH, checkpoint)
        _write_json(FAILURES_PATH, failures)

        print(
            json.dumps(
                {
                    "event": "batch_completed",
                    "batchStart": batch[0] if batch else None,
                    "batchEnd": last_symbol,
                    "batchSymbolCount": len(batch),
                    "processedSymbols": total_processed,
                    "failedSymbols": total_failed,
                    "barsWritten": total_bars,
                    "remainingSymbols": remaining,
                    "nextStartAfter": checkpoint["nextStartAfter"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # Retry failed symbols once, using low concurrency to reduce API pressure.
    retry_symbols = sorted(
        {
            str(item.get("symbol") or "").strip()
            for item in failures
            if str(item.get("symbol") or "").strip()
        }
    )
    retry_result: dict[str, Any] | None = None
    if retry_symbols:
        print(
            f"Retrying {len(retry_symbols)} failed symbols with concurrency=1",
            flush=True,
        )
        retry_result = await backfill_symbols(
            retry_symbols,
            years=years,
            concurrency=1,
        )

    statistics = await stock_database_service.statistics()
    final = {
        "ok": total_failed == 0 or bool(retry_result and retry_result.get("ok")),
        "processedSymbols": total_processed,
        "failedSymbolsBeforeRetry": total_failed,
        "barsWritten": total_bars,
        "lastSymbol": last_symbol,
        "retry": retry_result,
        "statistics": statistics,
    }
    print(json.dumps(final, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if final["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted. Use backfill_checkpoint.json to resume.", file=sys.stderr)
        raise SystemExit(130)
