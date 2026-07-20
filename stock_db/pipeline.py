"""Backfill, daily-update, and indicator pipelines."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from .data_sources import (
    fetch_finmind_history,
    fetch_official_daily_snapshot,
    fetch_security_master,
)
from .importers import row_to_daily_bar
from .models import Security
from .repository import stock_repository
from .service import stock_database_service


async def sync_security_master() -> dict[str, Any]:
    rows = await fetch_security_master()
    securities = [
        Security(
            symbol=row["symbol"], name=row["name"], market=row["market"],
            industry=row.get("industry"), is_active=True,
        )
        for row in rows
    ]
    count = await stock_repository.upsert_securities(securities)
    return {"ok": True, "processed": count}


async def backfill_symbols(
    symbols: Sequence[str],
    years: int = 3,
    concurrency: int = 3,
) -> dict[str, Any]:
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init
    end = date.today()
    start = end - timedelta(days=365 * max(1, min(years, 10)))
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 6)))
    processed = failed = bars_written = 0
    failures: list[dict[str, str]] = []

    async def one(symbol: str) -> None:
        nonlocal processed, failed, bars_written
        async with semaphore:
            try:
                rows = await fetch_finmind_history(symbol, start, end)
                bars = [row_to_daily_bar(row, "FinMind") for row in rows]
                bars_written += await stock_repository.bulk_upsert_daily_bars(bars)
                await stock_database_service.calculate_symbol_indicators(symbol)
                processed += 1
            except Exception as exc:
                failed += 1
                failures.append({
                    "symbol": symbol,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    await asyncio.gather(*(one(str(s).strip()) for s in symbols if str(s).strip()))
    return {
        "ok": failed == 0,
        "symbolCount": len(symbols),
        "processedSymbols": processed,
        "failedSymbols": failed,
        "barsWritten": bars_written,
        "failures": failures[:50],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }


async def backfill_all_market(
    years: int = 3,
    batch_size: int = 50,
    start_after: str | None = None,
) -> dict[str, Any]:
    master = await fetch_security_master()
    symbols = [row["symbol"] for row in master]
    if start_after and start_after in symbols:
        symbols = symbols[symbols.index(start_after) + 1:]
    batch_size = max(1, min(batch_size, 200))
    total = {"processedSymbols": 0, "failedSymbols": 0, "barsWritten": 0}
    failures: list[dict[str, str]] = []
    for index in range(0, len(symbols), batch_size):
        chunk = symbols[index:index + batch_size]
        result = await backfill_symbols(chunk, years=years)
        for key in total:
            total[key] += int(result.get(key, 0))
        failures.extend(result.get("failures") or [])
    return {
        "ok": total["failedSymbols"] == 0,
        "universeCount": len(symbols),
        **total,
        "failures": failures[:100],
    }


async def update_official_daily() -> dict[str, Any]:
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init
    rows = await fetch_official_daily_snapshot()
    securities = [
        Security(symbol=r["symbol"], name=r["name"], market=r["market"])
        for r in rows
    ]
    await stock_repository.upsert_securities(securities)
    bars = [row_to_daily_bar(row, row["source"]) for row in rows if row.get("close") is not None]
    written = await stock_repository.bulk_upsert_daily_bars(bars)
    symbols = sorted({bar.symbol for bar in bars})
    indicator_failures = []
    for symbol in symbols:
        try:
            await stock_database_service.calculate_symbol_indicators(symbol)
        except Exception as exc:
            indicator_failures.append(
                {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {
        "ok": True,
        "rowsFetched": len(rows),
        "barsWritten": written,
        "indicatorSymbols": len(symbols) - len(indicator_failures),
        "indicatorFailures": indicator_failures[:50],
    }
