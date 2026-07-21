"""Backfill, daily-update, and indicator pipelines."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Sequence

from .data_sources import (
    fetch_finmind_history,
    fetch_official_daily_snapshot,
    fetch_security_master,
)
from .importers import row_to_daily_bar
from .models import Security
from .repository import stock_repository
from .service import stock_database_service


def _normalise_symbols(symbols: str | Sequence[str] | None) -> list[str]:
    if symbols is None:
        return []
    if isinstance(symbols, str):
        raw_items = symbols.split(",")
    else:
        raw_items = symbols
    return list(dict.fromkeys(
        str(item).strip() for item in raw_items if str(item).strip()
    ))


def _resume_slice(
    symbols: Sequence[str],
    start_after: str | None,
) -> tuple[list[str], bool]:
    ordered = sorted(dict.fromkeys(str(symbol).strip() for symbol in symbols))
    if not start_after:
        return ordered, True
    marker = str(start_after).strip()
    if marker in ordered:
        return ordered[ordered.index(marker) + 1:], True
    # Marker may come from an older security-master snapshot. Falling back to
    # lexical order is safer than silently restarting from the first symbol.
    return [symbol for symbol in ordered if symbol > marker], False


async def sync_security_master() -> dict[str, Any]:
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init
    rows = await fetch_security_master()
    securities = [
        Security(
            symbol=row["symbol"],
            name=row["name"],
            market=row["market"],
            industry=row.get("industry"),
            is_active=True,
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

    symbol_list = _normalise_symbols(symbols)
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

    await asyncio.gather(*(one(symbol) for symbol in symbol_list))
    return {
        "ok": failed == 0,
        "symbolCount": len(symbol_list),
        "processedSymbols": processed,
        "failedSymbols": failed,
        "barsWritten": bars_written,
        "failures": failures[:50],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
    }


async def backfill_all_market(
    years: int = 3,
    batch_size: int = 20,
    start_after: str | None = None,
    concurrency: int = 3,
) -> dict[str, Any]:
    """Process one resumable market batch instead of one long HTTP request."""
    master = await fetch_security_master()
    all_symbols = sorted({str(row["symbol"]).strip() for row in master})
    remaining, marker_found = _resume_slice(all_symbols, start_after)
    batch_size = max(1, min(batch_size, 50))
    batch = remaining[:batch_size]

    if not batch:
        return {
            "ok": True,
            "universeCount": len(all_symbols),
            "batchSize": batch_size,
            "batchSymbolCount": 0,
            "processedSymbols": 0,
            "failedSymbols": 0,
            "barsWritten": 0,
            "failures": [],
            "lastSymbol": start_after,
            "hasMore": False,
            "nextStartAfter": None,
            "remainingSymbols": 0,
            "startAfterFound": marker_found,
        }

    result = await backfill_symbols(
        batch,
        years=years,
        concurrency=concurrency,
    )
    last_symbol = batch[-1]
    remaining_after = max(0, len(remaining) - len(batch))
    has_more = remaining_after > 0
    return {
        **result,
        "universeCount": len(all_symbols),
        "batchSize": batch_size,
        "batchSymbolCount": len(batch),
        "batchSymbols": batch,
        "lastSymbol": last_symbol,
        "hasMore": has_more,
        "nextStartAfter": last_symbol if has_more else None,
        "remainingSymbols": remaining_after,
        "startAfterFound": marker_found,
    }


async def calculate_all_indicators(
    symbols: str | Sequence[str] | None = None,
    batch_size: int = 20,
    start_after: str | None = None,
    concurrency: int = 3,
) -> dict[str, Any]:
    """Calculate indicators for explicit symbols or one resumable market batch."""
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init

    explicit_symbols = _normalise_symbols(symbols)
    is_full_market = not explicit_symbols
    marker_found = True

    if is_full_market:
        all_symbols = await stock_repository.get_symbols_with_daily_bars()
        remaining, marker_found = _resume_slice(all_symbols, start_after)
        batch_size = max(1, min(batch_size, 100))
        target_symbols = remaining[:batch_size]
        remaining_after = max(0, len(remaining) - len(target_symbols))
    else:
        all_symbols = explicit_symbols
        target_symbols = explicit_symbols
        remaining_after = 0

    if not target_symbols:
        return {
            "ok": True,
            "mode": "all" if is_full_market else "symbols",
            "universeCount": len(all_symbols),
            "processedSymbols": 0,
            "failedSymbols": 0,
            "indicatorRowsWritten": 0,
            "failures": [],
            "lastSymbol": start_after,
            "hasMore": False,
            "nextStartAfter": None,
            "remainingSymbols": 0,
            "startAfterFound": marker_found,
        }

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 6)))
    processed = failed = rows_written = 0
    failures: list[dict[str, str]] = []

    async def one(symbol: str) -> None:
        nonlocal processed, failed, rows_written
        async with semaphore:
            try:
                result = await stock_database_service.calculate_symbol_indicators(symbol)
                rows_written += int(result.get("processed", 0))
                processed += 1
            except Exception as exc:
                failed += 1
                failures.append({
                    "symbol": symbol,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    await asyncio.gather(*(one(symbol) for symbol in target_symbols))
    last_symbol = target_symbols[-1]
    has_more = is_full_market and remaining_after > 0
    return {
        "ok": failed == 0,
        "mode": "all" if is_full_market else "symbols",
        "universeCount": len(all_symbols),
        "batchSymbolCount": len(target_symbols),
        "batchSymbols": target_symbols,
        "processedSymbols": processed,
        "failedSymbols": failed,
        "indicatorRowsWritten": rows_written,
        "failures": failures[:100],
        "lastSymbol": last_symbol,
        "hasMore": has_more,
        "nextStartAfter": last_symbol if has_more else None,
        "remainingSymbols": remaining_after,
        "startAfterFound": marker_found,
    }


async def update_official_daily(
    batch_size: int = 50,
    start_after: str | None = None,
    concurrency: int = 6,
) -> dict[str, Any]:
    """Update official daily bars and calculate one resumable indicator batch.

    Official bars are bulk-upserted on every call and are idempotent. Indicator
    calculation only writes the newest row for each symbol, keeping each HTTP
    request short enough for a free Render Web Service.
    """
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init

    rows = await fetch_official_daily_snapshot()
    securities = [
        Security(symbol=row["symbol"], name=row["name"], market=row["market"])
        for row in rows
    ]
    await stock_repository.upsert_securities(securities)

    bars = [
        row_to_daily_bar(row, row["source"])
        for row in rows
        if row.get("close") is not None
    ]
    written = await stock_repository.bulk_upsert_daily_bars(bars)
    all_symbols = sorted({bar.symbol for bar in bars})
    remaining, marker_found = _resume_slice(all_symbols, start_after)
    batch_size = max(1, min(batch_size, 100))
    target_symbols = remaining[:batch_size]

    semaphore = asyncio.Semaphore(max(1, min(concurrency, 8)))
    processed = failed = indicator_rows_written = 0
    indicator_failures: list[dict[str, str]] = []

    async def one(symbol: str) -> None:
        nonlocal processed, failed, indicator_rows_written
        async with semaphore:
            try:
                result = await stock_database_service.calculate_symbol_indicators(
                    symbol,
                    latest_only=True,
                )
                indicator_rows_written += int(result.get("processed", 0))
                processed += 1
            except Exception as exc:
                failed += 1
                indicator_failures.append({
                    "symbol": symbol,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    await asyncio.gather(*(one(symbol) for symbol in target_symbols))

    remaining_after = max(0, len(remaining) - len(target_symbols))
    has_more = remaining_after > 0
    last_symbol = target_symbols[-1] if target_symbols else start_after
    return {
        "ok": failed == 0,
        "rowsFetched": len(rows),
        "barsWritten": written,
        "universeCount": len(all_symbols),
        "batchSize": batch_size,
        "batchSymbolCount": len(target_symbols),
        "batchSymbols": target_symbols,
        "indicatorSymbols": processed,
        "failedSymbols": failed,
        "indicatorRowsWritten": indicator_rows_written,
        "indicatorFailures": indicator_failures[:50],
        "lastSymbol": last_symbol,
        "hasMore": has_more,
        "nextStartAfter": last_symbol if has_more else None,
        "remainingSymbols": remaining_after,
        "startAfterFound": marker_found,
    }
