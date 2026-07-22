"""Repair only active securities that currently have zero daily bars."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stock_db.connection import stock_database
from stock_db.pipeline import backfill_symbols
from stock_db.service import stock_database_service

TARGETS_PATH = Path("repair_targets.json")
CHECKPOINT_PATH = Path("repair_checkpoint.json")
FAILURES_PATH = Path("repair_failures.json")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--request-delay", type=float, default=10.0)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def _error_text(result: dict[str, Any]) -> str:
    return " | ".join(
        str(item.get("error") or "")
        for item in (result.get("failures") or [])
    )


def _is_hard_rate_limit(error: str) -> bool:
    value = error.lower()
    return any(token in value for token in (
        "403", "forbidden", "ip banned",
        "402", "upper limit", "requests reach",
    ))


def _is_transient(error: str) -> bool:
    value = error.lower()
    return any(token in value for token in (
        "timeout", "timed out", "connecterror", "connection",
        "network", "429", "too many requests",
        "500", "502", "503", "504", "server error",
    ))


async def _missing_symbols() -> list[dict[str, Any]]:
    async with stock_database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT s.symbol, s.name
            FROM securities s
            LEFT JOIN daily_bars b ON b.symbol = s.symbol
            WHERE s.is_active = TRUE
            GROUP BY s.symbol, s.name
            HAVING COUNT(b.trade_date) = 0
            ORDER BY s.symbol
            """
        )
    return [dict(row) for row in rows]


async def _bar_count(symbol: str) -> int:
    async with stock_database.acquire() as connection:
        value = await connection.fetchval(
            "SELECT COUNT(*) FROM daily_bars WHERE symbol = $1",
            symbol,
        )
    return int(value or 0)


async def main() -> int:
    args = _parse_args()
    years = max(1, min(args.years, 10))
    request_delay = max(8.0, min(args.request_delay, 60.0))
    retries = max(0, min(args.retries, 10))

    initialized = await stock_database_service.initialize()
    if not initialized.get("ok"):
        print(json.dumps(initialized, ensure_ascii=False, default=str))
        return 1

    targets = await _missing_symbols()
    _write_json(TARGETS_PATH, targets)

    total = len(targets)
    repaired: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    print(json.dumps({
        "event": "repair_started",
        "targetCount": total,
        "years": years,
        "requestDelaySeconds": request_delay,
        "retries": retries,
        "targets": targets,
    }, ensure_ascii=False), flush=True)

    for index, target in enumerate(targets):
        symbol = str(target["symbol"])
        name = str(target.get("name") or symbol)
        success = False
        last_error = ""

        for attempt in range(1, retries + 2):
            result = await backfill_symbols([symbol], years=years, concurrency=1)
            count = await _bar_count(symbol)

            if count > 0:
                success = True
                repaired.append({
                    "symbol": symbol,
                    "name": name,
                    "attempt": attempt,
                    "barCount": count,
                    "barsWritten": int(result.get("barsWritten", 0)),
                })
                break

            last_error = _error_text(result) or "FinMind returned zero rows"

            if _is_hard_rate_limit(last_error):
                pending = targets[index:]
                checkpoint = {
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                    "status": "paused_rate_limit",
                    "blockedAtSymbol": symbol,
                    "processedTargets": index,
                    "repairedCount": len(repaired),
                    "remainingTargets": len(pending),
                    "repaired": repaired,
                    "pending": pending,
                    "lastError": last_error,
                }
                failures.append({
                    "symbol": symbol,
                    "name": name,
                    "attempt": attempt,
                    "error": last_error,
                })
                _write_json(CHECKPOINT_PATH, checkpoint)
                _write_json(FAILURES_PATH, failures)
                print(json.dumps(checkpoint, ensure_ascii=False, indent=2), flush=True)
                return 75

            if attempt <= retries:
                wait_seconds = 30 * (2 ** (attempt - 1)) if _is_transient(last_error) else request_delay
                print(json.dumps({
                    "event": "retry",
                    "symbol": symbol,
                    "name": name,
                    "attempt": attempt,
                    "waitSeconds": wait_seconds,
                    "error": last_error,
                }, ensure_ascii=False), flush=True)
                await asyncio.sleep(wait_seconds)

        if not success:
            failures.append({
                "symbol": symbol,
                "name": name,
                "attempts": retries + 1,
                "error": last_error,
            })

        remaining = total - index - 1
        checkpoint = {
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "status": "running" if remaining else "completed",
            "processedTargets": index + 1,
            "totalTargets": total,
            "repairedCount": len(repaired),
            "failedCount": len(failures),
            "remainingTargets": remaining,
            "lastSymbol": symbol,
        }
        _write_json(CHECKPOINT_PATH, checkpoint)
        _write_json(FAILURES_PATH, failures)

        print(json.dumps({
            "event": "repair_progress",
            "processedTargets": index + 1,
            "totalTargets": total,
            "repairedCount": len(repaired),
            "failedCount": len(failures),
            "remainingTargets": remaining,
            "lastSymbol": symbol,
        }, ensure_ascii=False), flush=True)

        if remaining:
            await asyncio.sleep(request_delay)

    still_missing = await _missing_symbols()
    final = {
        "ok": len(still_missing) == 0,
        "targetCount": total,
        "repairedCount": len(repaired),
        "failedCount": len(failures),
        "stillMissingCount": len(still_missing),
        "stillMissing": still_missing,
    }
    _write_json(CHECKPOINT_PATH, {
        **final,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "completed" if final["ok"] else "completed_with_failures",
    })
    _write_json(FAILURES_PATH, failures)
    print(json.dumps(final, ensure_ascii=False, indent=2), flush=True)
    return 0 if final["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted. Re-run the same workflow; it will query only symbols still missing.", file=sys.stderr)
        raise SystemExit(130)
