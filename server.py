import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


PORT = int(os.environ.get("PORT", "8000"))
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"

mcp = FastMCP(
    "Taiwan Stock MCP",
    instructions=(
        "Use this server to query Taiwan stock real-time quotes, historical K-lines, "
        "moving averages, Bollinger Bands, and basic volume/price technical summaries. "
        "Stock symbols are strings such as 2330, 2313, or 4977."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _validate_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[0-9A-Z]{4,7}", symbol):
        raise ValueError("股票代號格式不正確，例如：2330、2313、4977。")
    return symbol


def _api_key() -> str:
    key = os.environ.get("FUGLE_API_KEY")
    if not key:
        raise RuntimeError("伺服器尚未設定 FUGLE_API_KEY。")
    return key


async def _fugle_get(path: str, params: dict[str, Any] | None = None) -> dict:
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.get(
            f"{FUGLE_BASE_URL}/{path.lstrip('/')}",
            params=params,
            headers={"X-API-KEY": _api_key()},
        )

    if response.status_code == 401:
        raise RuntimeError("Fugle API Key 驗證失敗。")
    if response.status_code == 403:
        raise RuntimeError("目前 Fugle 方案沒有此資料權限。")
    if response.status_code == 404:
        raise ValueError("找不到指定股票或資料。")
    if response.status_code == 429:
        raise RuntimeError("Fugle API 呼叫次數已達上限，請稍後再試。")

    response.raise_for_status()
    return response.json()


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


@mcp.tool()
def ping() -> dict:
    """檢查台股 MCP 伺服器是否正常運作。"""
    return {
        "ok": True,
        "server": "Taiwan Stock MCP v2",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "tools": [
            "get_realtime_quote",
            "get_historical_candles",
            "get_technical_summary",
        ],
    }


@mcp.tool()
async def get_realtime_quote(symbol: str) -> dict:
    """取得台股上市櫃股票即時報價、成交量、內外盤與最佳五檔。"""
    symbol = _validate_symbol(symbol)
    data = await _fugle_get(f"intraday/quote/{symbol}")

    wanted_fields = [
        "date",
        "type",
        "exchange",
        "market",
        "symbol",
        "name",
        "referencePrice",
        "previousClose",
        "openPrice",
        "highPrice",
        "lowPrice",
        "closePrice",
        "lastPrice",
        "avgPrice",
        "change",
        "changePercent",
        "amplitude",
        "bids",
        "asks",
        "total",
        "lastTrade",
        "isLimitDownPrice",
        "isLimitUpPrice",
        "isTrial",
        "isOpen",
        "isClose",
        "lastUpdated",
    ]

    result = {field: data.get(field) for field in wanted_fields if field in data}
    total = data.get("total") or {}
    at_bid = total.get("tradeVolumeAtBid")
    at_ask = total.get("tradeVolumeAtAsk")

    if isinstance(at_bid, (int, float)) and isinstance(at_ask, (int, float)):
        denominator = at_bid + at_ask
        result["bidAskVolumeRatio"] = (
            _round(at_bid / denominator * 100) if denominator else None
        )
        result["interpretation"] = (
            "內盤成交量較高"
            if at_bid > at_ask
            else "外盤成交量較高"
            if at_ask > at_bid
            else "內外盤成交量相同"
        )

    result["source"] = "Fugle MarketData API v1.0"
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_historical_candles(
    symbol: str,
    days: int = 120,
    timeframe: str = "D",
    adjusted: bool = True,
) -> dict:
    """
    取得台股歷史 K 線。
    days 為回溯日曆天數，建議日 K 使用 120 至 365。
    timeframe 可填 D、W、M。
    """
    symbol = _validate_symbol(symbol)
    timeframe = timeframe.upper().strip()

    if timeframe not in {"D", "W", "M"}:
        raise ValueError("timeframe 僅支援 D（日）、W（週）、M（月）。")
    if not 5 <= days <= 365:
        raise ValueError("days 請填 5 到 365。")

    today = date.today()
    start = today - timedelta(days=days)

    data = await _fugle_get(
        f"historical/candles/{symbol}",
        params={
            "from": start.isoformat(),
            "to": today.isoformat(),
            "timeframe": timeframe,
            "adjusted": str(adjusted).lower(),
            "fields": "open,high,low,close,volume,turnover,change",
            "sort": "asc",
        },
    )

    return {
        "symbol": data.get("symbol", symbol),
        "type": data.get("type"),
        "exchange": data.get("exchange"),
        "market": data.get("market"),
        "timeframe": data.get("timeframe", timeframe),
        "adjusted": adjusted,
        "data": data.get("data", []),
        "source": "Fugle MarketData API v1.0",
        "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
async def get_technical_summary(symbol: str, days: int = 180) -> dict:
    """
    取得台股日 K 技術摘要，包括 5/10/20/60 日均線、20 日布林通道、
    近期成交量、量比、區間高低與均線排列。
    """
    symbol = _validate_symbol(symbol)
    if not 90 <= days <= 365:
        raise ValueError("days 請填 90 到 365，建議 180。")

    today = date.today()
    start = today - timedelta(days=days)

    raw = await _fugle_get(
        f"historical/candles/{symbol}",
        params={
            "from": start.isoformat(),
            "to": today.isoformat(),
            "timeframe": "D",
            "adjusted": "true",
            "fields": "open,high,low,close,volume,turnover,change",
            "sort": "asc",
        },
    )

    candles = raw.get("data", [])
    if len(candles) < 60:
        raise RuntimeError("歷史資料不足 60 個交易日，暫時無法計算完整技術摘要。")

    closes = [float(item["close"]) for item in candles if item.get("close") is not None]
    volumes = [float(item["volume"]) for item in candles if item.get("volume") is not None]
    highs = [float(item["high"]) for item in candles if item.get("high") is not None]
    lows = [float(item["low"]) for item in candles if item.get("low") is not None]

    if len(closes) < 60 or len(volumes) < 20:
        raise RuntimeError("有效歷史資料不足，無法計算技術指標。")

    last_close = closes[-1]
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    ma20 = mean(closes[-20:])
    ma60 = mean(closes[-60:])

    bb_mid = ma20
    bb_std = pstdev(closes[-20:])
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = ((bb_upper - bb_lower) / bb_mid * 100) if bb_mid else None
    bb_position = (
        ((last_close - bb_lower) / (bb_upper - bb_lower) * 100)
        if bb_upper != bb_lower
        else None
    )

    avg_volume_5 = mean(volumes[-5:])
    avg_volume_20 = mean(volumes[-20:])
    latest_volume = volumes[-1]
    volume_ratio_20 = latest_volume / avg_volume_20 if avg_volume_20 else None

    if ma5 > ma10 > ma20 > ma60:
        alignment = "多頭排列"
    elif ma5 < ma10 < ma20 < ma60:
        alignment = "空頭排列"
    else:
        alignment = "均線糾結或非典型排列"

    if last_close > bb_upper:
        bollinger_state = "站上布林上軌"
    elif last_close < bb_lower:
        bollinger_state = "跌破布林下軌"
    elif last_close >= bb_mid:
        bollinger_state = "位於布林中軌與上軌之間"
    else:
        bollinger_state = "位於布林下軌與中軌之間"

    latest = candles[-1]
    prior = candles[-2]

    return {
        "symbol": raw.get("symbol", symbol),
        "latestDate": latest.get("date"),
        "latestClose": _round(last_close),
        "latestChange": latest.get("change"),
        "previousClose": prior.get("close"),
        "movingAverages": {
            "MA5": _round(ma5),
            "MA10": _round(ma10),
            "MA20": _round(ma20),
            "MA60": _round(ma60),
            "alignment": alignment,
            "distanceFromMA20Percent": _round((last_close / ma20 - 1) * 100),
            "distanceFromMA60Percent": _round((last_close / ma60 - 1) * 100),
        },
        "bollingerBands20": {
            "upper": _round(bb_upper),
            "middle": _round(bb_mid),
            "lower": _round(bb_lower),
            "widthPercent": _round(bb_width),
            "positionPercent": _round(bb_position),
            "state": bollinger_state,
        },
        "volume": {
            "latest": int(latest_volume),
            "average5": int(avg_volume_5),
            "average20": int(avg_volume_20),
            "volumeRatio20": _round(volume_ratio_20),
            "interpretation": (
                "明顯放量"
                if volume_ratio_20 is not None and volume_ratio_20 >= 1.5
                else "量能高於 20 日均量"
                if volume_ratio_20 is not None and volume_ratio_20 >= 1
                else "量縮"
            ),
        },
        "ranges": {
            "high20": _round(max(highs[-20:])),
            "low20": _round(min(lows[-20:])),
            "high60": _round(max(highs[-60:])),
            "low60": _round(min(lows[-60:])),
        },
        "dataPoints": len(candles),
        "source": "Fugle MarketData API v1.0",
        "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
