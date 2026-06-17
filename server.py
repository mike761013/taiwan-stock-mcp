import os
import re
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP


PORT = int(os.environ.get("PORT", "8000"))

mcp = FastMCP(
    "Taiwan Stock MCP",
    instructions=(
        "Use this server to query Taiwan stock real-time quotes. "
        "Stock symbols are strings such as 2330, 2313, or 4977."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def ping() -> dict:
    """檢查台股 MCP 伺服器是否正常運作。"""
    return {
        "ok": True,
        "server": "Taiwan Stock MCP",
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
async def get_realtime_quote(symbol: str) -> dict:
    """取得台股上市櫃股票即時報價。symbol 請填股票代號，例如 2330。"""
    symbol = symbol.strip().upper()

    if not re.fullmatch(r"[0-9A-Z]{4,7}", symbol):
        raise ValueError("股票代號格式不正確，例如：2330、2313、4977。")

    api_key = os.environ.get("FUGLE_API_KEY")
    if not api_key:
        raise RuntimeError("伺服器尚未設定 FUGLE_API_KEY。")

    url = (
        "https://api.fugle.tw/marketdata/v1.0/stock/"
        f"intraday/quote/{symbol}"
    )

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            url,
            headers={"X-API-KEY": api_key},
        )

    if response.status_code == 401:
        raise RuntimeError("Fugle API Key 驗證失敗。")
    if response.status_code == 403:
        raise RuntimeError("目前 Fugle 方案沒有此資料權限。")
    if response.status_code == 404:
        raise ValueError(f"找不到股票代號 {symbol}。")
    if response.status_code == 429:
        raise RuntimeError("Fugle API 呼叫次數已達上限，請稍後再試。")

    response.raise_for_status()
    data = response.json()

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
        "avgPrice",
        "change",
        "changePercent",
        "total",
        "lastTrade",
        "lastTrial",
        "isTrial",
        "isDelayedOpen",
        "isDelayedClose",
    ]

    result = {field: data.get(field) for field in wanted_fields if field in data}
    result["source"] = "Fugle MarketData API v1.0"
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
