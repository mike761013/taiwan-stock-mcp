from __future__ import annotations

import asyncio
from datetime import date, timedelta

from stock_db.indicators import calculate_indicators
from stock_db.service import StockDatabaseService


class FakeRepository:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol
        self.requested_symbols = []
        self.requested_limit = None
        self.written_rows = []

    async def get_recent_daily_bars_for_symbols(
        self,
        symbols,
        limit_per_symbol=61,
    ):
        self.requested_symbols = list(symbols)
        self.requested_limit = limit_per_symbol
        return {
            symbol: list(self.rows_by_symbol.get(symbol, []))[-limit_per_symbol:]
            for symbol in symbols
        }

    async def bulk_upsert_indicators(self, rows):
        self.written_rows = list(rows)
        return len(self.written_rows)


def make_bars(symbol: str, count: int = 90):
    start = date(2026, 1, 1)
    return [
        {
            "symbol": symbol,
            "trade_date": start + timedelta(days=index),
            "open": 99 + index,
            "high": 101 + index,
            "low": 98 + index,
            "close": 100 + index,
            "volume": 1_000_000 + index * 1_000,
            "turnover": 100_000_000 + index * 100_000,
            "change_percent": 1,
            "source": "test",
        }
        for index in range(count)
    ]


def test_bulk_latest_matches_full_history_latest_values() -> None:
    rows_2330 = make_bars("2330")
    rows_4939 = make_bars("4939")
    repository = FakeRepository({
        "2330": rows_2330,
        "4939": rows_4939,
    })
    service = StockDatabaseService(repository=repository)

    result = asyncio.run(service.calculate_latest_indicators_bulk(
        ["2330", "4939"],
        lookback_bars=61,
    ))

    assert result["ok"] is True
    assert result["processedSymbols"] == 2
    assert result["failedSymbols"] == 0
    assert result["indicatorRowsWritten"] == 2
    assert repository.requested_symbols == ["2330", "4939"]
    assert repository.requested_limit == 61

    written = {
        row.symbol: row
        for row in repository.written_rows
    }
    expected = calculate_indicators(rows_2330)[-1]
    actual = written["2330"]
    assert actual.trade_date == expected["trade_date"]
    for field in (
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
    ):
        assert actual.values[field] == expected[field]


def test_bulk_latest_reports_symbol_without_bars() -> None:
    repository = FakeRepository({"2330": make_bars("2330")})
    service = StockDatabaseService(repository=repository)

    result = asyncio.run(service.calculate_latest_indicators_bulk(
        ["2330", "9999"],
    ))

    assert result["ok"] is False
    assert result["processedSymbols"] == 1
    assert result["failedSymbols"] == 1
    assert result["indicatorRowsWritten"] == 1
    assert result["failures"][0]["symbol"] == "9999"
