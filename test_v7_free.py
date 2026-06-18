import asyncio
import importlib.util
import sys
import types
from pathlib import Path


class FakeFastMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        def decorator(func):
            return func
        return decorator

    def run(self, *args, **kwargs):
        return None


mcp_mod = types.ModuleType("mcp")
server_mod = types.ModuleType("mcp.server")
fast_mod = types.ModuleType("mcp.server.fastmcp")
fast_mod.FastMCP = FakeFastMCP
sys.modules.setdefault("mcp", mcp_mod)
sys.modules.setdefault("mcp.server", server_mod)
sys.modules.setdefault("mcp.server.fastmcp", fast_mod)

spec = importlib.util.spec_from_file_location(
    "twstock_server",
    Path(__file__).with_name("server.py"),
)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def make_rows(market: str, count: int, trade_date: str):
    start = 1000 if market == "TSE" else 5000
    rows = []
    for index in range(count):
        symbol = str(start + index).zfill(4)
        close = 30 + index % 100
        change = ((index % 9) - 2) / 2
        prior = close - change
        rows.append({
            "symbol": symbol,
            "name": f"股票{symbol}",
            "market": market,
            "openPrice": close - 0.2,
            "highPrice": close + 1,
            "lowPrice": close - 1,
            "closePrice": close,
            "change": change,
            "changePercent": change / prior * 100 if prior else 0,
            "tradeVolume": 1_000_000 + index,
            "tradeValue": 150_000_000 + index * 1000,
            "date": trade_date,
        })
    return rows


def payload(market: str, trade_date: str, count: int):
    return {
        "market": market,
        "date": trade_date,
        "data": make_rows(market, count, trade_date),
        "source": market,
        "fetchedAtTaipei": "2026-06-18T18:00:00+08:00",
    }


def test_date_normalization():
    assert module._normalize_trade_date("1150618") == "2026-06-18"
    assert module._normalize_trade_date("115/06/18") == "2026-06-18"
    assert module._normalize_trade_date("20260618") == "2026-06-18"


def test_upsert_candle():
    candles = [
        {"date": "2026-06-16", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100},
        {"date": "2026-06-17", "open": 11, "high": 12, "low": 10, "close": 11, "volume": 200},
    ]
    quote = {
        "symbol": "2330",
        "openPrice": 12,
        "highPrice": 14,
        "lowPrice": 11,
        "closePrice": 13,
        "change": 2,
        "tradeVolume": 300,
        "tradeValue": 3900,
    }
    merged = module._upsert_official_candle(candles, quote, "2026-06-18")
    assert merged[-1]["date"] == "2026-06-18"
    assert merged[-1]["close"] == 13
    assert len(merged) == 3


def test_date_mismatch_refuses():
    async def fake_market(market, force_refresh=False):
        return payload(market, "2026-06-18" if market == "TSE" else "2026-06-17", 10)

    module._get_official_market_quotes = fake_market
    result = asyncio.run(module.screen_market(candidate_limit=10, top_n=5))
    assert result["ok"] is False
    assert result["errorCode"] == "MARKET_DATE_MISMATCH"


def test_latest_date_mismatch_refuses():
    async def fake_market(market, force_refresh=False):
        return payload(market, "2026-06-17", 10)

    async def fake_reference(force_refresh=False):
        return "2026-06-18"

    module._get_official_market_quotes = fake_market
    module._get_reference_market_date = fake_reference
    result = asyncio.run(module.screen_market(candidate_limit=10, top_n=5))
    assert result["ok"] is False
    assert result["errorCode"] == "OFFICIAL_DATA_NOT_LATEST"


def test_successful_screen():
    module.V7_MIN_UNIVERSE_COUNT = 20

    async def fake_market(market, force_refresh=False):
        return payload(market, "2026-06-18", 15)

    async def fake_reference(force_refresh=False):
        return "2026-06-18"

    async def fake_analyze(snapshot, strategy, scoring_date, semaphore, force_refresh=False):
        return {
            "symbol": snapshot["symbol"],
            "name": snapshot["name"],
            "market": snapshot["market"],
            "quoteDate": scoring_date,
            "closePrice": snapshot["closePrice"],
            "changePercent": snapshot["changePercent"],
            "tradeVolume": snapshot["tradeVolume"],
            "tradeValue": snapshot["tradeValue"],
            "score": float(snapshot["closePrice"]),
            "reasons": [],
            "risks": [],
            "technical": {"latestDate": scoring_date},
        }

    async def fake_chip(symbol, days=45, force_refresh=False):
        return {"available": True, "scoreAdjustment": 1, "reasons": [], "risks": []}

    module._get_official_market_quotes = fake_market
    module._get_reference_market_date = fake_reference
    module._analyze_candidate = fake_analyze
    module._safe_chip_for_ranking = fake_chip

    result = asyncio.run(module.screen_market(
        strategy="early_stage",
        markets="BOTH",
        top_n=5,
        candidate_limit=10,
        include_chip=True,
    ))
    assert result["ok"] is True
    assert result["scoringDate"] == "2026-06-18"
    assert result["allUniverseUsingSameDate"] is True
    assert result["marketUniverseCount"] == 30
    assert result["deepAnalyzedCount"] == 10
    assert result["chipAnalyzedCount"] == 5
    assert len(result["results"]) == 5
    assert all(item["technical"]["latestDate"] == "2026-06-18" for item in result["results"])


if __name__ == "__main__":
    tests = [
        test_date_normalization,
        test_upsert_candle,
        test_date_mismatch_refuses,
        test_latest_date_mismatch_refuses,
        test_successful_screen,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
