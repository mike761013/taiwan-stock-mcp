"""Backfill, daily-update, and indicator pipelines."""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from typing import Any, Sequence

from .connection import stock_database
from .data_sources import (
    fetch_finmind_history,
    fetch_official_daily_snapshot_with_fallback,
    fetch_security_master,
)
from .importers import row_to_daily_bar
from .models import Security
from .repository import stock_repository
from .service import stock_database_service


_COMMON_STOCK_SYMBOL_RE = re.compile(r"^[1-9][0-9]{3}$")
_COMMON_STOCK_MARKETS = {"TWSE", "TPEX", "OTC"}


def is_listed_otc_common_stock(symbol: Any, market: Any) -> bool:
    """Return whether a security belongs to the V12 common-stock universe."""
    normalized_symbol = str(symbol or "").strip()
    normalized_market = str(market or "").strip().upper()
    return (
        normalized_market in _COMMON_STOCK_MARKETS
        and _COMMON_STOCK_SYMBOL_RE.fullmatch(normalized_symbol) is not None
    )


async def _latest_common_stock_symbols() -> list[str]:
    """Read the latest TWSE/TPEx common-stock universe from PostgreSQL.

    Each market uses its own newest trade date so a temporarily delayed TPEx
    feed does not remove all OTC symbols from a resumable maintenance run.
    """
    query = """
        WITH market_latest AS (
            SELECT UPPER(s.market) AS market_key,
                   MAX(b.trade_date) AS trade_date
            FROM daily_bars b
            JOIN securities s ON s.symbol = b.symbol
            WHERE s.is_active = TRUE
              AND UPPER(s.market) = ANY($1::varchar[])
              AND b.symbol ~ '^[1-9][0-9]{3}$'
            GROUP BY UPPER(s.market)
        )
        SELECT DISTINCT b.symbol
        FROM daily_bars b
        JOIN securities s ON s.symbol = b.symbol
        JOIN market_latest d
          ON d.market_key = UPPER(s.market)
         AND d.trade_date = b.trade_date
        WHERE s.is_active = TRUE
          AND UPPER(s.market) = ANY($1::varchar[])
          AND b.symbol ~ '^[1-9][0-9]{3}$'
        ORDER BY b.symbol
    """
    async with stock_database.acquire() as connection:
        rows = await connection.fetch(query, sorted(_COMMON_STOCK_MARKETS))
    return [str(row["symbol"]) for row in rows]


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
    years: int = 5,
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
    batch_size: int = 500,
    start_after: str | None = None,
    concurrency: int = 6,
) -> dict[str, Any]:
    """Update official daily bars and calculate one resumable indicator batch.

    The official endpoints return mixed security types. Only TWSE/TPEx
    four-digit common stocks are persisted and used for indicator calculation.
    Continuation calls reuse the first call's committed snapshot, keeping each
    HTTP request short enough for a free Render Web Service.
    """
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init

    # The first request refreshes the official snapshot. Continuation requests
    # reuse the committed snapshot so every continuation batch does not download
    # the same 11k-row payload again.
    refresh_snapshot = not start_after
    raw_rows: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    written = 0
    snapshot_metadata: dict[str, Any] = {
        "primaryMarketDates": {},
        "finalMarketDates": {},
        "referenceDate": None,
        "targetDate": None,
        "fallbackEnabled": None,
        "fallbackUsed": False,
        "fallbackMarkets": [],
        "fallbackAttempts": [],
        "dataIntegrity": {
            "reusedCommittedSnapshot": not refresh_snapshot,
        },
    }
    if refresh_snapshot:
        snapshot = await fetch_official_daily_snapshot_with_fallback()
        snapshot_metadata = {
            key: snapshot.get(key)
            for key in (
                "primaryMarketDates",
                "finalMarketDates",
                "referenceDate",
                "targetDate",
                "fallbackEnabled",
                "fallbackUsed",
                "fallbackMarkets",
                "fallbackAttempts",
                "primaryErrors",
                "dataIntegrity",
            )
        }
        if not snapshot.get("ok"):
            return {
                "ok": False,
                "errorCode": snapshot.get("errorCode"),
                "error": snapshot.get("error"),
                "scope": "TWSE_TPEX_COMMON_STOCKS",
                "snapshotRefreshed": False,
                "rawRowsFetched": 0,
                "rowsFetched": 0,
                "barsWritten": 0,
                "universeCount": 0,
                "batchSize": 0,
                "batchSymbolCount": 0,
                "batchSymbols": [],
                "indicatorSymbols": 0,
                "failedSymbols": 0,
                "indicatorRowsWritten": 0,
                "indicatorFailures": [],
                "lastSymbol": start_after,
                "hasMore": False,
                "nextStartAfter": None,
                "remainingSymbols": 0,
                "startAfterFound": True,
                **snapshot_metadata,
            }
        raw_rows = list(snapshot.get("rows") or [])
        rows = [
            row
            for row in raw_rows
            if is_listed_otc_common_stock(
                row.get("symbol"),
                row.get("market"),
            )
        ]
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

    all_symbols = await _latest_common_stock_symbols()
    remaining, marker_found = _resume_slice(all_symbols, start_after)
    batch_size = max(1, min(batch_size, 500))
    target_symbols = remaining[:batch_size]

    processed = failed = indicator_rows_written = 0
    indicator_failures: list[dict[str, str]] = []
    if target_symbols:
        try:
            indicator_result = (
                await stock_database_service.calculate_latest_indicators_bulk(
                    target_symbols,
                    lookback_bars=61,
                )
            )
            processed = int(indicator_result.get("processedSymbols", 0))
            failed = int(indicator_result.get("failedSymbols", 0))
            indicator_rows_written = int(
                indicator_result.get("indicatorRowsWritten", 0)
            )
            indicator_failures = list(
                indicator_result.get("failures") or []
            )
        except Exception as exc:
            # Compatibility/resilience fallback: a transient bulk query error
            # must not discard the whole close batch. Recalculate each symbol
            # with the already committed bars.
            bulk_error = f"{type(exc).__name__}: {exc}"
            for symbol in target_symbols:
                try:
                    one = await stock_database_service.calculate_symbol_indicators(
                        symbol,
                        latest_only=True,
                    )
                    processed += 1
                    indicator_rows_written += int(one.get("processed", 0))
                except Exception as one_exc:
                    failed += 1
                    indicator_failures.append({
                        "symbol": symbol,
                        "error": f"{type(one_exc).__name__}: {one_exc}",
                    })
            if failed:
                indicator_failures.insert(0, {
                    "symbol": "__BATCH__",
                    "error": bulk_error,
                })

    remaining_after = max(0, len(remaining) - len(target_symbols))
    has_more = remaining_after > 0
    last_symbol = target_symbols[-1] if target_symbols else start_after
    return {
        "ok": failed == 0,
        "scope": "TWSE_TPEX_COMMON_STOCKS",
        "snapshotRefreshed": refresh_snapshot,
        "rawRowsFetched": len(raw_rows),
        "rowsFetched": len(rows),
        "barsWritten": written,
        "universeCount": len(all_symbols),
        "batchSize": batch_size,
        "indicatorCalculationMode": "bulk_latest_61_bars",
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
        **snapshot_metadata,
    }
