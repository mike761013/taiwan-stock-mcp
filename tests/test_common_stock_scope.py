import asyncio

from stock_db import pipeline
from stock_db.pipeline import is_listed_otc_common_stock
from stock_db.radar import (
    _COMMON_STOCK_UNIVERSE_COUNT_QUERY,
    _V12_SNAPSHOT_QUERY,
)


def test_common_stock_scope_includes_twse_and_tpex_companies() -> None:
    assert is_listed_otc_common_stock("2330", "TWSE") is True
    assert is_listed_otc_common_stock("4939", "TPEx") is True
    assert is_listed_otc_common_stock("6488", "OTC") is True


def test_common_stock_scope_excludes_funds_bonds_and_preferred_shares() -> None:
    assert is_listed_otc_common_stock("0050", "TWSE") is False
    assert is_listed_otc_common_stock("00679B", "TWSE") is False
    assert is_listed_otc_common_stock("01002T", "TWSE") is False
    assert is_listed_otc_common_stock("1101B", "TWSE") is False
    assert is_listed_otc_common_stock("2330", "UNKNOWN") is False


def test_v12_snapshot_and_universe_count_share_the_same_scope() -> None:
    predicate = "s.symbol ~ '^[1-9][0-9]{3}$'"
    market_filter = "UPPER(s.market) IN ('TWSE', 'TPEX', 'OTC')"
    assert predicate in _V12_SNAPSHOT_QUERY
    assert predicate in _COMMON_STOCK_UNIVERSE_COUNT_QUERY
    assert market_filter in _V12_SNAPSHOT_QUERY
    assert market_filter in _COMMON_STOCK_UNIVERSE_COUNT_QUERY


def test_daily_update_continuation_reuses_committed_snapshot(monkeypatch) -> None:
    async def initialize():
        return {"ok": True}

    async def fail_if_snapshot_is_downloaded():
        raise AssertionError("continuation must not download the snapshot again")

    async def latest_common_stock_symbols():
        return ["1101", "2330"]

    async def calculate_symbol_indicators(symbol, latest_only=False):
        assert symbol == "2330"
        assert latest_only is True
        return {"processed": 1}

    monkeypatch.setattr(pipeline.stock_database_service, "initialize", initialize)
    monkeypatch.setattr(
        pipeline,
        "fetch_official_daily_snapshot",
        fail_if_snapshot_is_downloaded,
    )
    monkeypatch.setattr(
        pipeline,
        "_latest_common_stock_symbols",
        latest_common_stock_symbols,
    )
    monkeypatch.setattr(
        pipeline.stock_database_service,
        "calculate_symbol_indicators",
        calculate_symbol_indicators,
    )

    result = asyncio.run(pipeline.update_official_daily(start_after="1101"))

    assert result["ok"] is True
    assert result["scope"] == "TWSE_TPEX_COMMON_STOCKS"
    assert result["snapshotRefreshed"] is False
    assert result["rawRowsFetched"] == 0
    assert result["rowsFetched"] == 0
    assert result["barsWritten"] == 0
    assert result["universeCount"] == 2
    assert result["indicatorSymbols"] == 1
    assert result["remainingSymbols"] == 0


def test_daily_update_persists_only_listed_otc_common_stocks(monkeypatch) -> None:
    captured_security_symbols = []
    captured_bar_symbols = []

    async def initialize():
        return {"ok": True}

    async def fetch_snapshot():
        base = {
            "date": "2026-07-22",
            "open": 100,
            "high": 102,
            "low": 99,
            "close": 101,
            "volume": 3_000_000,
            "turnover": 303_000_000,
            "change_percent": 1,
            "source": "official",
        }
        return [
            {**base, "symbol": "2330", "name": "台積電", "market": "TWSE"},
            {**base, "symbol": "4939", "name": "亞電", "market": "TPEx"},
            {**base, "symbol": "0050", "name": "元大台灣50", "market": "TWSE"},
            {**base, "symbol": "00679B", "name": "元大美債20年", "market": "TWSE"},
            {**base, "symbol": "1101B", "name": "測試特別股", "market": "TWSE"},
        ]

    async def upsert_securities(securities):
        captured_security_symbols.extend(item.symbol for item in securities)
        return len(securities)

    async def upsert_bars(bars):
        captured_bar_symbols.extend(item.symbol for item in bars)
        return len(bars)

    async def latest_common_stock_symbols():
        return ["2330", "4939"]

    async def calculate_symbol_indicators(symbol, latest_only=False):
        assert symbol in {"2330", "4939"}
        assert latest_only is True
        return {"processed": 1}

    monkeypatch.setattr(pipeline.stock_database_service, "initialize", initialize)
    monkeypatch.setattr(pipeline, "fetch_official_daily_snapshot", fetch_snapshot)
    monkeypatch.setattr(
        pipeline.stock_repository,
        "upsert_securities",
        upsert_securities,
    )
    monkeypatch.setattr(
        pipeline.stock_repository,
        "bulk_upsert_daily_bars",
        upsert_bars,
    )
    monkeypatch.setattr(
        pipeline,
        "_latest_common_stock_symbols",
        latest_common_stock_symbols,
    )
    monkeypatch.setattr(
        pipeline.stock_database_service,
        "calculate_symbol_indicators",
        calculate_symbol_indicators,
    )

    result = asyncio.run(pipeline.update_official_daily(batch_size=10))

    assert result["ok"] is True
    assert result["scope"] == "TWSE_TPEX_COMMON_STOCKS"
    assert result["rawRowsFetched"] == 5
    assert result["rowsFetched"] == 2
    assert result["barsWritten"] == 2
    assert captured_security_symbols == ["2330", "4939"]
    assert captured_bar_symbols == ["2330", "4939"]
    assert result["indicatorSymbols"] == 2
