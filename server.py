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
TWSE_DAILY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY_CLOSE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
CACHE_PREFIX = os.environ.get("CACHE_PREFIX", "twstock:mcp:v9")
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
    "officialMarketUpstreamRequests": 0,
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
    # 保留函式名稱以相容舊版快取狀態欄位。
    return _official_market_ttl()


def _official_market_ttl() -> int:
    # 公開市場端點不是 Fugle 即時快照。盤中每 15 分鐘重抓一次；
    # 盤後資料固定，快取至下一個交易日早上。
    return 15 * 60 if _is_tw_market_hours() else _seconds_until_next_weekday_open()


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
        "moving averages, Bollinger Bands, official-market screening, institutional trading, "
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


async def _official_http_json(
    url: str,
    cache_tag: str,
    force_refresh: bool = False,
) -> Any:
    """讀取證交所／櫃買中心公開 JSON，並套用共用快取。"""

    async def fetch() -> Any:
        CACHE_STATS["officialMarketUpstreamRequests"] += 1
        headers = {
            "Accept": "application/json",
            "User-Agent": "TaiwanStockMCP/6.0 (+public-market-screener)",
        }
        async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)

        if response.status_code == 429:
            raise RuntimeError("官方市場資料服務目前要求過於頻繁，請稍後再試。")
        try:
            response.raise_for_status()
        except Exception:
            CACHE_STATS["upstreamErrors"] += 1
            raise

        try:
            return response.json()
        except Exception as exc:
            CACHE_STATS["upstreamErrors"] += 1
            raise RuntimeError("官方市場資料回傳的內容不是有效 JSON。") from exc

    return await _cached_call(
        "official:market",
        {"tag": cache_tag, "url": url},
        _official_market_ttl(),
        fetch,
        force_refresh=force_refresh,
    )


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _market_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else default

    text = str(value).strip()
    if not text or text in {"--", "---", "----", "N/A", "null", "None"}:
        return default

    text = (
        text.replace(",", "")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("−", "-")
        .replace("▲", "+")
        .replace("△", "+")
        .replace("▼", "-")
        .replace("▽", "-")
    )
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _signed_market_change(row: dict[str, Any]) -> float:
    raw = _first_value(
        row,
        "Change", "ChangeAmount", "PriceChange", "漲跌價差", "漲跌", "漲跌幅",
    )
    value = _market_number(raw)
    sign = str(_first_value(
        row,
        "ChangeSign", "UpDown", "Trend", "漲跌符號", "漲跌註記",
    ) or "").strip()

    negative_tokens = {"-", "－", "跌", "down", "DOWN", "red_down"}
    positive_tokens = {"+", "＋", "漲", "up", "UP", "red_up"}
    if sign in negative_tokens:
        return -abs(value)
    if sign in positive_tokens:
        return abs(value)
    return value


def _response_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "Data", "rows", "result", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]

    numbered = []
    for key, value in payload.items():
        if re.fullmatch(r"data\d+", str(key)) and isinstance(value, list):
            if value and isinstance(value[0], dict):
                numbered.extend(value)
    return numbered


def _normalize_official_row(
    row: dict[str, Any],
    market: str,
) -> dict[str, Any] | None:
    symbol = str(_first_value(
        row,
        "Code", "SecuritiesCompanyCode", "SecuritiesCode", "StockCode",
        "證券代號", "股票代號", "代號",
    ) or "").strip()

    # 排除 ETF（0 開頭）、權證與非四碼普通股。
    if not re.fullmatch(r"[1-9]\d{3}", symbol):
        return None

    name = str(_first_value(
        row,
        "Name", "CompanyName", "SecuritiesCompanyName", "StockName",
        "證券名稱", "股票名稱", "名稱",
    ) or "").strip()

    close = _market_number(_first_value(
        row, "ClosingPrice", "Close", "ClosePrice", "收盤價", "收盤",
    ))
    open_price = _market_number(_first_value(
        row, "OpeningPrice", "Open", "OpenPrice", "開盤價", "開盤",
    ))
    high = _market_number(_first_value(
        row, "HighestPrice", "High", "HighPrice", "最高價", "最高",
    ))
    low = _market_number(_first_value(
        row, "LowestPrice", "Low", "LowPrice", "最低價", "最低",
    ))
    volume = _market_number(_first_value(
        row, "TradeVolume", "TradingShares", "TradingVolume", "Volume",
        "成交股數", "成交量",
    ))
    trade_value = _market_number(_first_value(
        row, "TradeValue", "TransactionAmount", "TradingAmount", "Amount",
        "成交金額", "成交值",
    ))
    change = _signed_market_change(row)

    if close <= 0:
        return None
    if trade_value <= 0 and volume > 0:
        trade_value = close * volume

    previous_close = close - change
    change_percent = (
        change / previous_close * 100
        if previous_close > 0
        else 0.0
    )

    report_date = _first_value(
        row, "Date", "TradeDate", "ReportDate", "資料日期", "日期",
    )

    return {
        "symbol": symbol,
        "name": name,
        "market": market,
        "openPrice": open_price or None,
        "highPrice": high or None,
        "lowPrice": low or None,
        "closePrice": close,
        "change": change,
        "changePercent": round(change_percent, 4),
        "tradeVolume": int(round(volume)),
        "tradeValue": int(round(trade_value)),
        "date": str(report_date) if report_date not in (None, "") else None,
    }


async def _get_official_market_quotes(
    market: str,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """取得證交所或櫃買中心最新公開全市場日行情，不需要 Fugle Snapshot 權限。"""
    market = market.upper().strip()
    if market == "TSE":
        url = TWSE_DAILY_ALL_URL
        source = "TWSE OpenAPI STOCK_DAY_ALL"
    elif market == "OTC":
        url = TPEX_DAILY_CLOSE_URL
        source = "TPEx OpenAPI tpex_mainboard_daily_close_quotes"
    else:
        raise ValueError("market 僅支援 TSE 或 OTC。")

    payload = await _official_http_json(
        url,
        cache_tag=market,
        force_refresh=force_refresh,
    )
    rows = _response_rows(payload)
    normalized = []
    for row in rows:
        item = _normalize_official_row(row, market)
        if item is not None:
            normalized.append(item)

    if not normalized:
        sample_keys = sorted(rows[0].keys())[:20] if rows else []
        raise RuntimeError(
            f"{market} 官方市場資料目前沒有解析到普通股。"
            f"可能是上游尚未更新或欄位改版；sampleKeys={sample_keys}"
        )

    report_dates = sorted({
        str(item.get("date"))
        for item in normalized
        if item.get("date")
    })
    return {
        "market": market,
        "date": report_dates[-1] if report_dates else None,
        "time": None,
        "data": normalized,
        "source": source,
        "fetchedAtTaipei": _taipei_now().isoformat(),
        "freshness": "官方端點最新公布的全市場日行情，不是盤中逐筆即時快照。",
    }


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
        "server": "Taiwan Stock MCP v9-monitor-alerts",
        "version": "9.0.0-monitor",
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
            "screen_market_v2",
            "record_screen_result",
            "get_backtest_summary",
            "get_signal_performance",
            "get_theme_score",
            "update_theme_tags",
            "get_risk_flags",
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
    掃描上市與上櫃普通股。全市場初篩使用證交所／櫃買中心最新公開日行情，
    不需要 Fugle Snapshot Quotes 付費權限；候選股深度技術分析仍使用
    Fugle 個股歷史 K 線。

    strategy：balanced、breakout、early_stage、trend、pullback。
    markets：BOTH、TSE、OTC。
    include_chip=true 時，只為最後入選股補上法人與融資籌碼評分。

    注意：官方全市場資料不是盤中逐筆即時快照，收盤後使用最完整。
    """
    strategy = strategy.lower().strip()
    markets = markets.upper().strip()
    if strategy not in {"balanced", "breakout", "early_stage", "trend", "pullback"}:
        raise ValueError("strategy 僅支援 balanced、breakout、early_stage、trend、pullback。")
    if markets not in {"BOTH", "TSE", "OTC"}:
        raise ValueError("markets 僅支援 BOTH、TSE、OTC。")
    if not 1 <= top_n <= 20:
        raise ValueError("top_n 請填 1 到 20。")
    if not top_n <= candidate_limit <= 55:
        raise ValueError("candidate_limit 必須大於等於 top_n，且最多 55。")
    if min_trade_value < 0:
        raise ValueError("min_trade_value 不可為負數。")
    if min_price <= 0 or max_price <= min_price:
        raise ValueError("價格範圍設定不正確。")

    requested_markets = ["TSE", "OTC"] if markets == "BOTH" else [markets]
    market_payloads = await asyncio.gather(
        *[
            _get_official_market_quotes(
                market,
                force_refresh=force_refresh,
            )
            for market in requested_markets
        ]
    )

    universe: list[dict[str, Any]] = []
    as_of: list[dict[str, Any]] = []
    for market, payload in zip(requested_markets, market_payloads):
        as_of.append({
            "market": market,
            "date": payload.get("date"),
            "source": payload.get("source"),
            "fetchedAtTaipei": payload.get("fetchedAtTaipei"),
        })
        for row in payload.get("data", []):
            close = _safe_float(row.get("closePrice"))
            trade_value = _safe_float(row.get("tradeValue"))
            if not min_price <= close <= max_price:
                continue
            if trade_value < min_trade_value:
                continue
            universe.append(dict(row))

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
        *[
            _analyze_candidate(
                row,
                strategy,
                semaphore,
                force_refresh=force_refresh,
            )
            for row in candidates
        ],
        return_exceptions=True,
    )

    analyzed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for row, result in zip(candidates, results):
        if isinstance(result, Exception):
            errors.append({
                "symbol": str(row.get("symbol")),
                "error": str(result),
            })
        else:
            analyzed.append(result)

    analyzed.sort(
        key=lambda item: _safe_float(item.get("score")),
        reverse=True,
    )
    selected = analyzed[:top_n]

    if include_chip and selected:
        chip_results = await asyncio.gather(
            *[_safe_chip_for_ranking(item["symbol"], 45) for item in selected]
        )
        for item, chip in zip(selected, chip_results):
            item["chip"] = chip
            item["score"] = round(
                _safe_float(item.get("score"))
                + _safe_float(chip.get("scoreAdjustment")),
                2,
            )
            item["reasons"] = (
                item.get("reasons", []) + chip.get("reasons", [])
            )[:8]
            item["risks"] = (
                item.get("risks", []) + chip.get("risks", [])
            )[:6]
        selected.sort(
            key=lambda item: _safe_float(item.get("score")),
            reverse=True,
        )

    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank

    return {
        "strategy": strategy,
        "markets": requested_markets,
        "asOf": as_of,
        "marketDataMode": "official_latest_daily_market",
        "marketDataFreshness": (
            "全市場初篩依證交所／櫃買中心端點最新公布的日行情；"
            "不是 Fugle 盤中 Snapshot。收盤後執行最完整。"
        ),
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
        "cacheNote": (
            "官方市場行情、歷史 K 線與籌碼依 TTL 使用快取；"
            "force_refresh=true 可略過快取。"
        ),
        "results": selected,
        "errors": errors[:15],
        "method": (
            "證交所／櫃買中心最新公開日行情預篩，"
            "再對候選股計算 180 日技術指標；不是報酬保證。"
        ),
        "source": (
            "TWSE OpenAPI + TPEx OpenAPI；候選股歷史 K 線使用 "
            "Fugle MarketData API v1.0；include_chip 時另用 FinMind API v4"
        ),
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
            "officialMarketDuringMarket": 900,
            "historicalDuringMarket": 300,
            "afterCloseQuoteMax": 3600,
            "afterCloseOfficialMarketAndHistorical": "until next weekday 08:30 Asia/Taipei",
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
    清除快取。scope 支援 all、quote、market、snapshot、historical、finmind。
    一般情況不必清除；需要強制取得最新資料時使用。
    """
    scope = scope.lower().strip()
    namespace_map = {
        "quote": "fugle:quote",
        "market": "official:market",
        "snapshot": "official:market",
        "historical": "fugle:historical",
        "finmind": "finmind:",
    }
    if scope not in {"all", *namespace_map.keys()}:
        raise ValueError("scope 僅支援 all、quote、market、snapshot、historical、finmind。")

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

# =========================
# V8 free-score-tracker add-ons
# =========================
THEMES_FILE = os.environ.get("THEMES_FILE", "themes.json")
RISK_RULES_FILE = os.environ.get("RISK_RULES_FILE", "risk_rules.json")
BACKTEST_MAX_RECORDS = int(os.environ.get("BACKTEST_MAX_RECORDS", "300"))

_V8_MEMORY_STORE: dict[str, Any] = {
    "themeTags": None,
    "riskRules": None,
    "screenRecords": [],
}


def _read_json_file(filename: str, default: Any) -> Any:
    try:
        path = os.path.join(os.getcwd(), filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        pass
    return default


async def _v8_redis_get_json(key: str) -> Any | None:
    client = await _get_redis_client()
    if client is None:
        return None
    try:
        raw = await client.get(f"{CACHE_PREFIX}:v8store:{key}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _v8_redis_set_json(key: str, value: Any, ttl: int | None = None) -> bool:
    client = await _get_redis_client()
    if client is None:
        return False
    try:
        redis_key = f"{CACHE_PREFIX}:v8store:{key}"
        if ttl is None:
            await client.set(redis_key, _json_dumps(value))
        else:
            await client.set(redis_key, _json_dumps(value), ex=max(1, int(ttl)))
        return True
    except Exception:
        return False


def _normalize_theme_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for raw_symbol, raw_tags in value.items():
        try:
            symbol = _validate_symbol(str(raw_symbol))
        except Exception:
            continue
        if isinstance(raw_tags, str):
            tags = [part.strip() for part in re.split(r"[,，、/]+", raw_tags) if part.strip()]
        elif isinstance(raw_tags, list):
            tags = [str(item).strip() for item in raw_tags if str(item).strip()]
        else:
            tags = []
        if tags:
            out[symbol] = list(dict.fromkeys(tags))
    return out


async def _get_theme_map() -> dict[str, list[str]]:
    remote = await _v8_redis_get_json("themeTags")
    if remote is not None:
        return _normalize_theme_map(remote)
    if _V8_MEMORY_STORE.get("themeTags") is None:
        _V8_MEMORY_STORE["themeTags"] = _normalize_theme_map(_read_json_file(THEMES_FILE, {}))
    return dict(_V8_MEMORY_STORE.get("themeTags") or {})


async def _set_theme_map(theme_map: dict[str, list[str]]) -> dict[str, Any]:
    normalized = _normalize_theme_map(theme_map)
    _V8_MEMORY_STORE["themeTags"] = normalized
    persisted = await _v8_redis_set_json("themeTags", normalized)
    return {"savedToRedis": persisted, "count": len(normalized)}


async def _get_risk_rules() -> dict[str, Any]:
    remote = await _v8_redis_get_json("riskRules")
    if isinstance(remote, dict):
        return remote
    if _V8_MEMORY_STORE.get("riskRules") is None:
        _V8_MEMORY_STORE["riskRules"] = _read_json_file(RISK_RULES_FILE, {})
    rules = _V8_MEMORY_STORE.get("riskRules") or {}
    return rules if isinstance(rules, dict) else {}


def _same_theme_symbols(symbol: str, theme_map: dict[str, list[str]]) -> list[str]:
    tags = set(theme_map.get(symbol, []))
    if not tags:
        return []
    peers = []
    for other_symbol, other_tags in theme_map.items():
        if other_symbol != symbol and tags.intersection(other_tags):
            peers.append(other_symbol)
    return peers


async def _get_market_universe_for_theme(markets: str = "BOTH", force_refresh: bool = False) -> list[dict[str, Any]]:
    markets = markets.upper().strip()
    requested_markets = ["TSE", "OTC"] if markets == "BOTH" else [markets]
    payloads = await asyncio.gather(
        *[_get_official_market_quotes(market, force_refresh=force_refresh) for market in requested_markets],
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        if isinstance(payload, Exception):
            continue
        rows.extend(payload.get("data", []))
    return rows


async def _compute_theme_score(symbol: str, markets: str = "BOTH", force_refresh: bool = False) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    theme_map = await _get_theme_map()
    tags = theme_map.get(symbol, [])
    if not tags:
        return {
            "available": False,
            "scoreAdjustment": 0.0,
            "tags": [],
            "reasons": [],
            "risks": ["尚未建立題材標籤"],
        }

    peers = set(_same_theme_symbols(symbol, theme_map))
    universe = await _get_market_universe_for_theme(markets, force_refresh=force_refresh)
    peer_rows = [row for row in universe if str(row.get("symbol")) in peers]
    if not peer_rows:
        return {
            "available": True,
            "scoreAdjustment": min(4.0, len(tags) * 1.0),
            "tags": tags,
            "peerCount": len(peers),
            "activePeerCount": 0,
            "reasons": ["已有題材標籤，但同題材樣本不足"],
            "risks": [],
        }

    rising = [row for row in peer_rows if _safe_float(row.get("changePercent")) > 0]
    strong = [row for row in peer_rows if _safe_float(row.get("changePercent")) >= 5]
    limit_like = [row for row in peer_rows if _safe_float(row.get("changePercent")) >= 8.5]
    avg_change = mean([_safe_float(row.get("changePercent")) for row in peer_rows]) if peer_rows else 0.0
    total_value = sum(_safe_float(row.get("tradeValue")) for row in peer_rows)

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []
    rising_ratio = len(rising) / len(peer_rows) if peer_rows else 0.0
    if rising_ratio >= 0.6:
        score += 5
        reasons.append("同題材多數股票上漲")
    elif rising_ratio <= 0.3:
        score -= 3
        risks.append("同題材漲勢不整齊")
    if avg_change >= 2:
        score += 4
        reasons.append("同題材平均漲幅明顯")
    elif avg_change < -1:
        score -= 3
        risks.append("同題材平均轉弱")
    if strong:
        score += min(5, len(strong) * 2)
        reasons.append("同題材出現強勢股")
    if limit_like:
        score += 4
        reasons.append("同題材接近漲停股帶動熱度")
    if total_value >= 5_000_000_000:
        score += 2
        reasons.append("同題材成交值活躍")

    score = _clamp(score, -8, 15)
    top_peers = sorted(peer_rows, key=lambda row: _safe_float(row.get("changePercent")), reverse=True)[:5]
    return {
        "available": True,
        "scoreAdjustment": round(score, 2),
        "tags": tags,
        "peerCount": len(peers),
        "activePeerCount": len(peer_rows),
        "risingPeerCount": len(rising),
        "strongPeerCount": len(strong),
        "averagePeerChangePercent": _round(avg_change),
        "peerTradeValue": int(total_value),
        "topPeers": [
            {
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "changePercent": row.get("changePercent"),
                "tradeValue": row.get("tradeValue"),
            }
            for row in top_peers
        ],
        "reasons": reasons[:5],
        "risks": risks[:5],
    }


async def _compute_risk_flags(symbol: str, text: str = "") -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    rules = await _get_risk_rules()
    exclude_keywords = [str(x) for x in rules.get("exclude_keywords", [])]
    warning_keywords = [str(x) for x in rules.get("warning_keywords", [])]
    symbol_risks = rules.get("symbol_risks", {}) if isinstance(rules.get("symbol_risks", {}), dict) else {}
    score_penalty = rules.get("score_penalty", {}) if isinstance(rules.get("score_penalty", {}), dict) else {}

    haystack = text or ""
    matched_exclude = [kw for kw in exclude_keywords if kw and kw in haystack]
    matched_warning = [kw for kw in warning_keywords if kw and kw in haystack]
    manual = symbol_risks.get(symbol, [])
    if isinstance(manual, str):
        manual = [manual]
    elif not isinstance(manual, list):
        manual = []

    penalty = 0.0
    if matched_exclude:
        penalty -= 30
    if matched_warning:
        penalty -= min(20, 5 * len(matched_warning))
    if manual:
        penalty -= min(20, 6 * len(manual))
    # 靜態規則表可放入自訂項目，例如 {"attention_stock": -8}
    if manual:
        for item in manual:
            key = str(item).strip()
            if key in score_penalty:
                penalty += _safe_float(score_penalty.get(key))

    action = "allow"
    if matched_exclude:
        action = "exclude"
    elif matched_warning or manual:
        action = "warning"

    return {
        "symbol": symbol,
        "action": action,
        "scoreAdjustment": round(penalty, 2),
        "matchedExcludeKeywords": matched_exclude,
        "matchedWarningKeywords": matched_warning,
        "manualRisks": manual,
        "note": "V8 免費版風險過濾以自訂關鍵字與手動風險清單為主；即時公告解析可在下一版再加強。",
    }


def _date_key_from_screen_payload(payload: dict[str, Any]) -> str:
    as_of = payload.get("asOf") or []
    if isinstance(as_of, list) and as_of:
        first_date = as_of[0].get("date") if isinstance(as_of[0], dict) else None
        if first_date:
            return re.sub(r"[^0-9]", "", str(first_date))[:8]
    return _taipei_now().strftime("%Y%m%d")


async def _load_screen_records() -> list[dict[str, Any]]:
    remote = await _v8_redis_get_json("screenRecords")
    if isinstance(remote, list):
        _V8_MEMORY_STORE["screenRecords"] = remote[-BACKTEST_MAX_RECORDS:]
    return list(_V8_MEMORY_STORE.get("screenRecords") or [])


async def _save_screen_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = records[-BACKTEST_MAX_RECORDS:]
    _V8_MEMORY_STORE["screenRecords"] = records
    persisted = await _v8_redis_set_json("screenRecords", records)
    return {"savedToRedis": persisted, "recordCount": len(records)}


async def _record_screen_payload(payload: dict[str, Any], note: str = "") -> dict[str, Any]:
    records = await _load_screen_records()
    date_key = _date_key_from_screen_payload(payload)
    strategy = str(payload.get("strategy", "unknown"))
    run_id = f"RUN-{date_key}-{strategy}-{len(records)+1:04d}"
    items = []
    for item in payload.get("results", []):
        symbol = str(item.get("symbol"))
        rank = int(item.get("rank", len(items) + 1) or len(items) + 1)
        signal_id = f"SIG-{date_key}-{strategy}-{rank:02d}-{symbol}"
        items.append({
            "signalId": signal_id,
            "rank": rank,
            "symbol": symbol,
            "name": item.get("name"),
            "score": item.get("score"),
            "closePrice": item.get("closePrice"),
            "reasons": item.get("reasons", []),
            "risks": item.get("risks", []),
            "theme": item.get("theme"),
            "riskFlags": item.get("riskFlags"),
            "technical": item.get("technical", {}),
        })
    record = {
        "runId": run_id,
        "dateKey": date_key,
        "strategy": strategy,
        "markets": payload.get("markets"),
        "createdAtTaipei": _taipei_now().isoformat(),
        "note": note,
        "items": items,
    }
    records.append(record)
    storage = await _save_screen_records(records)
    return {"ok": True, "runId": run_id, "dateKey": date_key, "itemCount": len(items), **storage}


def _parse_date_key(date_key: str) -> date | None:
    try:
        digits = re.sub(r"[^0-9]", "", str(date_key))[:8]
        return datetime.strptime(digits, "%Y%m%d").date()
    except Exception:
        return None


def _returns_from_candles(candles: list[dict[str, Any]], signal_date: date | None, horizons: list[int]) -> dict[str, Any]:
    valid = []
    for row in candles:
        try:
            d = datetime.fromisoformat(str(row.get("date"))[:10]).date()
            c = _safe_float(row.get("close"))
            h = _safe_float(row.get("high"))
            l = _safe_float(row.get("low"))
            if c > 0:
                valid.append({"date": d, "close": c, "high": h, "low": l})
        except Exception:
            continue
    if not valid:
        return {"available": False, "error": "no candle data"}
    start_index = None
    if signal_date is not None:
        for i, row in enumerate(valid):
            if row["date"] >= signal_date:
                start_index = i
                break
    if start_index is None:
        return {"available": False, "error": "signal date not found"}
    base = valid[start_index]["close"]
    out: dict[str, Any] = {
        "available": True,
        "baseDate": valid[start_index]["date"].isoformat(),
        "baseClose": _round(base),
        "returns": {},
    }
    future = valid[start_index + 1:]
    if future:
        max_high = max(row["high"] for row in future[: max(horizons)])
        min_low = min(row["low"] for row in future[: max(horizons)])
        out["maxFavorablePercent"] = _round((max_high / base - 1) * 100)
        out["maxAdversePercent"] = _round((min_low / base - 1) * 100)
    for horizon in horizons:
        idx = start_index + horizon
        key = f"d{horizon}"
        if idx < len(valid):
            out["returns"][key] = _round((valid[idx]["close"] / base - 1) * 100)
        else:
            out["returns"][key] = None
    return out


async def _performance_for_signal(record: dict[str, Any], item: dict[str, Any], horizons: list[int]) -> dict[str, Any]:
    symbol = _validate_symbol(str(item.get("symbol")))
    signal_date = _parse_date_key(str(record.get("dateKey")))
    raw = await _get_daily_candles_raw(symbol, days=365)
    perf = _returns_from_candles(raw.get("data", []), signal_date, horizons)
    return {
        "signalId": item.get("signalId"),
        "runId": record.get("runId"),
        "dateKey": record.get("dateKey"),
        "strategy": record.get("strategy"),
        "rank": item.get("rank"),
        "symbol": symbol,
        "name": item.get("name"),
        "score": item.get("score"),
        **perf,
    }


@mcp.tool()
async def get_theme_score(symbol: str, markets: str = "BOTH", force_refresh: bool = False) -> dict:
    """取得單檔股票的題材標籤與同題材強度分數。"""
    symbol = _validate_symbol(symbol)
    return await _compute_theme_score(symbol, markets=markets, force_refresh=force_refresh)


@mcp.tool()
async def update_theme_tags(symbol: str, tags: list[str], mode: str = "replace") -> dict:
    """
    新增或更新股票題材標籤。mode 支援 replace、append、remove。
    優先保存到 Redis；若未設定 Redis，僅在服務重啟前暫存於記憶體。
    """
    symbol = _validate_symbol(symbol)
    mode = mode.lower().strip()
    if mode not in {"replace", "append", "remove"}:
        raise ValueError("mode 僅支援 replace、append、remove。")
    cleaned_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    theme_map = await _get_theme_map()
    existing = theme_map.get(symbol, [])
    if mode == "replace":
        theme_map[symbol] = list(dict.fromkeys(cleaned_tags))
    elif mode == "append":
        theme_map[symbol] = list(dict.fromkeys(existing + cleaned_tags))
    else:
        remove_set = set(cleaned_tags)
        theme_map[symbol] = [tag for tag in existing if tag not in remove_set]
        if not theme_map[symbol]:
            theme_map.pop(symbol, None)
    storage = await _set_theme_map(theme_map)
    return {"ok": True, "symbol": symbol, "tags": theme_map.get(symbol, []), **storage}


@mcp.tool()
async def get_risk_flags(symbol: str, text: str = "") -> dict:
    """
    依 risk_rules.json 與手動風險清單檢查單檔風險。
    text 可貼公告或新聞標題；免費版尚未自動抓完整公告全文。
    """
    symbol = _validate_symbol(symbol)
    return await _compute_risk_flags(symbol, text=text)


@mcp.tool()
async def screen_market_v2(
    strategy: str = "early_stage",
    markets: str = "BOTH",
    top_n: int = 10,
    candidate_limit: int = 60,
    min_trade_value: int = 100000000,
    min_price: float = 10.0,
    max_price: float = 5000.0,
    include_chip: bool = True,
    include_theme: bool = True,
    include_risk: bool = True,
    record_result: bool = True,
    force_refresh: bool = False,
) -> dict:
    """
    V8 勝率加強版選股：先沿用 screen_market 做技術與籌碼篩選，
    再加入題材/族群熱度與風險扣分，並可自動記錄結果供後續回測。
    """
    base = await screen_market(
        strategy=strategy,
        markets=markets,
        top_n=top_n,
        candidate_limit=min(candidate_limit, 55),
        min_trade_value=min_trade_value,
        min_price=min_price,
        max_price=max_price,
        include_chip=include_chip,
        force_refresh=force_refresh,
    )
    enhanced = []
    for item in base.get("results", []):
        item = dict(item)
        original_score = _safe_float(item.get("score"))
        total_adjustment = 0.0
        if include_theme:
            theme = await _compute_theme_score(str(item.get("symbol")), markets=markets, force_refresh=force_refresh)
            item["theme"] = theme
            total_adjustment += _safe_float(theme.get("scoreAdjustment"))
            item["reasons"] = (item.get("reasons", []) + theme.get("reasons", []))[:10]
            item["risks"] = (item.get("risks", []) + theme.get("risks", []))[:8]
        if include_risk:
            # 目前以手動 risk_rules 與既有風險文字做關鍵字檢查。
            risk_text = " ".join([str(x) for x in item.get("risks", [])])
            risk = await _compute_risk_flags(str(item.get("symbol")), text=risk_text)
            item["riskFlags"] = risk
            total_adjustment += _safe_float(risk.get("scoreAdjustment"))
            if risk.get("action") == "exclude":
                item["excludedByRisk"] = True
        item["baseScore"] = round(original_score, 2)
        item["v8Adjustment"] = round(total_adjustment, 2)
        item["score"] = round(original_score + total_adjustment, 2)
        enhanced.append(item)

    enhanced = [item for item in enhanced if not item.get("excludedByRisk")]
    enhanced.sort(key=lambda row: _safe_float(row.get("score")), reverse=True)
    for rank, item in enumerate(enhanced, start=1):
        item["rank"] = rank
    base["results"] = enhanced[:top_n]
    base["v8"] = {
        "version": "9.0.0-monitor",
        "includeTheme": include_theme,
        "includeRisk": include_risk,
        "recordResult": record_result,
        "note": "V8 免費版先強化盤後選股、題材分數、手動風險過濾與回測追蹤；不是獲利保證。",
    }
    if record_result:
        base["record"] = await _record_screen_payload(base, note="screen_market_v2 auto record")
    return base


@mcp.tool()
async def record_screen_result(screen_result: dict, note: str = "") -> dict:
    """手動記錄一次 screen_market 或 screen_market_v2 的結果，供後續回測統計。"""
    if not isinstance(screen_result, dict) or not isinstance(screen_result.get("results"), list):
        raise ValueError("screen_result 必須是包含 results 陣列的選股結果。")
    return await _record_screen_payload(screen_result, note=note)


@mcp.tool()
async def get_backtest_summary(strategy: str = "early_stage", lookback_records: int = 60) -> dict:
    """
    統計已記錄選股訊號的隔日、3日、5日表現。
    需要先用 screen_market_v2(record_result=true) 或 record_screen_result 累積資料。
    """
    strategy = strategy.lower().strip()
    records = await _load_screen_records()
    matched = [record for record in records if str(record.get("strategy", "")).lower() == strategy]
    matched = matched[-max(1, min(lookback_records, 120)):]
    if not matched:
        return {
            "available": False,
            "strategy": strategy,
            "recordCount": 0,
            "message": "目前尚未累積此策略的選股紀錄。請先用 screen_market_v2 並設定 record_result=true。",
        }
    tasks = []
    for record in matched:
        for item in record.get("items", []):
            tasks.append(_performance_for_signal(record, item, horizons=[1, 3, 5]))
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    rows = [row for row in raw if isinstance(row, dict) and row.get("available")]
    if not rows:
        return {
            "available": False,
            "strategy": strategy,
            "recordCount": len(matched),
            "message": "已找到紀錄，但歷史K線尚不足以計算報酬，可能是訊號太新或資料尚未更新。",
        }

    def collect(key: str) -> list[float]:
        values = []
        for row in rows:
            value = (row.get("returns") or {}).get(key)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    summary: dict[str, Any] = {}
    for key in ["d1", "d3", "d5"]:
        values = collect(key)
        if values:
            summary[key] = {
                "sampleSize": len(values),
                "winRatePercent": _round(sum(1 for value in values if value > 0) / len(values) * 100),
                "averageReturnPercent": _round(mean(values)),
                "medianReturnPercent": _round(sorted(values)[len(values)//2]),
                "bestPercent": _round(max(values)),
                "worstPercent": _round(min(values)),
            }
        else:
            summary[key] = {"sampleSize": 0}

    adverse = [row.get("maxAdversePercent") for row in rows if isinstance(row.get("maxAdversePercent"), (int, float))]
    favorable = [row.get("maxFavorablePercent") for row in rows if isinstance(row.get("maxFavorablePercent"), (int, float))]
    return {
        "available": True,
        "strategy": strategy,
        "recordCount": len(matched),
        "signalCount": len(rows),
        "summary": summary,
        "averageMaxFavorablePercent": _round(mean(favorable)) if favorable else None,
        "averageMaxAdversePercent": _round(mean(adverse)) if adverse else None,
        "recentSignals": rows[-10:],
        "note": "此回測使用已記錄訊號與後續日K收盤價估算，尚未扣除交易成本、滑價與實際進出場規則。",
    }


@mcp.tool()
async def get_signal_performance(signal_id: str) -> dict:
    """查詢單一已記錄訊號的後續表現。"""
    signal_id = str(signal_id).strip()
    records = await _load_screen_records()
    for record in records:
        for item in record.get("items", []):
            if str(item.get("signalId")) == signal_id:
                return await _performance_for_signal(record, item, horizons=[1, 3, 5, 10])
    return {"available": False, "signalId": signal_id, "message": "找不到此 signal_id。"}


@mcp.tool()
async def send_test_notification(message: str = "") -> dict:
    """V9：傳送一則 Telegram 測試通知。需要先設定 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID。"""
    from notifications import build_test_message, send_telegram_message
    text = message.strip() if isinstance(message, str) and message.strip() else build_test_message()
    data = await send_telegram_message(text)
    return {
        "ok": True,
        "message": "Telegram 測試通知已送出。請檢查手機 Telegram。",
        "telegramOk": bool(data.get("ok")),
        "chatIdConfigured": bool(os.environ.get("TELEGRAM_CHAT_ID")),
        "botTokenConfigured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    }


@mcp.tool()
async def get_telegram_setup_status(limit: int = 5) -> dict:
    from notifications import get_telegram_updates

    token_value = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_value = os.environ.get("TELEGRAM_CHAT_ID", "")

    debug_env = {
    "telegramBotTokenInEnv": "TELEGRAM_BOT_TOKEN" in os.environ,
    "telegramBotTokenLength": len(token_value),
    "telegramBotTokenStrippedLength": len(token_value.strip()),
    "telegramChatIdInEnv": "TELEGRAM_CHAT_ID" in os.environ,
    "telegramChatIdLength": len(chat_value),
    "telegramChatIdStrippedLength": len(chat_value.strip()),
    "testEnv": os.environ.get("TEST_ENV", "NOT_FOUND"),
    "matchedEnvKeys": sorted([k for k in os.environ.keys() if "TELEGRAM" in k or "TEST" in k or "FUGLE" in k or "FINMIND" in k]),
}

    token_configured = bool(token_value.strip())
    chat_configured = bool(chat_value.strip())

    if not token_configured:
        return {
            "ok": False,
            "botTokenConfigured": False,
            "chatIdConfigured": chat_configured,
            "message": "尚未設定 TELEGRAM_BOT_TOKEN。請先用 BotFather 建立 Bot 並把 Token 放到 Render Environment。",
            "debug": debug_env,
        }

    try:
        updates = await get_telegram_updates(limit=limit)
    except Exception as exc:
        return {
            "ok": False,
            "botTokenConfigured": token_configured,
            "chatIdConfigured": chat_configured,
            "message": f"已讀到 TELEGRAM_BOT_TOKEN，但呼叫 Telegram getUpdates 失敗：{exc}",
            "debug": debug_env,
        }

    chats = []
    for item in updates.get("result", []):
        msg = item.get("message") or item.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats.append({
                "chatId": chat.get("id"),
                "type": chat.get("type"),
                "firstName": chat.get("first_name"),
                "username": chat.get("username"),
                "text": msg.get("text"),
            })

    return {
        "ok": True,
        "botTokenConfigured": token_configured,
        "chatIdConfigured": chat_configured,
        "configuredChatId": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "recentChats": chats[-10:],
        "message": "若 recentChats 有 chatId，請把它填到 Render 的 TELEGRAM_CHAT_ID。",
        "debug": debug_env,
    }


@mcp.tool()
async def get_monitor_config() -> dict:
    """V9：查看盤中監測設定與 Render 環境變數是否齊全。"""
    path = os.environ.get("MONITOR_RULES_FILE", "monitor_rules.json")
    config: dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    env_watchlist = os.environ.get("MONITOR_WATCHLIST", "")
    return {
        "ok": True,
        "version": "9.0.0-monitor",
        "rulesFile": path,
        "rulesFileExists": os.path.exists(path),
        "config": config,
        "environment": {
            "MONITOR_ENABLED": os.environ.get("MONITOR_ENABLED", ""),
            "MONITOR_WATCHLIST": env_watchlist,
            "MONITOR_POLL_SECONDS": os.environ.get("MONITOR_POLL_SECONDS", ""),
            "ALERT_COOLDOWN_SECONDS": os.environ.get("ALERT_COOLDOWN_SECONDS", ""),
            "TELEGRAM_BOT_TOKEN_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
            "TELEGRAM_CHAT_ID_configured": bool(os.environ.get("TELEGRAM_CHAT_ID")),
            "FUGLE_API_KEY_configured": bool(os.environ.get("FUGLE_API_KEY")),
        },
        "note": "免費版建議最多5檔，每檔用成交/報價輪詢；正式盤中監測由 Render Background Worker 執行。",
    }


@mcp.tool()
async def preview_order(
    symbol: str,
    side: str,
    entry_price: float,
    stop_price: float,
    budget: float = 50000,
    max_risk: float = 500,
    day_trade: bool = True,
    odd_lot: bool = True,
) -> dict:
    """V9：下單預覽，只計算股數、成本與風險，不會送出委託。"""
    from order_preview import build_order_preview
    return build_order_preview(symbol, side, entry_price, stop_price, budget, max_risk, day_trade, odd_lot)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
