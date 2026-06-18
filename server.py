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
CACHE_PREFIX = os.environ.get("CACHE_PREFIX", "twstock:mcp:v7-free")
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


# V7-free: stay below Fugle Basic historical-data limit and keep Render Free stable.
V7_HISTORY_CALLS_PER_MINUTE = min(
    55,
    max(1, int(os.environ.get("V7_HISTORY_CALLS_PER_MINUTE", "55"))),
)
V7_HISTORY_CONCURRENCY = max(
    1,
    min(6, int(os.environ.get("V7_HISTORY_CONCURRENCY", "3"))),
)
V7_CHIP_CONCURRENCY = max(
    1,
    min(5, int(os.environ.get("V7_CHIP_CONCURRENCY", "2"))),
)
V7_MIN_UNIVERSE_COUNT = int(os.environ.get("V7_MIN_UNIVERSE_COUNT", "1800"))
V7_MIN_TSE_COUNT = int(os.environ.get("V7_MIN_TSE_COUNT", "950"))
V7_MIN_OTC_COUNT = int(os.environ.get("V7_MIN_OTC_COUNT", "750"))
V7_REFERENCE_SYMBOL = os.environ.get("V7_REFERENCE_SYMBOL", "2330").strip() or "2330"

_history_rate_lock = asyncio.Lock()
_history_request_times: list[float] = []


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
    # 盤中每 15 分鐘重抓。收盤後官方資料可能分批更新，18:30 前維持短 TTL；
    # 避免 13:45 剛抓到前一日資料後，被快取到下一個交易日。
    now = _taipei_now()
    if _is_tw_market_hours(now):
        return 15 * 60
    if now.weekday() < 5 and dt_time(13, 45) < now.time() < dt_time(18, 30):
        return 5 * 60
    return _seconds_until_next_weekday_open(now)


def _historical_ttl() -> int:
    # Fugle 日 K 在收盤後仍可能更新；18:30 前保持短 TTL。
    now = _taipei_now()
    if _is_tw_market_hours(now):
        return 300
    if now.weekday() < 5 and dt_time(13, 45) < now.time() < dt_time(18, 30):
        return 5 * 60
    return _seconds_until_next_weekday_open(now)


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
    end = _taipei_now().date()
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


async def _get_institutional_data(
    symbol: str,
    days: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    start, end = _days_range(days, 5, 365)
    rows = await _finmind_get(
        "TaiwanStockInstitutionalInvestorsBuySellWide",
        symbol,
        start,
        end,
        force_refresh=force_refresh,
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


async def _get_margin_data(
    symbol: str,
    days: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    start, end = _days_range(days, 5, 365)
    rows = await _finmind_get(
        "TaiwanStockMarginPurchaseShortSale",
        symbol,
        start,
        end,
        force_refresh=force_refresh,
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



def _normalize_trade_date(value: Any) -> str | None:
    """Normalize ISO, YYYYMMDD, and ROC YYYMMDD dates to YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(TAIPEI_TZ).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    raw = str(value).strip()
    if not raw:
        return None

    iso_match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        except ValueError:
            return None
    if len(digits) == 7:
        try:
            return date(
                int(digits[:3]) + 1911,
                int(digits[3:5]),
                int(digits[5:7]),
            ).isoformat()
        except ValueError:
            return None
    return None


def _quote_reference_date(quote: dict[str, Any]) -> str | None:
    for candidate in (
        quote.get("date"),
        (quote.get("lastTrade") or {}).get("date")
        if isinstance(quote.get("lastTrade"), dict)
        else None,
    ):
        normalized = _normalize_trade_date(candidate)
        if normalized:
            return normalized

    updated = quote.get("lastUpdated")
    try:
        if updated is not None:
            epoch = float(updated)
            if epoch > 10_000_000_000:
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, TAIPEI_TZ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    return None


def _close_location(row: dict[str, Any]) -> float:
    high = _safe_float(row.get("highPrice"))
    low = _safe_float(row.get("lowPrice"))
    close = _safe_float(row.get("closePrice"))
    if high <= low:
        return 0.5
    return _clamp((close - low) / (high - low), 0.0, 1.0)


def _quick_prefilter_score(row: dict[str, Any], strategy: str) -> float:
    value = _safe_float(row.get("tradeValue"))
    change = _safe_float(row.get("changePercent"))
    close_loc = _close_location(row)
    liquidity = math.log10(max(value, 1))
    score = liquidity + close_loc * 2.5

    if strategy == "pullback":
        if -4.0 <= change <= 1.5:
            score += 4.0 - abs(change + 0.5) * 0.4
        elif change > 6.5:
            score -= 5.0
    elif strategy == "breakout":
        score += _clamp(change, 0, 8) * 1.1
        if change >= 9:
            score -= 8
    else:
        if 0.8 <= change <= 6.5:
            score += 5.0 + change * 0.5
        elif -0.8 <= change < 0.8:
            score += 2.0
        elif change >= 9:
            score -= 7.0
        elif change <= -5:
            score -= 5.0
    return score


def _main_uptrend_stage(score: float) -> tuple[int, str]:
    normalized = int(round(_clamp(score, 0.0, 100.0)))
    if normalized >= 78:
        stage = "主升段啟動候選"
    elif normalized >= 62:
        stage = "轉強觀察"
    else:
        stage = "尚未確認"
    return normalized, stage


def _build_multilane_candidates(
    rows: list[dict[str, Any]],
    strategy: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Create a diversified same-day pool instead of one momentum-only ranking."""
    selected: dict[str, dict[str, Any]] = {}
    lane_size = max(12, math.ceil(limit * 0.55))

    def add(items: list[dict[str, Any]], target_size: int) -> None:
        for item in items:
            selected[str(item.get("symbol"))] = item
            if len(selected) >= target_size:
                break

    if strategy == "pullback":
        lane1 = [row for row in rows if -4.0 <= _safe_float(row.get("changePercent")) <= 1.5]
    elif strategy == "breakout":
        lane1 = [row for row in rows if 1.5 <= _safe_float(row.get("changePercent")) <= 8.5]
    else:
        lane1 = [row for row in rows if 0.8 <= _safe_float(row.get("changePercent")) <= 7.5]
    lane1.sort(
        key=lambda row: (
            _quick_prefilter_score(row, strategy),
            _safe_float(row.get("tradeValue")),
        ),
        reverse=True,
    )
    add(lane1, lane_size)

    strong_close = [
        row for row in rows
        if -1.5 <= _safe_float(row.get("changePercent")) <= 6.5
        and _close_location(row) >= 0.72
    ]
    strong_close.sort(
        key=lambda row: (_close_location(row), _safe_float(row.get("tradeValue"))),
        reverse=True,
    )
    add(strong_close, lane_size * 2)

    liquid = sorted(
        rows,
        key=lambda row: _safe_float(row.get("tradeValue")),
        reverse=True,
    )
    add(liquid, lane_size * 3)

    quiet_turn = [
        row for row in rows
        if -0.8 <= _safe_float(row.get("changePercent")) <= 3.0
        and _close_location(row) >= 0.62
    ]
    quiet_turn.sort(
        key=lambda row: (_close_location(row), _safe_float(row.get("tradeValue"))),
        reverse=True,
    )
    add(quiet_turn, lane_size * 4)

    ranked = sorted(
        selected.values(),
        key=lambda row: _quick_prefilter_score(row, strategy),
        reverse=True,
    )

    if len(ranked) < limit:
        present = {str(row.get("symbol")) for row in ranked}
        remainder = [row for row in rows if str(row.get("symbol")) not in present]
        remainder.sort(
            key=lambda row: _quick_prefilter_score(row, strategy),
            reverse=True,
        )
        ranked.extend(remainder[: limit - len(ranked)])

    return ranked[:limit]


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
            "User-Agent": "TaiwanStockMCP/7.0-free (+official-same-day-screener)",
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
        normalized_date
        for item in normalized
        if (normalized_date := _normalize_trade_date(item.get("date")))
    })
    if len(report_dates) > 1:
        raise RuntimeError(
            f"{market} 官方市場資料包含多個交易日期：{report_dates[-5:]}"
        )
    return {
        "market": market,
        "date": report_dates[0] if report_dates else None,
        "time": None,
        "data": normalized,
        "source": source,
        "fetchedAtTaipei": _taipei_now().isoformat(),
        "freshness": "官方端點最新公布的全市場日行情，不是盤中逐筆即時快照。",
    }


async def _acquire_history_rate_slot() -> None:
    """Limit only actual Fugle historical upstream calls, not cache hits."""
    while True:
        async with _history_rate_lock:
            now = time.monotonic()
            cutoff = now - 60.0
            _history_request_times[:] = [
                stamp for stamp in _history_request_times if stamp > cutoff
            ]
            if len(_history_request_times) < V7_HISTORY_CALLS_PER_MINUTE:
                _history_request_times.append(now)
                return
            delay = _history_request_times[0] + 60.0 - now
        await asyncio.sleep(max(delay, 0.05))


async def _get_historical_candles_raw(
    symbol: str,
    days: int = 180,
    timeframe: str = "D",
    adjusted: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    symbol = _validate_symbol(symbol)
    timeframe = timeframe.upper().strip()
    end = _taipei_now().date()
    start = end - timedelta(days=days)
    params = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "timeframe": timeframe,
        "adjusted": str(adjusted).lower(),
        "fields": "open,high,low,close,volume,turnover,change",
        "sort": "asc",
    }
    async def fetch_history() -> dict[str, Any]:
        await _acquire_history_rate_slot()
        return await _fugle_get(f"historical/candles/{symbol}", params=params)

    return await _cached_call(
        "fugle:historical",
        {"symbol": symbol, **params},
        _historical_ttl(),
        fetch_history,
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



async def _get_reference_market_date(force_refresh: bool = False) -> str | None:
    """Use one free Fugle intraday quote to verify the official market date is latest."""
    try:
        quote = await _get_intraday_quote(
            V7_REFERENCE_SYMBOL,
            force_refresh=force_refresh,
        )
        reference = _quote_reference_date(quote)
        if reference:
            return reference
    except Exception:
        pass

    # Fallback to a short historical query if the quote did not contain a usable date.
    try:
        raw = await _get_daily_candles_raw(
            V7_REFERENCE_SYMBOL,
            days=30,
            force_refresh=force_refresh,
        )
        candles = raw.get("data") or []
        if candles:
            return _normalize_trade_date(candles[-1].get("date"))
    except Exception:
        pass
    return None


def _upsert_official_candle(
    candles: list[dict[str, Any]],
    snapshot: dict[str, Any],
    scoring_date: str,
) -> list[dict[str, Any]]:
    close = _safe_float(snapshot.get("closePrice"))
    if close <= 0:
        raise RuntimeError(f"{snapshot.get('symbol')} 官方收盤價無效。")

    today = {
        "date": scoring_date,
        "open": _safe_float(snapshot.get("openPrice"), close) or close,
        "high": _safe_float(snapshot.get("highPrice"), close) or close,
        "low": _safe_float(snapshot.get("lowPrice"), close) or close,
        "close": close,
        "volume": int(_safe_float(snapshot.get("tradeVolume"))),
        "turnover": int(_safe_float(snapshot.get("tradeValue"))),
        "change": _safe_float(snapshot.get("change")),
    }

    merged = [
        dict(row)
        for row in candles
        if _normalize_trade_date(row.get("date")) != scoring_date
    ]
    for row in merged:
        normalized = _normalize_trade_date(row.get("date"))
        if normalized:
            row["date"] = normalized
    merged.append(today)
    merged.sort(key=lambda row: str(row.get("date") or ""))
    return merged


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


async def _safe_chip_for_ranking(
    symbol: str,
    days: int = 45,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        institutional, margin = await asyncio.gather(
            _get_institutional_data(symbol, days, force_refresh=force_refresh),
            _get_margin_data(symbol, days, force_refresh=force_refresh),
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
    scoring_date: str,
    semaphore: asyncio.Semaphore,
    force_refresh: bool = False,
) -> dict[str, Any]:
    symbol = _validate_symbol(str(snapshot.get("symbol", "")))
    async with semaphore:
        raw = await _get_daily_candles_raw(
            symbol,
            180,
            force_refresh=force_refresh,
        )
    candles = _upsert_official_candle(
        raw.get("data", []),
        snapshot,
        scoring_date,
    )
    tech = _technical_features(symbol, candles)
    latest_date = _normalize_trade_date(tech.get("latestDate"))
    if latest_date != scoring_date:
        raise RuntimeError(
            f"技術資料日期不一致：{latest_date} != {scoring_date}"
        )
    tech["latestDate"] = scoring_date
    score, reasons, risks = _score_candidate(snapshot, tech, strategy)
    main_uptrend_score, stage = _main_uptrend_stage(score)
    return {
        "symbol": symbol,
        "name": snapshot.get("name"),
        "market": snapshot.get("market"),
        "quoteDate": scoring_date,
        "closePrice": snapshot.get("closePrice"),
        "changePercent": snapshot.get("changePercent"),
        "tradeVolume": snapshot.get("tradeVolume"),
        "tradeValue": snapshot.get("tradeValue"),
        "score": score,
        "mainUptrendScore": main_uptrend_score,
        "stage": stage,
        "reasons": reasons,
        "risks": risks,
        "technical": tech,
    }


@mcp.tool()
def ping() -> dict:
    """檢查台股 MCP 伺服器是否正常運作。"""
    return {
        "ok": True,
        "server": "Taiwan Stock MCP v7-free-official-same-day",
        "version": "7.0.0-free",
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
    V7 免費版全市場篩選：先使用證交所／櫃買中心「同一交易日」官方行情
    掃描全部普通股，再對候選股使用 Fugle 免費歷史 K 線做技術分析。

    strategy：balanced、breakout、early_stage、trend、pullback。
    markets：BOTH、TSE、OTC。
    candidate_limit：深度技術分析檔數，免費版最多 55。
    include_chip=true：只對技術初評前 top_n 檔補法人與融資籌碼。

    若上市、上櫃日期不同，或官方日期落後 Fugle 最新交易日，會拒絕排名，
    避免把前一交易日漲幅誤當成當日資料。
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
        raise ValueError("candidate_limit 必須大於等於 top_n，且免費版最多 55。")
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

    as_of: list[dict[str, Any]] = []
    market_dates: dict[str, str | None] = {}
    market_counts: dict[str, int] = {}
    full_universe: list[dict[str, Any]] = []

    for market, payload in zip(requested_markets, market_payloads):
        market_date = _normalize_trade_date(payload.get("date"))
        market_dates[market] = market_date
        rows = [dict(row) for row in payload.get("data", [])]
        market_counts[market] = len(rows)
        as_of.append({
            "market": market,
            "date": market_date,
            "source": payload.get("source"),
            "fetchedAtTaipei": payload.get("fetchedAtTaipei"),
        })
        full_universe.extend(rows)

    distinct_dates = {value for value in market_dates.values() if value}
    if len(distinct_dates) != 1 or any(value is None for value in market_dates.values()):
        return {
            "ok": False,
            "serverVersion": "v7-free-official-same-day",
            "errorCode": "MARKET_DATE_MISMATCH",
            "message": "上市與上櫃官方行情日期不同或缺漏，本次拒絕產生排名。",
            "strategy": strategy,
            "markets": requested_markets,
            "asOf": as_of,
            "marketDates": market_dates,
            "results": [],
            "errors": [],
            "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
        }

    scoring_date = next(iter(distinct_dates))
    warnings: list[str] = []
    reference_date = await _get_reference_market_date(force_refresh=force_refresh)
    if reference_date and reference_date != scoring_date:
        return {
            "ok": False,
            "serverVersion": "v7-free-official-same-day",
            "errorCode": "OFFICIAL_DATA_NOT_LATEST",
            "message": (
                "官方全市場資料尚未更新到最新交易日，本次拒絕排名；"
                f"官方={scoring_date}，Fugle參考={reference_date}。"
            ),
            "strategy": strategy,
            "markets": requested_markets,
            "scoringDate": scoring_date,
            "referenceDate": reference_date,
            "asOf": as_of,
            "results": [],
            "errors": [],
            "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
        }
    if not reference_date:
        warnings.append("無法取得Fugle參考交易日；僅以官方市場日期一致性判斷。")

    if markets == "BOTH":
        minimum_count = V7_MIN_UNIVERSE_COUNT
    elif markets == "TSE":
        minimum_count = V7_MIN_TSE_COUNT
    else:
        minimum_count = V7_MIN_OTC_COUNT

    if len(full_universe) < minimum_count:
        return {
            "ok": False,
            "serverVersion": "v7-free-official-same-day",
            "errorCode": "MARKET_UNIVERSE_INCOMPLETE",
            "message": (
                f"官方普通股資料僅 {len(full_universe)} 檔，"
                f"低於安全門檻 {minimum_count} 檔，本次拒絕排名。"
            ),
            "strategy": strategy,
            "markets": requested_markets,
            "scoringDate": scoring_date,
            "referenceDate": reference_date,
            "asOf": as_of,
            "marketCounts": market_counts,
            "results": [],
            "errors": [],
            "fetchedAtUtc": datetime.now(timezone.utc).isoformat(),
        }

    eligible: list[dict[str, Any]] = []
    for row in full_universe:
        close = _safe_float(row.get("closePrice"))
        trade_value = _safe_float(row.get("tradeValue"))
        if not min_price <= close <= max_price:
            continue
        if trade_value < min_trade_value:
            continue
        normalized = dict(row)
        normalized["date"] = scoring_date
        eligible.append(normalized)

    candidates = _build_multilane_candidates(
        eligible,
        strategy,
        candidate_limit,
    )

    semaphore = asyncio.Semaphore(V7_HISTORY_CONCURRENCY)
    results = await asyncio.gather(
        *[
            _analyze_candidate(
                row,
                strategy,
                scoring_date,
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
        chip_semaphore = asyncio.Semaphore(V7_CHIP_CONCURRENCY)

        async def load_chip(item: dict[str, Any]) -> dict[str, Any]:
            async with chip_semaphore:
                return await _safe_chip_for_ranking(
                    item["symbol"],
                    45,
                    force_refresh=force_refresh,
                )

        chip_results = await asyncio.gather(*[load_chip(item) for item in selected])
        for item, chip in zip(selected, chip_results):
            item["chip"] = chip
            item["score"] = round(
                _safe_float(item.get("score"))
                + _safe_float(chip.get("scoreAdjustment")),
                2,
            )
            item["mainUptrendScore"], item["stage"] = _main_uptrend_stage(
                _safe_float(item.get("score"))
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
        "ok": True,
        "serverVersion": "v7-free-official-same-day",
        "strategy": strategy,
        "markets": requested_markets,
        "scoringDate": scoring_date,
        "referenceDate": reference_date,
        "allUniverseUsingSameDate": True,
        "asOf": as_of,
        "marketDataMode": "official_same_day_full_market",
        "marketDataFreshness": (
            "全市場初篩使用證交所／櫃買中心同一交易日官方行情；"
            "並以Fugle參考股票日期確認是否為最新交易日。"
        ),
        "marketCounts": market_counts,
        "marketUniverseCount": len(full_universe),
        "eligibleUniverseCount": len(eligible),
        "snapshotUniverseCount": len(eligible),
        "technicalCandidateLimit": candidate_limit,
        "deepAnalyzedCount": len(analyzed),
        "chipAnalyzedCount": len(selected) if include_chip else 0,
        "candidateLimit": candidate_limit,
        "filters": {
            "minTradeValue": min_trade_value,
            "minPrice": min_price,
            "maxPrice": max_price,
        },
        "includeChip": include_chip,
        "forceRefresh": force_refresh,
        "warnings": warnings,
        "cacheNote": (
            "官方行情、歷史K線及籌碼使用快取；force_refresh=true會略過快取。"
        ),
        "results": selected,
        "errors": errors[:15],
        "method": (
            "同日官方全市場行情多通道初篩，候選股併入當日官方K棒後重算技術指標；"
            "技術前N名再補法人與融資籌碼。"
        ),
        "source": (
            "TWSE OpenAPI + TPEx OpenAPI；候選股歷史K線使用Fugle "
            "MarketData API v1.0；include_chip時另用FinMind API v4"
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
            "postCloseUpdateWindowOfficialAndHistorical": 300,
            "after1830OfficialMarketAndHistorical": "until next weekday 08:30 Asia/Taipei",
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


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
