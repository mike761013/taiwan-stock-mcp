import asyncio
import importlib.util
import sys
import types
from contextlib import contextmanager
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
    "twstock_server_v71",
    Path(__file__).with_name("server.py"),
)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


@contextmanager
def patched(**replacements):
    originals = {name: getattr(module, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in originals.items():
            setattr(module, name, value)


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


def payload(market: str, trade_date: str, count: int, *, fallback=False):
    result = {
        "market": market,
        "date": trade_date,
        "data": make_rows(market, count, trade_date),
        "source": f"{market}-{'fallback' if fallback else 'primary'}",
        "fetchedAtTaipei": "2026-06-18T18:00:00+08:00",
    }
    if fallback:
        result.update({
            "fallback": True,
            "fallbackTargetDate": trade_date,
            "validation": {
                "count": count,
                "uniqueSymbols": count,
                "ohlcCoverage": 1.0,
                "liquidityCoverage": 1.0,
            },
        })
    return result


def twse_fixture(count: int = 3):
    data = []
    for index in range(count):
        symbol = str(1101 + index)
        data.append([
            symbol,
            f"上市{symbol}",
            "1,000,000",
            "1,000",
            "50,000,000",
            "49.0",
            "51.0",
            "48.5",
            "50.0",
            "+",
            "1.0",
        ])
    return {
        "stat": "OK",
        "date": "20260618",
        "title": "115年06月18日 每日收盤行情",
        "tables": [{
            "fields": [
                "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
                "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差",
            ],
            "data": data,
        }],
    }


def tpex_fixture(count: int = 3):
    data = []
    for index in range(count):
        symbol = str(5001 + index)
        data.append([
            symbol,
            f"上櫃{symbol}",
            "40.0",
            "+0.5",
            "39.5",
            "41.0",
            "39.0",
            "800,000",
            "32,000,000",
        ])
    return {
        "status": "success",
        "date": "2026/06/18",
        "tables": [{
            "date": "115/06/18",
            "fields": [
                "代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低",
                "成交股數", "成交金額",
            ],
            "data": data,
        }],
    }


def test_date_normalization():
    assert module._normalize_trade_date("1150618") == "2026-06-18"
    assert module._normalize_trade_date("115/06/18") == "2026-06-18"
    assert module._normalize_trade_date("20260618") == "2026-06-18"
    assert module._normalize_trade_date("115年06月18日 每日收盤行情") == "2026-06-18"


def test_twse_fallback_parser():
    records = module._official_table_records(twse_fixture())
    assert len(records) == 3
    row = module._fallback_record_to_official_row(records[0], "TSE", "2026-06-18")
    assert row["symbol"] == "1101"
    assert row["closePrice"] == 50.0
    assert row["change"] == 1.0
    assert row["tradeVolume"] == 1_000_000
    assert row["date"] == "2026-06-18"


def test_tpex_fallback_parser():
    records = module._official_table_records(tpex_fixture())
    assert len(records) == 3
    row = module._fallback_record_to_official_row(records[0], "OTC", "2026-06-18")
    assert row["symbol"] == "5001"
    assert row["closePrice"] == 40.0
    assert row["change"] == 0.5
    assert row["tradeValue"] == 32_000_000


def test_twse_fallback_fetch_validation():
    async def fake_http(url, cache_tag, force_refresh=False):
        assert "MI_INDEX" in url
        assert "date=20260618" in url
        assert cache_tag == "fallback:TSE:2026-06-18"
        return twse_fixture(3)

    with patched(_official_http_json=fake_http, V7_MIN_TSE_COUNT=3):
        result = asyncio.run(module._get_official_market_quotes_fallback(
            "TSE", "2026-06-18", force_refresh=True
        ))

    assert result["fallback"] is True
    assert result["date"] == "2026-06-18"
    assert result["validation"]["count"] == 3
    assert result["validation"]["ohlcCoverage"] == 1.0


def test_tpex_fallback_fetch_validation():
    async def fake_http(url, cache_tag, force_refresh=False):
        assert "dailyQuotes" in url
        assert cache_tag == "fallback:OTC:2026-06-18"
        return tpex_fixture(3)

    with patched(_official_http_json=fake_http, V7_MIN_OTC_COUNT=3):
        result = asyncio.run(module._get_official_market_quotes_fallback(
            "OTC", "2026-06-18", force_refresh=True
        ))

    assert result["fallback"] is True
    assert result["date"] == "2026-06-18"
    assert result["validation"]["count"] == 3
    assert result["validation"]["liquidityCoverage"] == 1.0


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


def test_market_date_mismatch_uses_fallback():
    module.V7_MIN_UNIVERSE_COUNT = 20

    async def fake_primary(market, force_refresh=False):
        return payload(market, "2026-06-17" if market == "TSE" else "2026-06-18", 15)

    async def fake_reference(force_refresh=False):
        return "2026-06-18"

    async def fake_fallback(market, target_date, force_refresh=False):
        assert market == "TSE"
        assert target_date == "2026-06-18"
        return payload(market, target_date, 15, fallback=True)

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

    with patched(
        _get_official_market_quotes=fake_primary,
        _get_reference_market_date=fake_reference,
        _get_official_market_quotes_fallback=fake_fallback,
        _analyze_candidate=fake_analyze,
    ):
        result = asyncio.run(module.screen_market(candidate_limit=10, top_n=5))

    assert result["ok"] is True
    assert result["fallbackUsed"] is True
    assert result["fallbackMarkets"] == ["TSE"]
    assert result["primaryMarketDates"]["TSE"] == "2026-06-17"
    assert result["finalMarketDates"]["TSE"] == "2026-06-18"
    assert result["allUniverseUsingSameDate"] is True


def test_fallback_failure_refuses():
    async def fake_primary(market, force_refresh=False):
        return payload(market, "2026-06-17" if market == "TSE" else "2026-06-18", 15)

    async def fake_reference(force_refresh=False):
        return "2026-06-18"

    async def fake_fallback(market, target_date, force_refresh=False):
        raise RuntimeError("備援尚未發布")

    with patched(
        _get_official_market_quotes=fake_primary,
        _get_reference_market_date=fake_reference,
        _get_official_market_quotes_fallback=fake_fallback,
    ):
        result = asyncio.run(module.screen_market(candidate_limit=10, top_n=5))

    assert result["ok"] is False
    assert result["errorCode"] == "MARKET_DATE_MISMATCH"
    assert result["fallbackAttempts"][0]["error"] == "備援尚未發布"


def test_latest_date_mismatch_refuses_after_fallback_failure():
    async def fake_primary(market, force_refresh=False):
        return payload(market, "2026-06-17", 15)

    async def fake_reference(force_refresh=False):
        return "2026-06-18"

    async def fake_fallback(market, target_date, force_refresh=False):
        raise RuntimeError("指定日期資料尚未更新")

    with patched(
        _get_official_market_quotes=fake_primary,
        _get_reference_market_date=fake_reference,
        _get_official_market_quotes_fallback=fake_fallback,
    ):
        result = asyncio.run(module.screen_market(candidate_limit=10, top_n=5))

    assert result["ok"] is False
    assert result["errorCode"] == "OFFICIAL_DATA_NOT_LATEST"


def test_successful_screen_without_fallback():
    module.V7_MIN_UNIVERSE_COUNT = 20

    async def fake_primary(market, force_refresh=False):
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

    with patched(
        _get_official_market_quotes=fake_primary,
        _get_reference_market_date=fake_reference,
        _analyze_candidate=fake_analyze,
        _safe_chip_for_ranking=fake_chip,
    ):
        result = asyncio.run(module.screen_market(
            strategy="early_stage",
            markets="BOTH",
            top_n=5,
            candidate_limit=10,
            include_chip=True,
        ))

    assert result["ok"] is True
    assert result["serverVersion"] == "v7.1-free-official-fallback"
    assert result["scoringDate"] == "2026-06-18"
    assert result["fallbackUsed"] is False
    assert result["marketUniverseCount"] == 30
    assert result["deepAnalyzedCount"] == 10
    assert result["chipAnalyzedCount"] == 5
    assert len(result["results"]) == 5
    assert all(item["technical"]["latestDate"] == "2026-06-18" for item in result["results"])


if __name__ == "__main__":
    tests = [
        test_date_normalization,
        test_twse_fallback_parser,
        test_tpex_fallback_parser,
        test_twse_fallback_fetch_validation,
        test_tpex_fallback_fetch_validation,
        test_upsert_candle,
        test_market_date_mismatch_uses_fallback,
        test_fallback_failure_refuses,
        test_latest_date_mismatch_refuses_after_fallback_failure,
        test_successful_screen_without_fallback,
    ]
    for test in tests:
        test()
        print("PASS", test.__name__)
