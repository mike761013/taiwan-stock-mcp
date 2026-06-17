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
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

mcp = FastMCP(
    "Taiwan Stock MCP",
    instructions=(
        "Use this server to query Taiwan stock real-time quotes, historical K-lines, "
        "moving averages, Bollinger Bands, technical summaries, institutional trading, "
        "margin/short balances, foreign ownership, securities lending, and shareholding distribution. "
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


def _finmind_token() -> str:
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        raise RuntimeError(
            "伺服器尚未設定 FINMIND_TOKEN。請到 FinMind 取得 Token，"
            "並在 Render Environment 新增 FINMIND_TOKEN。"
        )
    return token


async def _finmind_get(
    dataset: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    params = {
        "dataset": dataset,
        "data_id": symbol,
        "start_date": start_date,
        "end_date": end_date,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            FINMIND_URL,
            params=params,
            headers={"Authorization": f"Bearer {_finmind_token()}"},
        )

    if response.status_code in {401, 403}:
        raise RuntimeError(
            "FinMind Token 驗證失敗，或目前會員方案沒有此資料集權限。"
        )
    if response.status_code == 402:
        raise RuntimeError("此 FinMind 資料集需要付費會員方案。")
    if response.status_code == 429:
        raise RuntimeError("FinMind API 呼叫次數已達上限，請稍後再試。")

    response.raise_for_status()
    payload = response.json()

    if payload.get("status") not in (None, 200):
        message = payload.get("msg") or payload.get("message") or "FinMind API 回傳錯誤。"
        raise RuntimeError(str(message))

    data = payload.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("FinMind API 回傳格式不正確。")

    return sorted(data, key=lambda row: str(row.get("date", "")))


def _number(row: dict[str, Any], key: str) -> float:
    value = row.get(key, 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: float) -> int:
    return int(round(value))


def _days_range(days: int, minimum: int = 5, maximum: int = 365) -> tuple[str, str]:
    if not minimum <= days <= maximum:
        raise ValueError(f"days 請填 {minimum} 到 {maximum}。")
    end = date.today()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _institutional_row(row: dict[str, Any]) -> dict[str, Any]:
    foreign = _number(row, "Foreign_Investor_buy") - _number(row, "Foreign_Investor_sell")
    foreign_dealer = (
        _number(row, "Foreign_Dealer_Self_buy")
        - _number(row, "Foreign_Dealer_Self_sell")
    )
    trust = _number(row, "Investment_Trust_buy") - _number(row, "Investment_Trust_sell")
    dealer = (
        _number(row, "Dealer_buy")
        - _number(row, "Dealer_sell")
        + _number(row, "Dealer_self_buy")
        - _number(row, "Dealer_self_sell")
        + _number(row, "Dealer_Hedging_buy")
        - _number(row, "Dealer_Hedging_sell")
    )
    total = foreign + foreign_dealer + trust + dealer

    return {
        "date": row.get("date"),
        "foreignNetShares": _as_int(foreign),
        "foreignNetLots": _round(foreign / 1000),
        "foreignDealerSelfNetShares": _as_int(foreign_dealer),
        "investmentTrustNetShares": _as_int(trust),
        "investmentTrustNetLots": _round(trust / 1000),
        "dealerNetShares": _as_int(dealer),
        "dealerNetLots": _round(dealer / 1000),
        "totalInstitutionalNetShares": _as_int(total),
        "totalInstitutionalNetLots": _round(total / 1000),
    }


async def _get_institutional_data(symbol: str, days: int) -> dict[str, Any]:
    start, end = _days_range(days, 5, 365)
    rows = await _finmind_get(
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        symbol,
        start,
        end,
    )
    parsed = [_institutional_row(row) for row in rows]

    if not parsed:
        raise RuntimeError("查無三大法人資料，可能尚未更新或股票代號不正確。")

    def cumulative(field: str, count: int) -> int:
        return sum(int(row.get(field, 0)) for row in parsed[-count:])

    latest = parsed[-1]
    return {
        "symbol": symbol,
        "latestDate": latest["date"],
        "latest": latest,
        "cumulative": {
            "last5TradingDays": {
                "foreignNetShares": cumulative("foreignNetShares", 5),
                "investmentTrustNetShares": cumulative("investmentTrustNetShares", 5),
                "dealerNetShares": cumulative("dealerNetShares", 5),
                "totalInstitutionalNetShares": cumulative("totalInstitutionalNetShares", 5),
            },
            "last10TradingDays": {
                "foreignNetShares": cumulative("foreignNetShares", 10),
                "investmentTrustNetShares": cumulative("investmentTrustNetShares", 10),
                "dealerNetShares": cumulative("dealerNetShares", 10),
                "totalInstitutionalNetShares": cumulative("totalInstitutionalNetShares", 10),
            },
            "last20TradingDays": {
                "foreignNetShares": cumulative("foreignNetShares", 20),
                "investmentTrustNetShares": cumulative("investmentTrustNetShares", 20),
                "dealerNetShares": cumulative("dealerNetShares", 20),
                "totalInstitutionalNetShares": cumulative("totalInstitutionalNetShares", 20),
            },
        },
        "recent": parsed[-30:],
        "source": "FinMind TaiwanStockInstitutionalInvestorsBuySellWide",
        "note": "法人買賣數值原始單位為股；Lots 欄位以 1,000 股換算。",
    }


async def _get_margin_data(symbol: str, days: int) -> dict[str, Any]:
    start, end = _days_range(days, 5, 365)
    rows = await _finmind_get(
        "TaiwanStockMarginPurchaseShortSale",
        symbol,
        start,
        end,
    )
    if not rows:
        raise RuntimeError("查無融資融券資料，可能尚未更新或股票代號不正確。")

    parsed = []
    for row in rows:
        margin_today = _number(row, "MarginPurchaseTodayBalance")
        margin_yesterday = _number(row, "MarginPurchaseYesterdayBalance")
        short_today = _number(row, "ShortSaleTodayBalance")
        short_yesterday = _number(row, "ShortSaleYesterdayBalance")
        parsed.append({
            "date": row.get("date"),
            "marginBuy": _as_int(_number(row, "MarginPurchaseBuy")),
            "marginSell": _as_int(_number(row, "MarginPurchaseSell")),
            "marginCashRepayment": _as_int(_number(row, "MarginPurchaseCashRepayment")),
            "marginBalance": _as_int(margin_today),
            "marginBalanceChange": _as_int(margin_today - margin_yesterday),
            "shortSell": _as_int(_number(row, "ShortSaleSell")),
            "shortBuy": _as_int(_number(row, "ShortSaleBuy")),
            "shortCashRepayment": _as_int(_number(row, "ShortSaleCashRepayment")),
            "shortBalance": _as_int(short_today),
            "shortBalanceChange": _as_int(short_today - short_yesterday),
            "offsetLoanAndShort": _as_int(_number(row, "OffsetLoanAndShort")),
        })

    latest = parsed[-1]
    first_raw = rows[0]
    period_margin_change = (
        _number(rows[-1], "MarginPurchaseTodayBalance")
        - _number(first_raw, "MarginPurchaseYesterdayBalance")
    )
    period_short_change = (
        _number(rows[-1], "ShortSaleTodayBalance")
        - _number(first_raw, "ShortSaleYesterdayBalance")
    )

    return {
        "symbol": symbol,
        "latestDate": latest["date"],
        "latest": latest,
        "periodChange": {
            "marginBalanceChange": _as_int(period_margin_change),
            "shortBalanceChange": _as_int(period_short_change),
        },
        "recent": parsed[-30:],
        "source": "FinMind TaiwanStockMarginPurchaseShortSale",
        "note": "融資融券欄位沿用交易所公布單位；台股個股通常以張呈現。",
    }


async def _get_foreign_shareholding_data(symbol: str, days: int) -> dict[str, Any]:
    start, end = _days_range(days, 7, 365)
    rows = await _finmind_get(
        "TaiwanStockShareholding",
        symbol,
        start,
        end,
    )
    if not rows:
        raise RuntimeError("查無外資持股資料，可能尚未更新或股票代號不正確。")

    latest = rows[-1]
    first = rows[0]
    latest_ratio = _number(latest, "ForeignInvestmentSharesRatio")
    first_ratio = _number(first, "ForeignInvestmentSharesRatio")

    recent = [{
        "date": row.get("date"),
        "foreignShares": _as_int(_number(row, "ForeignInvestmentShares")),
        "foreignSharesRatio": _round(_number(row, "ForeignInvestmentSharesRatio")),
        "foreignRemainingRatio": _round(_number(row, "ForeignInvestmentRemainRatio")),
        "issuedShares": _as_int(_number(row, "NumberOfSharesIssued")),
    } for row in rows[-30:]]

    return {
        "symbol": symbol,
        "stockName": latest.get("stock_name"),
        "latestDate": latest.get("date"),
        "foreignShares": _as_int(_number(latest, "ForeignInvestmentShares")),
        "foreignSharesRatio": _round(latest_ratio),
        "periodRatioChangePercentagePoints": _round(latest_ratio - first_ratio),
        "recent": recent,
        "source": "FinMind TaiwanStockShareholding",
    }


async def _get_lending_data(symbol: str, days: int) -> dict[str, Any]:
    start, end = _days_range(days, 5, 180)
    rows = await _finmind_get(
        "TaiwanStockSecuritiesLending",
        symbol,
        start,
        end,
    )
    if not rows:
        return {
            "symbol": symbol,
            "message": "所選期間查無借券成交資料。",
            "recentDaily": [],
            "source": "FinMind TaiwanStockSecuritiesLending",
        }

    daily: dict[str, dict[str, float]] = {}
    for row in rows:
        day = str(row.get("date"))
        volume = _number(row, "volume")
        fee_rate = _number(row, "fee_rate")
        item = daily.setdefault(day, {"volume": 0.0, "weightedFee": 0.0})
        item["volume"] += volume
        item["weightedFee"] += volume * fee_rate

    recent = []
    for day in sorted(daily):
        volume = daily[day]["volume"]
        avg_fee = daily[day]["weightedFee"] / volume if volume else 0
        recent.append({
            "date": day,
            "lendingVolume": _as_int(volume),
            "weightedAverageFeeRate": _round(avg_fee, 4),
        })

    return {
        "symbol": symbol,
        "latestDate": recent[-1]["date"],
        "periodTotalVolume": sum(item["lendingVolume"] for item in recent),
        "recentDaily": recent[-30:],
        "source": "FinMind TaiwanStockSecuritiesLending",
        "note": "此資料為借券成交明細彙總，不等同借券賣出餘額。",
    }


async def _get_distribution_data(symbol: str, days: int) -> dict[str, Any]:
    start, end = _days_range(days, 28, 365)
    rows = await _finmind_get(
        "TaiwanStockHoldingSharesPer",
        symbol,
        start,
        end,
    )
    if not rows:
        raise RuntimeError(
            "查無股權分散資料。此資料集需要 FinMind backer 或 sponsor 方案。"
        )

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_date.setdefault(str(row.get("date")), []).append(row)

    dates = sorted(by_date)
    latest_date = dates[-1]

    def summarize(day: str) -> dict[str, Any]:
        levels = []
        small_percent = 0.0
        small_people = 0
        for row in by_date[day]:
            level = str(row.get("HoldingSharesLevel", ""))
            digits = [int(x) for x in re.findall(r"\d+", level.replace(",", ""))]
            upper = max(digits) if digits else None
            percent = _number(row, "percent")
            people = _as_int(_number(row, "people"))
            unit = _as_int(_number(row, "unit"))
            if upper is not None and upper <= 100000:
                small_percent += percent
                small_people += people
            levels.append({
                "level": level,
                "people": people,
                "percent": _round(percent),
                "shares": unit,
            })
        return {
            "date": day,
            "under100LotsPercent": _round(small_percent),
            "under100LotsPeople": small_people,
            "levels": sorted(levels, key=lambda x: x["level"]),
        }

    latest = summarize(latest_date)
    previous = summarize(dates[-2]) if len(dates) >= 2 else None

    return {
        "symbol": symbol,
        "latest": latest,
        "previousUnder100LotsPercent": (
            previous["under100LotsPercent"] if previous else None
        ),
        "under100LotsPercentChange": (
            _round(latest["under100LotsPercent"] - previous["under100LotsPercent"])
            if previous else None
        ),
        "source": "FinMind TaiwanStockHoldingSharesPer",
        "access": "FinMind backer 或 sponsor 會員",
    }


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


@mcp.tool()
def ping() -> dict:
    """檢查台股 MCP 伺服器是否正常運作。"""
    return {
        "ok": True,
        "server": "Taiwan Stock MCP v3",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "tools": [
            "get_realtime_quote",
            "get_historical_candles",
            "get_technical_summary",
            "get_institutional_trades",
            "get_margin_short",
            "get_foreign_shareholding",
            "get_securities_lending",
            "get_shareholding_distribution",
            "get_chip_summary",
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


@mcp.tool()
async def get_institutional_trades(symbol: str, days: int = 45) -> dict:
    """
    取得個股三大法人買賣超，包含外資、投信、自營商，
    並計算近 5、10、20 個交易日累計買賣超。
    """
    symbol = _validate_symbol(symbol)
    result = await _get_institutional_data(symbol, days)
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_margin_short(symbol: str, days: int = 45) -> dict:
    """
    取得個股融資融券買賣、餘額與每日增減，
    並計算查詢期間的融資與融券餘額變化。
    """
    symbol = _validate_symbol(symbol)
    result = await _get_margin_data(symbol, days)
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_foreign_shareholding(symbol: str, days: int = 45) -> dict:
    """取得外資持股股數、持股比例及期間持股比例變化。"""
    symbol = _validate_symbol(symbol)
    result = await _get_foreign_shareholding_data(symbol, days)
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_securities_lending(symbol: str, days: int = 30) -> dict:
    """取得借券成交量與加權平均借券費率；此資料不等同借券賣出餘額。"""
    symbol = _validate_symbol(symbol)
    result = await _get_lending_data(symbol, days)
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_shareholding_distribution(symbol: str, days: int = 120) -> dict:
    """
    取得股權持股分級，並計算百張以下持股比例及相較前一期變化。
    注意：FinMind 此資料集需要 backer 或 sponsor 方案。
    """
    symbol = _validate_symbol(symbol)
    result = await _get_distribution_data(symbol, days)
    result["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return result


@mcp.tool()
async def get_chip_summary(symbol: str, days: int = 45) -> dict:
    """
    一次取得個股籌碼摘要：三大法人、融資融券、外資持股與借券成交。
    股權分散需另外呼叫 get_shareholding_distribution。
    """
    symbol = _validate_symbol(symbol)

    institutional = await _get_institutional_data(symbol, days)
    margin = await _get_margin_data(symbol, days)
    foreign = await _get_foreign_shareholding_data(symbol, days)
    lending = await _get_lending_data(symbol, min(days, 180))

    return {
        "symbol": symbol,
        "institutional": institutional,
        "marginShort": margin,
        "foreignShareholding": foreign,
        "securitiesLending": lending,
        "source": "FinMind API v4",
        "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
