import asyncio
import hashlib
import json
import math
import os
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from statistics import mean, pstdev
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

try:
    import redis.asyncio as redis_async
except ImportError:  # Redis is optional; memory cache still works.
    redis_async = None


PORT = int(os.environ.get("PORT", "8000"))
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
CACHE_PREFIX = os.environ.get("CACHE_PREFIX", "twstock:mcp:v5")
CACHE_MAX_ITEMS = int(os.environ.get("CACHE_MAX_ITEMS", "2500"))
REDIS_URL = os.environ.get("REDIS_URL", "").strip()

# Always-on local cache. Optional Redis/Valkey makes cache shared and survives
# web-service sleeps as long as the Key Value instance itself remains available.
_memory_cache: dict[str, dict[str, Any]] = {}
_cache_lock = asyncio.Lock()
_key_locks: dict[str, asyncio.Lock] = {}
_redis_client: Any = None
_redis_init_attempted = False
_redis_error: str | None = None

CACHE_STATS: dict[str, int] = {
    "memoryHits": 0,
    "redisHits": 0,
    "misses": 0,
    "writes": 0,
    "fugleUpstreamRequests": 0,
    "finmindUpstreamRequests": 0,
    "upstreamErrors": 0,
}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _cache_key(namespace: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()[:32]
    return f"{CACHE_PREFIX}:{namespace}:{digest}"


def _taipei_now() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _is_tw_market_hours(now: datetime | None = None) -> bool:
    now = now or _taipei_now()
    return now.weekday() < 5 and dt_time(8, 30) <= now.time() <= dt_time(13, 45)


def _seconds_until_next_weekday_open(now: datetime | None = None) -> int:
    now = now or _taipei_now()
    candidate = now.replace(hour=8, minute=30, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(300, int((candidate - now).total_seconds()))


def _quote_ttl() -> int:
    return 10 if _is_tw_market_hours() else min(_seconds_until_next_weekday_open(), 3600)


def _snapshot_ttl() -> int:
    return 30 if _is_tw_market_hours() else _seconds_until_next_weekday_open()


def _historical_ttl() -> int:
    # Daily candle changes intraday, but is stable after the close.
    return 300 if _is_tw_market_hours() else _seconds_until_next_weekday_open()


def _finmind_ttl(dataset: str) -> int:
    now = _taipei_now()
    if dataset == "TaiwanStockHoldingSharesPer":
        return 24 * 3600
    # Around the usual evening update window, keep TTL short so fresh data can arrive.
    if 19 <= now.hour < 22:
        return 20 * 60
    if 8 <= now.hour < 19:
        return 2 * 3600
    return 10 * 3600


async def _get_redis_client() -> Any:
    global _redis_client, _redis_init_attempted, _redis_error
    if not REDIS_URL or redis_async is None:
        return None
    if _redis_client is not None:
        return _redis_client
    if _redis_init_attempted and _redis_error:
        return None
    _redis_init_attempted = True
    try:
        client = redis_async.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await client.ping()
        _redis_client = client
        _redis_error = None
        return client
    except Exception as exc:
        _redis_error = str(exc)
        return None


def _memory_prune(now_epoch: float | None = None) -> None:
    now_epoch = now_epoch or time.time()
    expired = [key for key, entry in _memory_cache.items() if float(entry.get("expiresAt", 0)) <= now_epoch]
    for key in expired:
        _memory_cache.pop(key, None)
    if len(_memory_cache) > CACHE_MAX_ITEMS:
        ordered = sorted(_memory_cache.items(), key=lambda item: float(item[1].get("createdAt", 0)))
        for key, _ in ordered[: len(_memory_cache) - CACHE_MAX_ITEMS]:
            _memory_cache.pop(key, None)


async def _cache_get(namespace: str, payload: dict[str, Any]) -> Any | None:
    key = _cache_key(namespace, payload)
    now_epoch = time.time()
    entry = _memory_cache.get(key)
    if entry and float(entry.get("expiresAt", 0)) > now_epoch:
        CACHE_STATS["memoryHits"] += 1
        return json.loads(entry["payload"])
    if entry:
        _memory_cache.pop(key, None)

    client = await _get_redis_client()
    if client is not None:
        try:
            raw = await client.get(key)
            if raw:
                remote_entry = json.loads(raw)
                if float(remote_entry.get("expiresAt", 0)) > now_epoch:
                    CACHE_STATS["redisHits"] += 1
                    _memory_cache[key] = {
                        "namespace": remote_entry.get("namespace", namespace),
                        "createdAt": remote_entry.get("createdAt", now_epoch),
                        "expiresAt": remote_entry.get("expiresAt", now_epoch + 60),
                        "payload": _json_dumps(remote_entry.get("value")),
                    }
                    return remote_entry.get("value")
                await client.delete(key)
        except Exception:
            pass

    CACHE_STATS["misses"] += 1
    return None


async def _cache_set(namespace: str, payload: dict[str, Any], value: Any, ttl: int) -> None:
    key = _cache_key(namespace, payload)
    now_epoch = time.time()
    entry = {
        "namespace": namespace,
        "createdAt": now_epoch,
        "expiresAt": now_epoch + max(1, int(ttl)),
        "payload": _json_dumps(value),
    }
    _memory_cache[key] = entry
    _memory_prune(now_epoch)
    CACHE_STATS["writes"] += 1

    client = await _get_redis_client()
    if client is not None:
        try:
            remote_entry = {
                "namespace": namespace,
                "createdAt": now_epoch,
                "expiresAt": now_epoch + max(1, int(ttl)),
                "value": value,
            }
            await client.set(key, _json_dumps(remote_entry), ex=max(1, int(ttl)))
        except Exception:
            pass


async def _cached_call(
    namespace: str,
    payload: dict[str, Any],
    ttl: int,
    fetcher: Callable[[], Awaitable[Any]],
    force_refresh: bool = False,
) -> Any:
    key = _cache_key(namespace, payload)
    if not force_refresh:
        cached = await _cache_get(namespace, payload)
        if cached is not None:
            return cached

    async with _cache_lock:
        lock = _key_locks.setdefault(key, asyncio.Lock())

    async with lock:
        if not force_refresh:
            cached = await _cache_get(namespace, payload)
            if cached is not None:
                return cached
        value = await fetcher()
        await _cache_set(namespace, payload, value, ttl)
        return value

mcp = FastMCP(
    "Taiwan Stock MCP",
    instructions=(
        "Use this server to query Taiwan stock real-time quotes, historical K-lines, "
        "moving averages, Bollinger Bands, technical summaries, institutional trading, "
        "margin/short balances, foreign ownership, securities lending, shareholding distribution, "
        "full-market screening, watchlist ranking, and full stock analysis. "
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
    CACHE_STATS["fugleUpstreamRequests"] += 1
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

    try:
        response.raise_for_status()
    except Exception:
        CACHE_STATS["upstreamErrors"] += 1
        raise
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
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    payload = {
        "dataset": dataset,
        "data_id": symbol,
        "start_date": start_date,
        "end_date": end_date,
    }

    async def fetch() -> list[dict[str, Any]]:
        CACHE_STATS["finmindUpstreamRequests"] += 1
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                FINMIND_URL,
                params=payload,
                headers={"Authorization": f"Bearer {_finmind_token()}"},
            )

        if response.status_code in {401, 403}:
            raise RuntimeError(
                "FinMind Token 驗證失敗，或目前會員方案沒有此資料集權限。"
            )
        if response.status_code == 402:
            raise RuntimeError("此 FinMind 資料集需要付費會員方案或已達使用上限。")
        if response.status_code == 429:
            raise RuntimeError("FinMind API 呼叫次數已達上限，請稍後再試。")

        try:
            response.raise_for_status()
        except Exception:
            CACHE_STATS["upstreamErrors"] += 1
            raise
        body = response.json()

        if body.get("status") not in (None, 200):
            message = body.get("msg") or body.get("message") or "FinMind API 回傳錯誤。"
            raise RuntimeError(str(message))

        rows = body.get("data") or []
        if not isinstance(rows, list):
            raise RuntimeError("FinMind API 回傳格式不正確。")
        return sorted(rows, key=lambda row: str(row.get("date", "")))

    return await _cached_call(
        f"finmind:{dataset}",
        payload,
        _finmind_ttl(dataset),
        fetch,
        force_refresh=force_refresh,
    )


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



def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


async def _get_intraday_quote(symbol: str, force_refresh: bool = False) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    return await _cached_call(
        "fugle:quote",
        {"symbol": symbol},
        _quote_ttl(),
        lambda: _fugle_get(f"intraday/quote/{symbol}"),
        force_refresh=force_refresh,
    )


async def _get_snapshot_quotes(market: str, force_refresh: bool = False) -> dict[str, Any]:
    """取得 Fugle 全市場快照；此端點需要 Fugle 開發者或進階方案。"""
    market = market.upper().strip()
    if market not in {"TSE", "OTC"}:
        raise ValueError("market 僅支援 TSE 或 OTC。")
    return await _cached_call(
        "fugle:snapshot",
        {"market": market, "type": "COMMONSTOCK"},
        _snapshot_ttl(),
        lambda: _fugle_get(
            f"snapshot/quotes/{market}",
            params={"type": "COMMONSTOCK"},
        ),
        force_refresh=force_refresh,
    )


async def _get_historical_candles_raw(
    symbol: str,
    days: int = 180,
    timeframe: str = "D",
    adjusted: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = timeframe.upper().strip()
    end = date.today()
    start = end - timedelta(days=days)
    params = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timeframe": timeframe,
        "adjusted": str(adjusted).lower(),
        "fields": "open,high,low,close,volume,turnover,change",
        "sort": "asc",
    }
    return await _cached_call(
        "fugle:historical",
        {"symbol": symbol, **params},
        _historical_ttl(),
        lambda: _fugle_get(f"historical/candles/{symbol}", params=params),
        force_refresh=force_refresh,
    )


async def _get_daily_candles_raw(
    symbol: str,
    days: int = 180,
    force_refresh: bool = False,
) -> dict[str, Any]:
    return await _get_historical_candles_raw(
        symbol,
        days=days,
        timeframe="D",
        adjusted=True,
        force_refresh=force_refresh,
    )


def _technical_features(symbol: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [
        row for row in candles
        if row.get("close") is not None
        and row.get("high") is not None
        and row.get("low") is not None
        and row.get("volume") is not None
    ]
    if len(valid) < 60:
        raise RuntimeError(f"{symbol} 歷史資料不足 60 個交易日。")

    closes = [_safe_float(row.get("close")) for row in valid]
    highs = [_safe_float(row.get("high")) for row in valid]
    lows = [_safe_float(row.get("low")) for row in valid]
    volumes = [_safe_float(row.get("volume")) for row in valid]

    close = closes[-1]
    ma5 = mean(closes[-5:])
    ma10 = mean(closes[-10:])
    ma20 = mean(closes[-20:])
    ma60 = mean(closes[-60:])

    bb_std = pstdev(closes[-20:])
    bb_upper = ma20 + 2 * bb_std
    bb_lower = ma20 - 2 * bb_std
    bb_width = (bb_upper - bb_lower) / ma20 * 100 if ma20 else 0.0
    bb_position = (
        (close - bb_lower) / (bb_upper - bb_lower) * 100
        if bb_upper != bb_lower else 50.0
    )

    avg_volume20 = mean(volumes[-20:])
    volume_ratio20 = volumes[-1] / avg_volume20 if avg_volume20 else 0.0

    prior_high20 = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[-20:])
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    high60 = max(highs[-60:])
    low60 = min(lows[-60:])
    distance_to_prior_high20 = (close / prior_high20 - 1) * 100 if prior_high20 else 0.0
    gain5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else 0.0
    gain20 = (close / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else 0.0

    if ma5 > ma10 > ma20 > ma60:
        alignment = "多頭排列"
    elif ma5 < ma10 < ma20 < ma60:
        alignment = "空頭排列"
    else:
        alignment = "非典型排列"

    return {
        "symbol": symbol,
        "latestDate": valid[-1].get("date"),
        "close": _round(close),
        "ma5": _round(ma5),
        "ma10": _round(ma10),
        "ma20": _round(ma20),
        "ma60": _round(ma60),
        "alignment": alignment,
        "aboveMA20": close >= ma20,
        "aboveMA60": close >= ma60,
        "distanceFromMA20Percent": _round((close / ma20 - 1) * 100 if ma20 else 0.0),
        "distanceFromMA60Percent": _round((close / ma60 - 1) * 100 if ma60 else 0.0),
        "bollingerUpper": _round(bb_upper),
        "bollingerMiddle": _round(ma20),
        "bollingerLower": _round(bb_lower),
        "bollingerWidthPercent": _round(bb_width),
        "bollingerPositionPercent": _round(bb_position),
        "volumeRatio20": _round(volume_ratio20),
        "averageVolume20": _as_int(avg_volume20),
        "priorHigh20": _round(prior_high20),
        "distanceToPriorHigh20Percent": _round(distance_to_prior_high20),
        "high20": _round(high20),
        "low20": _round(low20),
        "high60": _round(high60),
        "low60": _round(low60),
        "gain5Percent": _round(gain5),
        "gain20Percent": _round(gain20),
        "latestVolume": _as_int(volumes[-1]),
    }


def _score_candidate(
    snapshot: dict[str, Any],
    tech: dict[str, Any],
    strategy: str,
) -> tuple[float, list[str], list[str]]:
    strategy = strategy.lower().strip()
    close = _safe_float(snapshot.get("closePrice"), _safe_float(tech.get("close")))
    change_pct = _safe_float(snapshot.get("changePercent"))
    trade_value = _safe_float(snapshot.get("tradeValue"))
    volume_ratio = _safe_float(tech.get("volumeRatio20"))
    dist_ma20 = _safe_float(tech.get("distanceFromMA20Percent"))
    dist_high20 = _safe_float(tech.get("distanceToPriorHigh20Percent"))
    gain20 = _safe_float(tech.get("gain20Percent"))
    bb_width = _safe_float(tech.get("bollingerWidthPercent"))
    bb_position = _safe_float(tech.get("bollingerPositionPercent"))

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    # 流動性（最高 18 分）
    if trade_value >= 3_000_000_000:
        score += 18
        reasons.append("成交值高、流動性佳")
    elif trade_value >= 1_000_000_000:
        score += 14
        reasons.append("成交值充足")
    elif trade_value >= 300_000_000:
        score += 9
    elif trade_value >= 100_000_000:
        score += 5

    # 趨勢（最高 28 分）
    alignment = str(tech.get("alignment"))
    if alignment == "多頭排列":
        score += 24
        reasons.append("5/10/20/60 日均線多頭排列")
    elif tech.get("aboveMA20") and tech.get("aboveMA60"):
        score += 15
        reasons.append("股價站上月線與季線")
    elif tech.get("aboveMA20"):
        score += 8
    else:
        score -= 10
        risks.append("股價位於月線下方")

    # 動能與量價（最高約 28 分）
    if 1.2 <= volume_ratio <= 3.0:
        score += 15
        reasons.append(f"量比 {volume_ratio:.2f}，量價有加速")
    elif 1.0 <= volume_ratio < 1.2:
        score += 8
    elif volume_ratio < 0.7:
        score -= 4
        risks.append("量能低於 20 日均量")
    elif volume_ratio > 4.0:
        score -= 4
        risks.append("爆量過大，隔日震盪風險升高")

    if 1.0 <= change_pct <= 6.5:
        score += 10
        reasons.append("當日漲幅具動能但尚未極端")
    elif 0 < change_pct < 1.0:
        score += 4
    elif change_pct > 8.5:
        score -= 8
        risks.append("當日漲幅接近漲停，追價風險較高")
    elif change_pct < -3.0:
        score -= 8

    # 突破位置（最高 20 分）
    if dist_high20 >= 0:
        score += 18
        reasons.append("已突破近 20 日前高")
    elif -2.0 <= dist_high20 < 0:
        score += 13
        reasons.append("距近 20 日前高不到 2%")
    elif -5.0 <= dist_high20 < -2.0:
        score += 6

    # 過熱控制
    if gain20 > 35:
        score -= 12
        risks.append("近 20 日漲幅超過 35%，短線偏熱")
    elif gain20 > 22:
        score -= 5
        risks.append("近 20 日漲幅較大")

    if dist_ma20 > 18:
        score -= 10
        risks.append("股價乖離月線過大")
    elif 0 <= dist_ma20 <= 8:
        score += 7
        reasons.append("距月線不遠，乖離仍可控")

    # 策略加權
    if strategy == "breakout":
        score += 12 if dist_high20 >= -1.0 else -5
        score += 8 if volume_ratio >= 1.2 else 0
    elif strategy == "early_stage":
        if 0 <= dist_ma20 <= 8 and gain20 <= 20:
            score += 15
            reasons.append("符合剛起漲、低乖離條件")
        if 4 <= bb_width <= 15:
            score += 7
            reasons.append("布林通道寬度適中，具發動空間")
        if bb_position > 95 and change_pct > 7:
            score -= 7
    elif strategy == "trend":
        score += 12 if alignment == "多頭排列" else 0
        score += 6 if gain20 > 0 else -3
    elif strategy == "pullback":
        if -2 <= dist_ma20 <= 4 and tech.get("aboveMA60"):
            score += 16
            reasons.append("回測月線附近且仍守季線")
        if change_pct < -1:
            score += 3
    elif strategy != "balanced":
        raise ValueError("strategy 僅支援 balanced、breakout、early_stage、trend、pullback。")

    # 收盤位置
    high = _safe_float(snapshot.get("highPrice"))
    low = _safe_float(snapshot.get("lowPrice"))
    if high > low and close:
        close_location = (close - low) / (high - low)
        if close_location >= 0.8:
            score += 5
            reasons.append("收盤接近日高")
        elif close_location <= 0.2:
            score -= 4
            risks.append("收盤接近日低")

    return round(score, 2), reasons[:6], risks[:5]


async def _safe_chip_for_ranking(symbol: str, days: int = 45) -> dict[str, Any]:
    try:
        institutional, margin = await asyncio.gather(
            _get_institutional_data(symbol, days),
            _get_margin_data(symbol, days),
        )
        five = institutional.get("cumulative", {}).get("last5TradingDays", {})
        foreign5 = int(five.get("foreignNetShares", 0))
        trust5 = int(five.get("investmentTrustNetShares", 0))
        margin_change = int(margin.get("periodChange", {}).get("marginBalanceChange", 0))
        score_adjustment = 0.0
        reasons: list[str] = []
        risks: list[str] = []
        if foreign5 > 0:
            score_adjustment += 5
            reasons.append("外資近 5 日累計買超")
        elif foreign5 < 0:
            score_adjustment -= 3
            risks.append("外資近 5 日累計賣超")
        if trust5 > 0:
            score_adjustment += 5
            reasons.append("投信近 5 日累計買超")
        if margin_change < 0:
            score_adjustment += 3
            reasons.append("期間融資餘額下降")
        elif margin_change > 0:
            score_adjustment -= 2
            risks.append("期間融資餘額增加")
        return {
            "available": True,
            "scoreAdjustment": score_adjustment,
            "reasons": reasons,
            "risks": risks,
            "foreign5Shares": foreign5,
            "investmentTrust5Shares": trust5,
            "marginPeriodChange": margin_change,
            "latestInstitutionalDate": institutional.get("latestDate"),
            "latestMarginDate": margin.get("latestDate"),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "scoreAdjustment": 0.0}


async def _analyze_candidate(
    snapshot: dict[str, Any],
    strategy: str,
    semaphore: asyncio.Semaphore,
    force_refresh: bool = False,
) -> dict[str, Any]:
    symbol = _validate_symbol(str(snapshot.get("symbol", "")))
    async with semaphore:
        raw = await _get_daily_candles_raw(symbol, 180, force_refresh=force_refresh)
    tech = _technical_features(symbol, raw.get("data", []))
    score, reasons, risks = _score_candidate(snapshot, tech, strategy)
    return {
        "symbol": symbol,
        "name": snapshot.get("name"),
        "market": snapshot.get("market"),
        "closePrice": snapshot.get("closePrice"),
        "changePercent": snapshot.get("changePercent"),
        "tradeVolume": snapshot.get("tradeVolume"),
        "tradeValue": snapshot.get("tradeValue"),
        "score": score,
        "reasons": reasons,
        "risks": risks,
        "technical": tech,
    }


@mcp.tool()
def ping() -> dict:
    """檢查台股 MCP 伺服器是否正常運作。"""
    return {
        "ok": True,
        "server": "Taiwan Stock MCP v5-cache",
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
            "screen_market",
            "rank_screened_stocks",
            "get_stock_full_analysis",
            "get_cache_status",
            "clear_cache",
        ],
    }


@mcp.tool()
async def get_realtime_quote(symbol: str) -> dict:
    """取得台股上市櫃股票即時報價、成交量、內外盤與最佳五檔。"""
    symbol = _validate_symbol(symbol)
    data = await _get_intraday_quote(symbol)

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

    data = await _get_historical_candles_raw(
        symbol, days=days, timeframe=timeframe, adjusted=adjusted
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

    raw = await _get_daily_candles_raw(symbol, days)

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



@mcp.tool()
async def screen_market(
    strategy: str = "balanced",
    markets: str = "BOTH",
    top_n: int = 10,
    candidate_limit: int = 40,
    min_trade_value: int = 100000000,
    min_price: float = 10.0,
    max_price: float = 5000.0,
    include_chip: bool = False,
    force_refresh: bool = False,
) -> dict:
    """
    掃描上市與上櫃普通股，先以全市場行情快照篩出流動性候選股，
    再計算均線、布林、量比、突破位置與過熱風險並排名。

    strategy：balanced、breakout、early_stage、trend、pullback。
    markets：BOTH、TSE、OTC。
    include_chip=true 時，只為最後入選股補上法人與融資籌碼評分。

    注意：全市場快照需要 Fugle 開發者或進階行情方案。
    """
    strategy = strategy.lower().strip()
    markets = markets.upper().strip()
    if strategy not in {"balanced", "breakout", "early_stage", "trend", "pullback"}:
        raise ValueError("strategy 僅支援 balanced、breakout、early_stage、trend、pullback。")
    if markets not in {"BOTH", "TSE", "OTC"}:
        raise ValueError("markets 僅支援 BOTH、TSE、OTC。")
    if not 1 <= top_n <= 20:
        raise ValueError("top_n 請填 1 到 20。")
    if not top_n <= candidate_limit <= 60:
        raise ValueError("candidate_limit 必須大於等於 top_n，且最多 60。")
    if min_trade_value < 0:
        raise ValueError("min_trade_value 不可為負數。")
    if min_price <= 0 or max_price <= min_price:
        raise ValueError("價格範圍設定不正確。")

    requested_markets = ["TSE", "OTC"] if markets == "BOTH" else [markets]
    try:
        snapshots_raw = await asyncio.gather(
            *[_get_snapshot_quotes(market, force_refresh=force_refresh) for market in requested_markets]
        )
    except RuntimeError as exc:
        if "方案沒有此資料權限" in str(exc):
            raise RuntimeError(
                "全市場選股需要 Fugle 的 Snapshot Quotes 權限。"
                "請確認 Fugle 為開發者或進階方案；個股查詢功能不受影響。"
            ) from exc
        raise

    universe: list[dict[str, Any]] = []
    as_of: list[dict[str, Any]] = []
    for market, payload in zip(requested_markets, snapshots_raw):
        as_of.append({"market": market, "date": payload.get("date"), "time": payload.get("time")})
        for row in payload.get("data", []):
            symbol = str(row.get("symbol", ""))
            close = _safe_float(row.get("closePrice"))
            trade_value = _safe_float(row.get("tradeValue"))
            if not re.fullmatch(r"\d{4}", symbol):
                continue
            if not min_price <= close <= max_price:
                continue
            if trade_value < min_trade_value:
                continue
            item = dict(row)
            item["market"] = market
            universe.append(item)

    # 預篩兼顧成交值、當日動能與不追過熱。
    def prefilter_score(row: dict[str, Any]) -> float:
        value = _safe_float(row.get("tradeValue"))
        change = _safe_float(row.get("changePercent"))
        liquidity = math.log10(max(value, 1))
        momentum = _clamp(change, -3, 7) * 0.7
        overheat_penalty = max(change - 8.0, 0) * 2.0
        if strategy == "pullback":
            momentum = -abs(change) * 0.25
        elif strategy == "breakout":
            momentum = _clamp(change, 0, 8) * 1.2
        return liquidity + momentum - overheat_penalty

    universe.sort(key=prefilter_score, reverse=True)
    candidates = universe[:candidate_limit]
    semaphore = asyncio.Semaphore(6)
    results = await asyncio.gather(
        *[_analyze_candidate(row, strategy, semaphore, force_refresh=force_refresh) for row in candidates],
        return_exceptions=True,
    )

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row, result in zip(candidates, results):
        if isinstance(result, Exception):
            errors.append({"symbol": str(row.get("symbol")), "error": str(result)})
        else:
            analyzed.append(result)

    analyzed.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)
    selected = analyzed[:top_n]

    if include_chip and selected:
        chip_results = await asyncio.gather(
            *[_safe_chip_for_ranking(item["symbol"], 45) for item in selected]
        )
        for item, chip in zip(selected, chip_results):
            item["chip"] = chip
            item["score"] = round(_safe_float(item.get("score")) + _safe_float(chip.get("scoreAdjustment")), 2)
            item["reasons"] = (item.get("reasons", []) + chip.get("reasons", []))[:8]
            item["risks"] = (item.get("risks", []) + chip.get("risks", []))[:6]
        selected.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)

    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank

    return {
        "strategy": strategy,
        "markets": requested_markets,
        "asOf": as_of,
        "snapshotUniverseCount": len(universe),
        "deepAnalyzedCount": len(analyzed),
        "candidateLimit": candidate_limit,
        "filters": {
            "minTradeValue": min_trade_value,
            "minPrice": min_price,
            "maxPrice": max_price,
        },
        "includeChip": include_chip,
        "forceRefresh": force_refresh,
        "cacheNote": "相同行情、K 線與籌碼請求會依 TTL 使用快取；force_refresh=true 可略過。",
        "results": selected,
        "errors": errors[:15],
        "method": "全市場快照預篩，再對候選股計算 180 日技術指標；不是報酬保證。",
        "source": "Fugle MarketData API v1.0；include_chip 時另用 FinMind API v4",
        "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
async def rank_screened_stocks(
    symbols: list[str],
    strategy: str = "balanced",
    include_chip: bool = True,
) -> dict:
    """
    對指定股票清單做深度排名。適合比較自選股或 screen_market 的候選股。
    symbols 最多 20 檔；strategy 支援 balanced、breakout、early_stage、trend、pullback。
    """
    strategy = strategy.lower().strip()
    if strategy not in {"balanced", "breakout", "early_stage", "trend", "pullback"}:
        raise ValueError("strategy 僅支援 balanced、breakout、early_stage、trend、pullback。")
    cleaned = list(dict.fromkeys(_validate_symbol(symbol) for symbol in symbols))
    if not cleaned:
        raise ValueError("symbols 不可為空。")
    if len(cleaned) > 20:
        raise ValueError("一次最多比較 20 檔股票。")

    semaphore = asyncio.Semaphore(6)

    async def analyze(symbol: str) -> dict[str, Any]:
        async with semaphore:
            quote, raw = await asyncio.gather(
                _get_intraday_quote(symbol),
                _get_daily_candles_raw(symbol, 180),
            )
        total = quote.get("total") or {}
        snapshot = {
            "symbol": symbol,
            "name": quote.get("name"),
            "market": quote.get("market"),
            "openPrice": quote.get("openPrice"),
            "highPrice": quote.get("highPrice"),
            "lowPrice": quote.get("lowPrice"),
            "closePrice": quote.get("closePrice") or quote.get("lastPrice"),
            "change": quote.get("change"),
            "changePercent": quote.get("changePercent"),
            "tradeVolume": total.get("tradeVolume"),
            "tradeValue": total.get("tradeValue"),
        }
        tech = _technical_features(symbol, raw.get("data", []))
        score, reasons, risks = _score_candidate(snapshot, tech, strategy)
        result: dict[str, Any] = {
            "symbol": symbol,
            "name": quote.get("name"),
            "score": score,
            "quote": snapshot,
            "technical": tech,
            "reasons": reasons,
            "risks": risks,
        }
        if include_chip:
            chip = await _safe_chip_for_ranking(symbol, 45)
            result["chip"] = chip
            result["score"] = round(score + _safe_float(chip.get("scoreAdjustment")), 2)
            result["reasons"] = (reasons + chip.get("reasons", []))[:8]
            result["risks"] = (risks + chip.get("risks", []))[:6]
        return result

    raw_results = await asyncio.gather(*[analyze(symbol) for symbol in cleaned], return_exceptions=True)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for symbol, result in zip(cleaned, raw_results):
        if isinstance(result, Exception):
            errors.append({"symbol": symbol, "error": str(result)})
        else:
            results.append(result)
    results.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)
    for rank, item in enumerate(results, start=1):
        item["rank"] = rank

    return {
        "strategy": strategy,
        "includeChip": include_chip,
        "results": results,
        "errors": errors,
        "source": "Fugle MarketData API v1.0；include_chip 時另用 FinMind API v4",
        "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
    }


@mcp.tool()
async def get_stock_full_analysis(
    symbol: str,
    include_distribution: bool = False,
) -> dict:
    """
    一次取得單檔股票的即時報價、180 日技術面、三大法人、融資融券、
    外資持股與借券成交。include_distribution=true 時另查股權分散；
    該資料可能需要 FinMind 付費方案。
    """
    symbol = _validate_symbol(symbol)

    calls = [
        _get_intraday_quote(symbol),
        _get_daily_candles_raw(symbol, 180),
        _get_institutional_data(symbol, 45),
        _get_margin_data(symbol, 45),
        _get_foreign_shareholding_data(symbol, 45),
        _get_lending_data(symbol, 30),
    ]
    labels = [
        "quote", "technicalRaw", "institutional", "marginShort",
        "foreignShareholding", "securitiesLending",
    ]
    if include_distribution:
        calls.append(_get_distribution_data(symbol, 120))
        labels.append("shareholdingDistribution")

    gathered = await asyncio.gather(*calls, return_exceptions=True)
    output: dict[str, Any] = {"symbol": symbol, "errors": {}}
    for label, result in zip(labels, gathered):
        if isinstance(result, Exception):
            output["errors"][label] = str(result)
        else:
            output[label] = result

    raw = output.pop("technicalRaw", None)
    if isinstance(raw, dict):
        try:
            output["technical"] = _technical_features(symbol, raw.get("data", []))
        except Exception as exc:
            output["errors"]["technical"] = str(exc)

    quote = output.get("quote") or {}
    technical = output.get("technical") or {}
    total = quote.get("total") or {}
    snapshot = {
        "symbol": symbol,
        "name": quote.get("name"),
        "market": quote.get("market"),
        "openPrice": quote.get("openPrice"),
        "highPrice": quote.get("highPrice"),
        "lowPrice": quote.get("lowPrice"),
        "closePrice": quote.get("closePrice") or quote.get("lastPrice"),
        "change": quote.get("change"),
        "changePercent": quote.get("changePercent"),
        "tradeVolume": total.get("tradeVolume"),
        "tradeValue": total.get("tradeValue"),
    }
    if technical:
        score, reasons, risks = _score_candidate(snapshot, technical, "balanced")
        output["screeningAssessment"] = {
            "balancedScore": score,
            "reasons": reasons,
            "risks": risks,
        }

    output["source"] = "Fugle MarketData API v1.0 + FinMind API v4"
    output["fetchedAtUtc"] = datetime.now(timezone.utc).isoformat()
    return output


@mcp.tool()
async def get_cache_status() -> dict:
    """查看快取後端、命中率、上游 API 實際呼叫次數與目前快取項目數。"""
    _memory_prune()
    client = await _get_redis_client()
    redis_entries: int | None = None
    if client is not None:
        try:
            count = 0
            async for _ in client.scan_iter(match=f"{CACHE_PREFIX}:*"):
                count += 1
                if count >= 10000:
                    break
            redis_entries = count
        except Exception:
            redis_entries = None

    hits = CACHE_STATS["memoryHits"] + CACHE_STATS["redisHits"]
    attempts = hits + CACHE_STATS["misses"]
    return {
        "backend": "memory+redis" if client is not None else "memory-only",
        "redisConfigured": bool(REDIS_URL),
        "redisConnected": client is not None,
        "redisError": _redis_error,
        "memoryEntries": len(_memory_cache),
        "redisEntries": redis_entries,
        "statsSinceProcessStart": {
            **CACHE_STATS,
            "hitRatePercent": round(hits / attempts * 100, 2) if attempts else 0.0,
            "estimatedUpstreamRequestsSaved": hits,
        },
        "ttlPolicySeconds": {
            "quoteDuringMarket": 10,
            "snapshotDuringMarket": 30,
            "historicalDuringMarket": 300,
            "afterCloseQuoteMax": 3600,
            "afterCloseSnapshotAndHistorical": "until next weekday 08:30 Asia/Taipei",
            "finmindUpdateWindow": 1200,
            "finmindDaytime": 7200,
            "finmindNight": 36000,
            "shareholdingDistribution": 86400,
        },
        "note": (
            "未設定 REDIS_URL 時使用記憶體快取；免費 Render 休眠或重新部署後會清空。"
            "設定 Render Key Value 的 REDIS_URL 後，可在服務運作期間跨請求共用快取。"
        ),
        "timeTaipei": _taipei_now().isoformat(),
    }


@mcp.tool()
async def clear_cache(scope: str = "all") -> dict:
    """
    清除快取。scope 支援 all、quote、snapshot、historical、finmind。
    一般情況不必清除；需要強制取得最新資料時使用。
    """
    scope = scope.lower().strip()
    namespace_map = {
        "quote": "fugle:quote",
        "snapshot": "fugle:snapshot",
        "historical": "fugle:historical",
        "finmind": "finmind:",
    }
    if scope not in {"all", *namespace_map.keys()}:
        raise ValueError("scope 僅支援 all、quote、snapshot、historical、finmind。")

    target = None if scope == "all" else namespace_map[scope]
    memory_deleted = 0
    for key, entry in list(_memory_cache.items()):
        namespace = str(entry.get("namespace", ""))
        if target is None or namespace.startswith(target):
            _memory_cache.pop(key, None)
            memory_deleted += 1

    redis_deleted = 0
    client = await _get_redis_client()
    if client is not None:
        pattern = f"{CACHE_PREFIX}:*" if target is None else f"{CACHE_PREFIX}:{target}*"
        batch: list[str] = []
        async for key in client.scan_iter(match=pattern):
            batch.append(key)
            if len(batch) >= 200:
                redis_deleted += int(await client.delete(*batch))
                batch.clear()
        if batch:
            redis_deleted += int(await client.delete(*batch))

    return {
        "ok": True,
        "scope": scope,
        "memoryDeleted": memory_deleted,
        "redisDeleted": redis_deleted,
        "timeTaipei": _taipei_now().isoformat(),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
