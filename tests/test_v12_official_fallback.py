import asyncio
from datetime import date

import pytest

from stock_db import data_sources, maintenance, pipeline, radar


def _row(symbol: str, market: str, trade_date: str) -> dict:
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


def _market_snapshot(
    market: str,
    trade_date: str,
    *,
    fallback: bool = False,
) -> dict:
    symbol = "1101" if market == "TWSE" else "5001"
    return {
        "market": market,
        "date": trade_date,
        "rows": [_row(symbol, market, trade_date)],
        "source": f"{market} {'fallback' if fallback else 'primary'}",
        "validation": {
            "count": 1,
            "uniqueSymbols": 1,
            "ohlcCoverage": 1.0,
            "liquidityCoverage": 1.0,
        },
        "fallback": fallback,
    }


def _twse_fallback_fixture() -> dict:
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


def _tpex_fallback_fixture() -> dict:
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


def test_official_date_normalisation_supports_roc_and_gregorian() -> None:
    assert data_sources._normalise_trade_date("1150723") == "2026-07-23"
    assert data_sources._normalise_trade_date("115/07/23") == "2026-07-23"
    assert data_sources._normalise_trade_date("20260723") == "2026-07-23"
    assert data_sources._normalise_trade_date(
        "115年07月23日 每日收盤行情"
    ) == "2026-07-23"


def test_primary_tpex_source_fits_database_column(monkeypatch) -> None:
    async def fake_get_json(url, **kwargs):
        assert "tpex_mainboard_daily_close_quotes" in url
        return [{
            "Date": "20260723",
            "Code": "5001",
            "Name": "測試上櫃",
            "OpeningPrice": "39.5",
            "HighestPrice": "41.0",
            "LowestPrice": "39.0",
            "ClosingPrice": "40.0",
            "TradeVolume": "800000",
            "TradeValue": "32000000",
        }]

    monkeypatch.setattr(data_sources, "_get_json", fake_get_json)

    snapshot = asyncio.run(
        data_sources._fetch_primary_market_snapshot("TPEx")
    )

    assert len(snapshot["source"]) <= 32
    assert len(snapshot["rows"][0]["source"]) <= 32


def test_twse_and_tpex_fallback_payloads_are_parsed_and_validated(
    monkeypatch,
) -> None:
    async def fake_get_json(url, **kwargs):
        if "MI_INDEX" in url:
            return _twse_fallback_fixture()
        if "dailyQuotes" in url:
            return _tpex_fallback_fixture()
        raise AssertionError(url)

    monkeypatch.setattr(data_sources, "_get_json", fake_get_json)
    monkeypatch.setattr(data_sources, "V12_MIN_TWSE_COMMON_STOCKS", 1)
    monkeypatch.setattr(data_sources, "V12_MIN_TPEX_COMMON_STOCKS", 1)

    twse = asyncio.run(
        data_sources._fetch_fallback_market_snapshot(
            "TWSE",
            "2026-07-23",
        )
    )
    tpex = asyncio.run(
        data_sources._fetch_fallback_market_snapshot(
            "TPEx",
            "2026-07-23",
        )
    )

    assert twse["fallback"] is True
    assert twse["rows"][0]["symbol"] == "1101"
    assert twse["rows"][0]["volume"] == 1_000_000
    assert tpex["fallback"] is True
    assert tpex["rows"][0]["symbol"] == "5001"
    assert tpex["rows"][0]["change_percent"] == 0.5


def test_market_date_mismatch_automatically_uses_fallback(
    monkeypatch,
) -> None:
    async def primary(market):
        trade_date = "2026-07-22" if market == "TWSE" else "2026-07-23"
        return _market_snapshot(market, trade_date)

    async def reference():
        return "2026-07-23"

    async def fallback(market, target_date):
        assert market == "TWSE"
        assert target_date == "2026-07-23"
        return _market_snapshot(market, target_date, fallback=True)

    monkeypatch.setattr(
        data_sources,
        "_fetch_primary_market_snapshot",
        primary,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_reference_trade_date",
        reference,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_fallback_market_snapshot",
        fallback,
    )

    result = asyncio.run(
        data_sources.fetch_official_daily_snapshot_with_fallback()
    )

    assert result["ok"] is True
    assert result["fallbackUsed"] is True
    assert result["fallbackMarkets"] == ["TWSE"]
    assert result["primaryMarketDates"]["TWSE"] == "2026-07-22"
    assert result["finalMarketDates"] == {
        "TWSE": "2026-07-23",
        "TPEx": "2026-07-23",
    }
    assert result["dataIntegrity"]["allMarketsSameDate"] is True
    assert len(result["rows"]) == 2


def test_equally_stale_primary_feeds_use_reference_date_for_both_fallbacks(
    monkeypatch,
) -> None:
    async def primary(market):
        return _market_snapshot(market, "2026-07-22")

    async def reference():
        return "2026-07-23"

    async def fallback(market, target_date):
        return _market_snapshot(market, target_date, fallback=True)

    monkeypatch.setattr(
        data_sources,
        "_fetch_primary_market_snapshot",
        primary,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_reference_trade_date",
        reference,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_fallback_market_snapshot",
        fallback,
    )

    result = asyncio.run(
        data_sources.fetch_official_daily_snapshot_with_fallback()
    )

    assert result["ok"] is True
    assert result["fallbackMarkets"] == ["TWSE", "TPEx"]
    assert set(result["finalMarketDates"].values()) == {"2026-07-23"}


def test_existing_fallback_environment_switch_is_now_effective(
    monkeypatch,
) -> None:
    async def primary(market):
        trade_date = "2026-07-22" if market == "TWSE" else "2026-07-23"
        return _market_snapshot(market, trade_date)

    async def reference():
        return "2026-07-23"

    async def must_not_fetch_fallback(market, target_date):
        raise AssertionError("disabled fallback must not be called")

    monkeypatch.setenv("STOCK_DB_FALLBACK_ENABLED", "false")
    monkeypatch.setattr(
        data_sources,
        "_fetch_primary_market_snapshot",
        primary,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_reference_trade_date",
        reference,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_fallback_market_snapshot",
        must_not_fetch_fallback,
    )

    result = asyncio.run(
        data_sources.fetch_official_daily_snapshot_with_fallback()
    )

    assert result["ok"] is False
    assert result["fallbackEnabled"] is False
    assert result["fallbackAttempts"] == []


def test_failed_fallback_refuses_rows_and_database_write(
    monkeypatch,
) -> None:
    async def primary(market):
        trade_date = "2026-07-22" if market == "TWSE" else "2026-07-23"
        return _market_snapshot(market, trade_date)

    async def reference():
        return "2026-07-23"

    async def fallback(market, target_date):
        raise RuntimeError("備援尚未發布")

    monkeypatch.setattr(
        data_sources,
        "_fetch_primary_market_snapshot",
        primary,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_reference_trade_date",
        reference,
    )
    monkeypatch.setattr(
        data_sources,
        "_fetch_fallback_market_snapshot",
        fallback,
    )

    resolved = asyncio.run(
        data_sources.fetch_official_daily_snapshot_with_fallback()
    )

    assert resolved["ok"] is False
    assert resolved["errorCode"] == "MARKET_DATE_MISMATCH"
    assert resolved["rows"] == []
    assert "備援尚未發布" in resolved["fallbackAttempts"][0]["error"]

    async def initialize():
        return {"ok": True}

    async def rejected_snapshot():
        return resolved

    async def must_not_write(*args, **kwargs):
        raise AssertionError("rejected snapshot must not be persisted")

    monkeypatch.setattr(
        pipeline.stock_database_service,
        "initialize",
        initialize,
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_official_daily_snapshot_with_fallback",
        rejected_snapshot,
    )
    monkeypatch.setattr(
        pipeline.stock_repository,
        "upsert_securities",
        must_not_write,
    )
    monkeypatch.setattr(
        pipeline.stock_repository,
        "bulk_upsert_daily_bars",
        must_not_write,
    )

    update = asyncio.run(pipeline.update_official_daily())

    assert update["ok"] is False
    assert update["barsWritten"] == 0
    assert update["errorCode"] == "MARKET_DATE_MISMATCH"


def test_daily_maintenance_does_not_finalize_rejected_market_update(
    monkeypatch,
) -> None:
    async def failed_update(**kwargs):
        return {
            "ok": False,
            "errorCode": "MARKET_DATE_MISMATCH",
            "hasMore": False,
            "remainingSymbols": 0,
        }

    async def must_not_run(*args, **kwargs):
        raise AssertionError("radar/performance must not run")

    monkeypatch.setattr(maintenance, "update_official_daily", failed_update)
    monkeypatch.setattr(
        maintenance,
        "run_full_bullish_radar",
        must_not_run,
    )
    monkeypatch.setattr(
        maintenance,
        "update_signal_performance",
        must_not_run,
    )

    result = asyncio.run(maintenance.run_daily_maintenance())

    assert result["ok"] is False
    assert result["completed"] is False
    assert result["stage"] == "market_update"


def test_v12_formal_radar_refuses_mismatched_database_dates(
    monkeypatch,
) -> None:
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

    monkeypatch.setattr(
        radar.stock_database,
        "acquire",
        lambda: Acquire(),
    )

    with pytest.raises(
        RuntimeError,
        match="V12_MARKET_DATE_MISMATCH",
    ):
        asyncio.run(radar._fetch_v12_snapshot())
