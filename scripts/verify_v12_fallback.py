"""Offline regression checks for the V12 official same-date fallback."""

from __future__ import annotations

import asyncio
import json
import sys
import types
from contextlib import ExitStack
from datetime import date
from unittest.mock import patch

# The verification is intentionally runnable in a bare Python environment.
# Production Render installs these dependencies from requirements.txt.
try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError:
    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.Pool = object
    asyncpg_stub.Connection = object

    async def unavailable_pool(*args, **kwargs):
        raise RuntimeError("asyncpg is unavailable in offline verification")

    asyncpg_stub.create_pool = unavailable_pool
    sys.modules["asyncpg"] = asyncpg_stub

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_stub = types.ModuleType("httpx")

    class UnavailableAsyncClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("httpx is unavailable in offline verification")

    httpx_stub.AsyncClient = UnavailableAsyncClient
    sys.modules["httpx"] = httpx_stub

from stock_db import data_sources, maintenance, pipeline, radar


def row(symbol: str, market: str, trade_date: str) -> dict:
    return {
        "symbol": symbol,
        "name": f"股票{symbol}",
        "market": market,
        "date": trade_date,
        "open": 49.0,
        "high": 51.0,
        "low": 48.5,
        "close": 50.0,
        "volume": 1_000_000,
        "turnover": 50_000_000,
        "change_percent": 1.0,
        "source": f"{market} test",
    }


def market_snapshot(
    market: str,
    trade_date: str,
    *,
    fallback: bool = False,
) -> dict:
    symbol = "1101" if market == "TWSE" else "5001"
    return {
        "market": market,
        "date": trade_date,
        "rows": [row(symbol, market, trade_date)],
        "source": f"{market} {'fallback' if fallback else 'primary'}",
        "validation": {
            "count": 1,
            "uniqueSymbols": 1,
            "ohlcCoverage": 1.0,
            "liquidityCoverage": 1.0,
        },
        "fallback": fallback,
    }


def twse_fixture() -> dict:
    return {
        "stat": "OK",
        "date": "20260723",
        "title": "115年07月23日 每日收盤行情",
        "tables": [{
            "fields": [
                "證券代號",
                "證券名稱",
                "成交股數",
                "成交筆數",
                "成交金額",
                "開盤價",
                "最高價",
                "最低價",
                "收盤價",
                "漲跌(+/-)",
                "漲跌價差",
            ],
            "data": [[
                "1101",
                "台泥",
                "1,000,000",
                "1,000",
                "50,000,000",
                "49.0",
                "51.0",
                "48.5",
                "50.0",
                "+",
                "1.0",
            ]],
        }],
    }


def tpex_fixture() -> dict:
    return {
        "status": "success",
        "date": "2026/07/23",
        "tables": [{
            "date": "115/07/23",
            "fields": [
                "代號",
                "名稱",
                "收盤",
                "漲跌",
                "開盤",
                "最高",
                "最低",
                "成交股數",
                "成交金額",
            ],
            "data": [[
                "5001",
                "測試上櫃",
                "40.0",
                "+0.5",
                "39.5",
                "41.0",
                "39.0",
                "800,000",
                "32,000,000",
            ]],
        }],
    }


async def verify_fallback_parsers() -> None:
    async def fake_get_json(url, **kwargs):
        if "MI_INDEX" in url:
            return twse_fixture()
        if "dailyQuotes" in url:
            return tpex_fixture()
        raise AssertionError(url)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(data_sources, "_get_json", fake_get_json)
        )
        stack.enter_context(
            patch.object(data_sources, "V12_MIN_TWSE_COMMON_STOCKS", 1)
        )
        stack.enter_context(
            patch.object(data_sources, "V12_MIN_TPEX_COMMON_STOCKS", 1)
        )
        twse = await data_sources._fetch_fallback_market_snapshot(
            "TWSE",
            "2026-07-23",
        )
        tpex = await data_sources._fetch_fallback_market_snapshot(
            "TPEx",
            "2026-07-23",
        )
    assert twse["rows"][0]["symbol"] == "1101"
    assert twse["rows"][0]["volume"] == 1_000_000
    assert tpex["rows"][0]["symbol"] == "5001"
    assert tpex["rows"][0]["change_percent"] == 0.5


async def verify_resolution_and_rejection() -> None:
    async def primary(market):
        trade_date = "2026-07-22" if market == "TWSE" else "2026-07-23"
        return market_snapshot(market, trade_date)

    async def reference():
        return "2026-07-23"

    async def fallback(market, target_date):
        assert market == "TWSE"
        return market_snapshot(market, target_date, fallback=True)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_primary_market_snapshot",
                primary,
            )
        )
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_reference_trade_date",
                reference,
            )
        )
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_fallback_market_snapshot",
                fallback,
            )
        )
        resolved = (
            await data_sources.fetch_official_daily_snapshot_with_fallback()
        )
    assert resolved["ok"] is True
    assert resolved["fallbackMarkets"] == ["TWSE"]
    assert resolved["finalMarketDates"] == {
        "TWSE": "2026-07-23",
        "TPEx": "2026-07-23",
    }

    async def failed_fallback(market, target_date):
        raise RuntimeError("備援尚未發布")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_primary_market_snapshot",
                primary,
            )
        )
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_reference_trade_date",
                reference,
            )
        )
        stack.enter_context(
            patch.object(
                data_sources,
                "_fetch_fallback_market_snapshot",
                failed_fallback,
            )
        )
        rejected = (
            await data_sources.fetch_official_daily_snapshot_with_fallback()
        )
    assert rejected["ok"] is False
    assert rejected["errorCode"] == "MARKET_DATE_MISMATCH"
    assert rejected["rows"] == []


async def verify_pipeline_and_maintenance_stop_on_failure() -> None:
    rejected = {
        "ok": False,
        "errorCode": "MARKET_DATE_MISMATCH",
        "error": "測試日期不一致",
        "rows": [],
        "primaryMarketDates": {
            "TWSE": "2026-07-22",
            "TPEx": "2026-07-23",
        },
        "finalMarketDates": {
            "TWSE": "2026-07-22",
            "TPEx": "2026-07-23",
        },
        "referenceDate": "2026-07-23",
        "targetDate": "2026-07-23",
        "fallbackUsed": False,
        "fallbackMarkets": [],
        "fallbackAttempts": [],
        "primaryErrors": {},
        "dataIntegrity": {
            "allMarketsPresent": True,
            "allMarketsSameDate": False,
            "matchesReferenceDate": False,
        },
    }

    async def initialize():
        return {"ok": True}

    async def rejected_snapshot():
        return rejected

    async def must_not_run(*args, **kwargs):
        raise AssertionError("failure path performed a forbidden write/finalize")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                pipeline.stock_database_service,
                "initialize",
                initialize,
            )
        )
        stack.enter_context(
            patch.object(
                pipeline,
                "fetch_official_daily_snapshot_with_fallback",
                rejected_snapshot,
            )
        )
        stack.enter_context(
            patch.object(
                pipeline.stock_repository,
                "upsert_securities",
                must_not_run,
            )
        )
        stack.enter_context(
            patch.object(
                pipeline.stock_repository,
                "bulk_upsert_daily_bars",
                must_not_run,
            )
        )
        update = await pipeline.update_official_daily()
    assert update["ok"] is False
    assert update["barsWritten"] == 0

    async def failed_update(**kwargs):
        return {
            "ok": False,
            "errorCode": "MARKET_DATE_MISMATCH",
            "hasMore": False,
            "remainingSymbols": 0,
        }

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(maintenance, "update_official_daily", failed_update)
        )
        stack.enter_context(
            patch.object(
                maintenance,
                "run_full_bullish_radar",
                must_not_run,
            )
        )
        stack.enter_context(
            patch.object(
                maintenance,
                "update_signal_performance",
                must_not_run,
            )
        )
        result = await maintenance.run_daily_maintenance()
    assert result["ok"] is False
    assert result["completed"] is False


async def verify_formal_radar_same_date_gate() -> None:
    class Connection:
        async def fetch(self, query, *args):
            assert query == radar._V12_MARKET_DATES_QUERY
            return [
                {
                    "market_key": "TWSE",
                    "trade_date": date(2026, 7, 22),
                },
                {
                    "market_key": "TPEX",
                    "trade_date": date(2026, 7, 23),
                },
            ]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    with patch.object(
        radar.stock_database,
        "acquire",
        lambda: Acquire(),
    ):
        try:
            await radar._fetch_v12_snapshot()
        except RuntimeError as exc:
            assert "V12_MARKET_DATE_MISMATCH" in str(exc)
        else:
            raise AssertionError("formal radar accepted mismatched dates")


async def main() -> None:
    checks = []
    assert data_sources._normalise_trade_date("1150723") == "2026-07-23"
    assert data_sources._normalise_trade_date("20260723") == "2026-07-23"
    checks.append("日期正規化")

    await verify_fallback_parsers()
    checks.append("TWSE/TPEx 備援解析與品質檢查")

    await verify_resolution_and_rejection()
    checks.append("自動切換備援與失敗拒絕")

    await verify_pipeline_and_maintenance_stop_on_failure()
    checks.append("拒絕資料庫寫入與後續雷達")

    await verify_formal_radar_same_date_gate()
    checks.append("正式雷達同日閘門")

    print(json.dumps({
        "ok": True,
        "checks": checks,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
