"""V12.4 multi-factor enrichment for the formal bullish radar.

The module deliberately stores compact daily/monthly features instead of raw
ticks.  A missing provider is reported as missing data and is never converted
to a zero score.  Available factors are reweighted to 100 percent.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx

from .advanced_factors import (
    ADR_MAP,
    build_derivatives_context,
    build_global_market_context,
    fetch_official_context,
    fetch_tdcc_context,
    portfolio_risk_map,
    score_cashflow_rows,
    score_event_context,
    score_news_rows,
    score_sector_driver,
)
from .connection import stock_database


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_FUTURES_SNAPSHOT_URL = (
    "https://api.finmindtrade.com/api/v4/taiwan_futures_snapshot"
)
FUGLE_QUOTE_URL = "https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{symbol}"
FUGLE_CANDLES_URL = "https://api.fugle.tw/marketdata/v1.0/stock/intraday/candles/{symbol}"
TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
_REMOTE_CACHE: dict[tuple[str, str], tuple[Any, ...]] = {}
# Kept as an internal compatibility name; public output is one release only.
V12_3_ACCURACY_ENGINE = "V12.4"
V12_4_FACTOR_MODEL = "V12.4-COMPLETE-FACTORS-1"
DEFAULT_FUNDAMENTAL_REFRESH_INTERVAL_DAYS = 7

FACTOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monthly_revenue (
    symbol VARCHAR(16) NOT NULL,
    revenue_month DATE NOT NULL,
    revenue NUMERIC(22,2),
    monthly_change_percent NUMERIC(22,4),
    yearly_change_percent NUMERIC(22,4),
    yearly_acceleration_percent NUMERIC(22,4),
    source VARCHAR(80),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, revenue_month)
);
CREATE TABLE IF NOT EXISTS security_theme_tags (
    symbol VARCHAR(16) NOT NULL,
    theme VARCHAR(80) NOT NULL,
    source VARCHAR(40) NOT NULL DEFAULT 'manual',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, theme)
);
CREATE TABLE IF NOT EXISTS intraday_daily_features (
    symbol VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    last_price NUMERIC(14,4),
    day_change_percent NUMERIC(10,4),
    close_position NUMERIC(10,4),
    volume_ratio NUMERIC(10,4),
    bid_ask_imbalance NUMERIC(10,4),
    score NUMERIC(10,4),
    source VARCHAR(80),
    snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, trade_date)
);
CREATE TABLE IF NOT EXISTS daily_factor_snapshots (
    symbol VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    chip_score NUMERIC(10,4),
    fundamental_score NUMERIC(10,4),
    theme_score NUMERIC(10,4),
    sector_score NUMERIC(10,4),
    intraday_score NUMERIC(10,4),
    event_score NUMERIC(10,4),
    cross_market_score NUMERIC(10,4),
    derivatives_score NUMERIC(10,4),
    sector_driver_score NUMERIC(10,4),
    sentiment_score NUMERIC(10,4),
    portfolio_risk_score NUMERIC(10,4),
    data_confidence NUMERIC(10,4) NOT NULL DEFAULT 0,
    missing_factors JSONB NOT NULL DEFAULT '[]'::JSONB,
    features JSONB NOT NULL DEFAULT '{}'::JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_monthly_revenue_month
    ON monthly_revenue(revenue_month DESC, symbol);
CREATE INDEX IF NOT EXISTS idx_factor_snapshot_date
    ON daily_factor_snapshots(trade_date DESC, data_confidence DESC);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema=current_schema()
          AND table_name='monthly_revenue'
          AND column_name IN (
              'monthly_change_percent',
              'yearly_change_percent',
              'yearly_acceleration_percent'
          )
          AND COALESCE(numeric_precision, 0) < 22
    ) THEN
        ALTER TABLE monthly_revenue
            ALTER COLUMN monthly_change_percent TYPE NUMERIC(22,4),
            ALTER COLUMN yearly_change_percent TYPE NUMERIC(22,4),
            ALTER COLUMN yearly_acceleration_percent TYPE NUMERIC(22,4);
    END IF;
END $$;
ALTER TABLE daily_factor_snapshots
    ADD COLUMN IF NOT EXISTS event_score NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS cross_market_score NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS derivatives_score NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS sector_driver_score NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS sentiment_score NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS portfolio_risk_score NUMERIC(10,4);
"""


def _float(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "--", "---"):
        return default
    try:
        parsed = float(str(value).replace(",", "").replace("%", "").strip())
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(value, high))


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


async def ensure_factor_schema() -> None:
    async with stock_database.acquire() as connection:
        await connection.execute(FACTOR_SCHEMA_SQL)


def _normalise_refresh_datetime(value: datetime | None) -> datetime | None:
    """Return a database timestamp in the maintenance timezone."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=TAIPEI_TZ)
    return value.astimezone(TAIPEI_TZ)


def _build_fundamental_refresh_status(
    *,
    now: datetime,
    interval_days: int,
    twse_last_updated_at: datetime | None,
    tpex_last_updated_at: datetime | None,
    revenue_rows: int,
    official_theme_tags: int,
) -> dict[str, Any]:
    """Build the automatic weekly refresh decision without touching the DB."""
    interval_days = max(1, int(interval_days))
    current = _normalise_refresh_datetime(now) or datetime.now(TAIPEI_TZ)
    twse_last = _normalise_refresh_datetime(twse_last_updated_at)
    tpex_last = _normalise_refresh_datetime(tpex_last_updated_at)

    complete_last = min(twse_last, tpex_last) if twse_last and tpex_last else None
    next_due = (
        complete_last + timedelta(days=interval_days)
        if complete_last is not None
        else None
    )
    if int(revenue_rows or 0) <= 0:
        due, reason = True, "基本面資料尚未初始化"
    elif twse_last is None or tpex_last is None:
        due, reason = True, "上市或上櫃基本面資料缺漏"
    elif int(official_theme_tags or 0) <= 0:
        due, reason = True, "官方產業題材標籤尚未初始化"
    elif next_due is not None and current >= next_due:
        due, reason = True, f"已達{interval_days}天更新週期"
    else:
        due, reason = False, f"距上次完整更新未滿{interval_days}天"

    return {
        "ok": True,
        "schedule": "AUTO_INTERVAL",
        "intervalDays": interval_days,
        "due": due,
        "reason": reason,
        "checkedAt": current.isoformat(),
        "lastCompleteUpdateAt": (
            complete_last.isoformat() if complete_last is not None else None
        ),
        "nextDueAt": next_due.isoformat() if next_due is not None else None,
        "perMarketLastUpdatedAt": {
            "TWSE": twse_last.isoformat() if twse_last is not None else None,
            "TPEx": tpex_last.isoformat() if tpex_last is not None else None,
        },
        "revenueRows": int(revenue_rows or 0),
        "officialThemeTags": int(official_theme_tags or 0),
    }


async def get_fundamental_refresh_status(
    interval_days: int = DEFAULT_FUNDAMENTAL_REFRESH_INTERVAL_DAYS,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check whether official fundamentals and automatic tags are due."""
    await ensure_factor_schema()
    async with stock_database.acquire() as connection:
        row = await connection.fetchrow("""
            SELECT
                MAX(updated_at) FILTER(
                    WHERE source LIKE 'TWSE %'
                ) AS twse_last_updated_at,
                MAX(updated_at) FILTER(
                    WHERE source LIKE 'TPEx %'
                ) AS tpex_last_updated_at,
                COUNT(*) AS revenue_rows,
                (
                    SELECT COUNT(*)
                    FROM security_theme_tags
                    WHERE source='official_industry'
                ) AS official_theme_tags
            FROM monthly_revenue
        """)
    values = dict(row or {})
    return _build_fundamental_refresh_status(
        now=now or datetime.now(TAIPEI_TZ),
        interval_days=interval_days,
        twse_last_updated_at=values.get("twse_last_updated_at"),
        tpex_last_updated_at=values.get("tpex_last_updated_at"),
        revenue_rows=int(values.get("revenue_rows") or 0),
        official_theme_tags=int(values.get("official_theme_tags") or 0),
    )


async def _json_get(url: str, **kwargs: Any) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "TaiwanStockMCP/12.3"}
    headers.update(kwargs.pop("headers", {}) or {})
    async with httpx.AsyncClient(timeout=kwargs.pop("timeout", 45), follow_redirects=True) as client:
        response = await client.get(url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()


def _roc_month_to_date(value: Any) -> date | None:
    text = str(value or "").strip().replace("/", "").replace("-", "")
    if not text.isdigit() or len(text) not in {5, 6}:
        return None
    year_text, month_text = text[:-2], text[-2:]
    year = int(year_text)
    if year < 1911:
        year += 1911
    month = int(month_text)
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def _parse_official_revenue_row(
    row: Mapping[str, Any],
    market: str,
) -> dict[str, Any] | None:
    """Parse the exact TWSE/TPEx monthly-revenue OpenAPI column names."""
    symbol = str(
        _pick(row, "公司代號", "公司代碼", "SecuritiesCompanyCode", "Code")
        or ""
    ).strip()
    month = _roc_month_to_date(
        _pick(row, "資料年月", "出表日期", "YearMonth", "年月")
    )
    if not symbol or month is None:
        return None
    return {
        "symbol": symbol,
        "month": month,
        "revenue": _float(
            _pick(row, "營業收入-當月營收", "當月營收", "MonthlyRevenue")
        ),
        "mom": _float(
            _pick(
                row,
                "營業收入-上月比較增減(%)",
                "上月比較增減(%)",
                "上月比較增減％",
                "MoM",
            )
        ),
        "yoy": _float(
            _pick(
                row,
                "營業收入-去年同月增減(%)",
                "去年同月增減(%)",
                "去年同月增減％",
                "YoY",
            )
        ),
        "industry": str(_pick(row, "產業別", "Industry") or "").strip(),
        "source": f"{market} monthly revenue",
    }


async def refresh_monthly_revenue() -> dict[str, Any]:
    """Refresh official TWSE/TPEx monthly revenue and YoY acceleration."""
    await ensure_factor_schema()
    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    for market, url in (("TWSE", TWSE_REVENUE_URL), ("TPEx", TPEX_REVENUE_URL)):
        try:
            body = await _json_get(url)
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")
            continue
        for row in body if isinstance(body, list) else []:
            if not isinstance(row, Mapping):
                continue
            value = _parse_official_revenue_row(row, market)
            if value is not None:
                parsed.append(value)
    if not parsed:
        return {"ok": False, "updated": 0, "errors": errors or ["官方月營收沒有可解析資料"]}
    async with stock_database.acquire() as connection:
        async with connection.transaction():
            await connection.executemany("""
                INSERT INTO monthly_revenue(
                    symbol,revenue_month,revenue,monthly_change_percent,
                    yearly_change_percent,source,updated_at
                ) VALUES($1,$2,$3,$4,$5,$6,NOW())
                ON CONFLICT(symbol,revenue_month) DO UPDATE SET
                    revenue=EXCLUDED.revenue,
                    monthly_change_percent=EXCLUDED.monthly_change_percent,
                    yearly_change_percent=EXCLUDED.yearly_change_percent,
                    source=EXCLUDED.source,updated_at=NOW()
            """, [(x["symbol"], x["month"], x["revenue"], x["mom"], x["yoy"], x["source"]) for x in parsed])
            symbols = sorted({x["symbol"] for x in parsed})
            await connection.execute(
                "DELETE FROM security_theme_tags "
                "WHERE source='official_industry' AND symbol=ANY($1::varchar[])",
                symbols,
            )
            official_tags = sorted({
                (x["symbol"], x["industry"], "official_industry")
                for x in parsed if x["industry"]
            })
            if official_tags:
                await connection.executemany("""
                    INSERT INTO security_theme_tags(symbol,theme,source,updated_at)
                    VALUES($1,$2,$3,NOW())
                    ON CONFLICT(symbol,theme) DO UPDATE SET
                        source=CASE
                            WHEN security_theme_tags.source='manual'
                            THEN security_theme_tags.source
                            ELSE EXCLUDED.source
                        END,
                        updated_at=NOW()
                """, official_tags)
            await connection.execute("""
                WITH ranked AS (
                    SELECT symbol,revenue_month,yearly_change_percent,
                           LAG(yearly_change_percent) OVER(
                               PARTITION BY symbol ORDER BY revenue_month
                           ) AS prior_yoy
                    FROM monthly_revenue
                )
                UPDATE monthly_revenue m
                SET yearly_acceleration_percent=r.yearly_change_percent-r.prior_yoy
                FROM ranked r
                WHERE m.symbol=r.symbol AND m.revenue_month=r.revenue_month
            """)
    return {
        "ok": True,
        "updated": len(parsed),
        "autoThemeTags": sum(1 for x in parsed if x["industry"]),
        "errors": errors,
    }


async def refresh_monthly_revenue_if_due(
    interval_days: int = DEFAULT_FUNDAMENTAL_REFRESH_INTERVAL_DAYS,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh fundamentals only when the automatic interval has elapsed.

    The regular close job calls this helper every day.  The lightweight status
    query is always allowed, while the remote download and roughly two thousand
    database upserts run only once per interval.  Missing TWSE/TPEx data or
    missing official theme tags bypass the interval so an incomplete database
    repairs itself on the next close job.
    """
    status = await get_fundamental_refresh_status(
        interval_days=interval_days,
        now=now,
    )
    if not force and not status["due"]:
        return {
            **status,
            "skipped": True,
            "updated": 0,
            "autoThemeTags": 0,
        }

    before = dict(status)
    refreshed = await refresh_monthly_revenue()
    output: dict[str, Any] = {
        **refreshed,
        "schedule": "AUTO_INTERVAL",
        "intervalDays": max(1, int(interval_days)),
        "skipped": False,
        "forced": bool(force),
        "dueReason": "手動強制更新" if force else before["reason"],
        "lastCompleteUpdateAtBefore": before["lastCompleteUpdateAt"],
    }
    if refreshed.get("ok"):
        after = await get_fundamental_refresh_status(
            interval_days=interval_days,
            now=now,
        )
        output.update({
            "lastCompleteUpdateAt": after["lastCompleteUpdateAt"],
            "nextDueAt": after["nextDueAt"],
            "retryNextClose": bool(after["due"]),
        })
    return output


async def update_theme_tags(symbol: str, themes: Sequence[str], source: str = "manual") -> dict[str, Any]:
    symbol = str(symbol).strip()
    clean = sorted({str(theme).strip() for theme in themes if str(theme).strip()})
    if not symbol:
        raise ValueError("symbol 不可空白")
    await ensure_factor_schema()
    async with stock_database.acquire() as connection:
        async with connection.transaction():
            # A manual topic update must not erase the official industry tag
            # created by ``refresh_monthly_revenue``.
            await connection.execute(
                "DELETE FROM security_theme_tags WHERE symbol=$1 AND source=$2",
                symbol,
                source,
            )
            if clean:
                await connection.executemany(
                    "INSERT INTO security_theme_tags(symbol,theme,source) VALUES($1,$2,$3)",
                    [(symbol, theme, source) for theme in clean],
                )
    return {"ok": True, "symbol": symbol, "themes": clean}


async def _finmind_rows(dataset: str, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    token = (os.getenv("FINMIND_TOKEN") or "").strip()
    if not token:
        return []
    if dataset == "taiwan_futures_snapshot":
        url = FINMIND_FUTURES_SNAPSHOT_URL
        params = {"data_id": symbol}
    else:
        url = FINMIND_URL
        params = {
            "dataset": dataset,
            "data_id": symbol,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    body = await _json_get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=45,
    )
    if not isinstance(body, Mapping) or int(body.get("status", 200)) != 200:
        return []
    return [dict(row) for row in (body.get("data") or []) if isinstance(row, Mapping)]


async def _chip_factor(symbol: str, trade_date: date) -> tuple[float | None, dict[str, Any]]:
    start = trade_date - timedelta(days=14)
    try:
        institutional, margin, foreign, lending = await asyncio.gather(
            _finmind_rows("TaiwanStockInstitutionalInvestorsBuySell", symbol, start, trade_date),
            _finmind_rows("TaiwanStockMarginPurchaseShortSale", symbol, start, trade_date),
            _finmind_rows("TaiwanStockShareholding", symbol, start, trade_date),
            _finmind_rows("TaiwanStockSecuritiesLending", symbol, start, trade_date),
        )
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    if not institutional and not margin and not foreign and not lending:
        return None, {"reason": "FINMIND_TOKEN缺少或籌碼資料無回傳"}

    components: list[tuple[float, float, str]] = []
    foreign_ratio = foreign_delta = issued_shares = None
    if foreign:
        first_ratio = _float(_pick(foreign[0], "ForeignInvestmentSharesRatio"))
        foreign_ratio = _float(_pick(foreign[-1], "ForeignInvestmentSharesRatio"))
        issued_shares = _float(_pick(foreign[-1], "NumberOfSharesIssued"))
        if first_ratio is not None and foreign_ratio is not None:
            foreign_delta = foreign_ratio - first_ratio

    institutional_net = 0.0
    for row in institutional[-40:]:
        institutional_net += (_float(_pick(row, "buy", "Buy"), 0.0) or 0.0) - (_float(_pick(row, "sell", "Sell"), 0.0) or 0.0)
    if institutional:
        institutional_to_issued_pct = (
            institutional_net / issued_shares * 100
            if issued_shares and issued_shares > 0 else None
        )
        institutional_pressure = (
            institutional_to_issued_pct / 0.5
            if institutional_to_issued_pct is not None
            else institutional_net / 2_000_000.0
        )
        components.append((
            _clamp(50.0 + math.tanh(institutional_pressure) * 30.0),
            0.42,
            "institutional",
        ))
    else:
        institutional_to_issued_pct = None

    margin_delta = margin_change_pct = 0.0
    if len(margin) >= 2:
        first = _float(_pick(margin[0], "MarginPurchaseTodayBalance", "MarginPurchaseBalance"), 0.0) or 0.0
        last = _float(_pick(margin[-1], "MarginPurchaseTodayBalance", "MarginPurchaseBalance"), 0.0) or 0.0
        margin_delta = last - first
        margin_change_pct = margin_delta / first * 100 if first > 0 else 0.0
        components.append((
            _clamp(50.0 - math.tanh(margin_change_pct / 12.0) * 18.0),
            0.18,
            "margin",
        ))

    if foreign:
        components.append((
            _clamp(50.0 + max(-5.0, min(5.0, foreign_delta or 0.0)) * 6.0),
            0.28,
            "foreignOwnership",
        ))

    lending_by_date: dict[str, float] = {}
    lending_fee_rates: list[float] = []
    for row in lending:
        day = str(row.get("date") or "")
        lending_by_date[day] = lending_by_date.get(day, 0.0) + (
            _float(_pick(row, "volume", "lendingVolume"), 0.0) or 0.0
        )
        fee = _float(row.get("fee_rate"))
        if fee is not None:
            lending_fee_rates.append(fee)
    lending_daily = [
        value for _, value in sorted(lending_by_date.items())
    ]
    lending_volume = sum(lending_daily)
    lending_change_pct = None
    if len(lending_daily) >= 4:
        split = max(1, len(lending_daily) // 2)
        prior_average = mean(lending_daily[:split])
        recent_average = mean(lending_daily[-split:])
        if prior_average > 0:
            lending_change_pct = (
                recent_average / prior_average - 1
            ) * 100
    average_lending_fee = (
        mean(lending_fee_rates) if lending_fee_rates else None
    )
    if lending:
        acceleration_pressure = math.tanh(
            (lending_change_pct or 0.0) / 60.0
        ) * 10.0
        fee_pressure = (
            max(0.0, math.tanh((average_lending_fee - 1.0) / 2.0)) * 5.0
            if average_lending_fee is not None else 0.0
        )
        components.append((
            _clamp(50.0 - acceleration_pressure - fee_pressure),
            0.12,
            "securitiesLending",
        ))

    available_weight = sum(weight for _, weight, _ in components)
    if available_weight <= 0:
        return None, {"reason": "籌碼資料存在但沒有可計算欄位"}
    score = sum(value * weight for value, weight, _ in components) / available_weight
    return round(_clamp(score), 2), {
        "institutionalNetShares": round(institutional_net),
        "institutionalNetToIssuedPercent": (
            round(institutional_to_issued_pct, 4)
            if institutional_to_issued_pct is not None else None
        ),
        "marginBalanceChangeShares": round(margin_delta),
        "marginBalanceChangePercent": round(margin_change_pct, 4),
        "foreignHoldingPercent": foreign_ratio,
        "foreignHoldingChangePercentagePoints": (
            round(foreign_delta, 4) if foreign_delta is not None else None
        ),
        "securitiesLendingVolume": round(lending_volume),
        "securitiesLendingVolumeChangePercent": (
            round(lending_change_pct, 4)
            if lending_change_pct is not None else None
        ),
        "averageSecuritiesLendingFeeRate": (
            round(average_lending_fee, 4)
            if average_lending_fee is not None else None
        ),
        "componentScores": {
            name: round(value, 2) for value, _, name in components
        },
        "source": "FinMind institutional/margin/foreign/lending datasets",
    }


def _merge_ownership_chip_factor(
    base_score: float | None,
    base_features: Mapping[str, Any],
    tdcc: Mapping[str, Any] | None,
    insider: Mapping[str, Any] | None,
) -> tuple[float | None, dict[str, Any]]:
    components: list[tuple[float, float]] = []
    if base_score is not None:
        components.append((base_score, 0.70))

    tdcc_score = None
    if tdcc:
        small = _float(tdcc.get("under100LotsPercent"))
        small_change = _float(tdcc.get("under100LotsPercentChange"))
        large_change = _float(tdcc.get("over400LotsPercentChange"))
        tdcc_score = 50.0
        if small_change is not None:
            tdcc_score -= max(-4.0, min(4.0, small_change)) * 7.0
        elif small is not None:
            tdcc_score += max(-6.0, min(6.0, (50.0 - small) * 0.15))
        if large_change is not None:
            tdcc_score += max(-4.0, min(4.0, large_change)) * 6.0
        tdcc_score = _clamp(tdcc_score)
        components.append((tdcc_score, 0.20))

    insider_score = None
    if insider:
        pledge = _float(insider.get("pledgeRatioPercent"))
        planned_sale = _float(insider.get("plannedSaleToInsiderHoldingPercent"))
        insider_score = 50.0
        if pledge is not None:
            insider_score -= max(0.0, pledge - 20.0) * 0.45
        if planned_sale is not None:
            insider_score -= max(0.0, min(20.0, planned_sale)) * 1.2
        insider_score = _clamp(insider_score)
        components.append((insider_score, 0.10))

    if not components:
        return None, dict(base_features)
    available_weight = sum(weight for _, weight in components)
    score = sum(value * weight for value, weight in components) / available_weight
    return round(_clamp(score), 2), {
        **dict(base_features),
        "tdccDistribution": dict(tdcc or {}),
        "insiderHoldings": dict(insider or {}),
        "tdccScore": round(tdcc_score, 2) if tdcc_score is not None else None,
        "insiderScore": (
            round(insider_score, 2) if insider_score is not None else None
        ),
    }


async def _intraday_factor(
    symbol: str,
    expected_trade_date: date | None = None,
) -> tuple[float | None, dict[str, Any]]:
    key = (os.getenv("FUGLE_API_KEY") or "").strip()
    if not key:
        return None, {"reason": "FUGLE_API_KEY缺少"}
    try:
        quote, candles_body = await asyncio.gather(
            _json_get(
                FUGLE_QUOTE_URL.format(symbol=symbol),
                headers={"X-API-KEY": key},
                timeout=20,
            ),
            _json_get(
                FUGLE_CANDLES_URL.format(symbol=symbol),
                params={"timeframe": "5", "sort": "asc"},
                headers={"X-API-KEY": key},
                timeout=25,
            ),
        )
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(quote, Mapping):
        return None, {"reason": "Fugle即時報價格式不正確，盤中結構不計分"}
    quote_date = str(quote.get("date") or "").strip()
    if expected_trade_date is not None and quote_date:
        if quote_date != expected_trade_date.isoformat():
            return None, {
                "reason": "Fugle報價日期與雷達交易日不同，盤中結構不計分",
                "quoteDate": quote_date,
                "expectedTradeDate": expected_trade_date.isoformat(),
            }
    last_trade = quote.get("lastTrade") if isinstance(quote, Mapping) else None
    last_trade = last_trade if isinstance(last_trade, Mapping) else {}
    last = _float(_pick(quote, "lastPrice", "closePrice", "price"))
    if last is None:
        last = _float(_pick(last_trade, "price", "lastPrice"))
    open_price = _float(_pick(quote, "openPrice", "open"))
    high = _float(_pick(quote, "highPrice", "high"))
    low = _float(_pick(quote, "lowPrice", "low"))
    if any(value is None or value <= 0 for value in (last, open_price, high, low)):
        return None, {
            "reason": "Fugle未回傳完整的現價與開高低，盤中結構不計分",
            "lastPrice": last,
            "openPrice": open_price,
            "highPrice": high,
            "lowPrice": low,
        }
    change = _float(_pick(quote, "changePercent"))
    reference = _float(_pick(quote, "referencePrice", "previousClose"))
    if change is None:
        change = ((last / reference - 1) * 100) if reference and reference > 0 else 0.0
    position = 0.5 if high <= low else (last-low)/(high-low)
    bids = quote.get("bids") if isinstance(quote, Mapping) else None
    asks = quote.get("asks") if isinstance(quote, Mapping) else None
    total = quote.get("total") if isinstance(quote, Mapping) else None
    total = total if isinstance(total, Mapping) else {}
    bid_volume = _float(_pick(total, "tradeVolumeAtBid"))
    ask_volume = _float(_pick(total, "tradeVolumeAtAsk"))
    if bid_volume is None:
        bid_volume = sum(_float(_pick(x, "volume", "size"), 0.0) or 0.0 for x in bids or [] if isinstance(x, Mapping))
    if ask_volume is None:
        ask_volume = sum(_float(_pick(x, "volume", "size"), 0.0) or 0.0 for x in asks or [] if isinstance(x, Mapping))
    # Fugle defines tradeVolumeAtBid as inner-volume (seller initiated) and
    # tradeVolumeAtAsk as outer-volume (buyer initiated).  More outer volume
    # is bullish, so the sign must be ask minus bid.
    imbalance = 0.0 if bid_volume + ask_volume <= 0 else (ask_volume-bid_volume)/(bid_volume+ask_volume)
    open_strength = 0.0 if not last or not open_price else (last/open_price-1)*100
    score = 50 + (position-.5)*40 + max(-10, min(10, change))*1.5 + max(-5, min(5, open_strength))*2 + imbalance*10

    candle_date = str(candles_body.get("date") or "").strip() if isinstance(candles_body, Mapping) else ""
    candles = (
        [dict(row) for row in candles_body.get("data", []) if isinstance(row, Mapping)]
        if isinstance(candles_body, Mapping) else []
    )
    if expected_trade_date is not None and candle_date != expected_trade_date.isoformat():
        candles = []
    vwap = close_vs_vwap = volume_acceleration = opening_range_recovery = None
    if candles:
        cumulative_value = cumulative_volume = 0.0
        volumes: list[float] = []
        for row in candles:
            volume = _float(row.get("volume"), 0.0) or 0.0
            average = _float(row.get("average"))
            if average is None:
                high_value = _float(row.get("high"), 0.0) or 0.0
                low_value = _float(row.get("low"), 0.0) or 0.0
                close_value = _float(row.get("close"), 0.0) or 0.0
                average = (high_value + low_value + close_value) / 3 if close_value > 0 else None
            if average is not None and volume > 0:
                cumulative_value += average * volume
                cumulative_volume += volume
            volumes.append(volume)
        if cumulative_volume > 0:
            vwap = cumulative_value / cumulative_volume
            close_vs_vwap = (last / vwap - 1) * 100 if vwap > 0 else None
            if close_vs_vwap is not None:
                score += max(-8.0, min(8.0, close_vs_vwap * 2.2))
        if len(volumes) >= 6:
            recent = mean(volumes[-3:])
            prior = mean(volumes[-6:-3])
            volume_acceleration = recent / prior if prior > 0 else None
            if volume_acceleration is not None:
                score += max(-4.0, min(4.0, (volume_acceleration - 1) * 3.0))
        opening = candles[:3]
        opening_highs = [_float(row.get("high")) for row in opening]
        opening_lows = [_float(row.get("low")) for row in opening]
        usable_highs = [value for value in opening_highs if value is not None]
        usable_lows = [value for value in opening_lows if value is not None]
        if usable_highs and usable_lows:
            range_high, range_low = max(usable_highs), min(usable_lows)
            if range_high > range_low:
                opening_range_recovery = (last - range_low) / (range_high - range_low)
                score += max(-4.0, min(4.0, (opening_range_recovery - 0.5) * 6.0))
    score = _clamp(score)
    return round(score, 2), {
        "quoteDate": quote_date or None,
        "lastPrice": last,
        "openPrice": open_price,
        "highPrice": high,
        "lowPrice": low,
        "dayChangePercent": change,
        "closePosition": round(position, 4),
        "innerVolume": round(bid_volume, 2),
        "outerVolume": round(ask_volume, 2),
        "bidAskImbalance": round(imbalance, 4),
        "vwap": round(vwap, 4) if vwap is not None else None,
        "closeVsVwapPercent": (
            round(close_vs_vwap, 4) if close_vs_vwap is not None else None
        ),
        "openingRangeRecovery": (
            round(opening_range_recovery, 4)
            if opening_range_recovery is not None else None
        ),
        "fiveMinuteVolumeAcceleration": (
            round(volume_acceleration, 4)
            if volume_acceleration is not None else None
        ),
        "fiveMinuteCandleCount": len(candles),
        "microstructureComplete": bool(candles),
        "source": "Fugle quote + 5-minute candles",
    }


async def _sentiment_factor(
    symbol: str,
    trade_date: date,
) -> tuple[float | None, dict[str, Any]]:
    if not (os.getenv("FINMIND_TOKEN") or "").strip():
        return None, {"reason": "FINMIND_TOKEN缺少，新聞情緒不計分"}
    try:
        rows = await _finmind_rows(
            "TaiwanStockNews",
            symbol,
            trade_date - timedelta(days=7),
            trade_date,
        )
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    return score_news_rows(rows, trade_date)


async def _cashflow_context(symbol: str, trade_date: date) -> dict[str, Any]:
    if not (os.getenv("FINMIND_TOKEN") or "").strip():
        return {"available": False, "reason": "FINMIND_TOKEN缺少"}
    try:
        rows = await _finmind_rows(
            "TaiwanStockCashFlowsStatement",
            symbol,
            trade_date - timedelta(days=500),
            trade_date,
        )
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    return score_cashflow_rows(rows)


async def _stored_factors(symbols: Sequence[str]) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    if not symbols:
        return {}, {}
    try:
        await ensure_factor_schema()
        async with stock_database.acquire() as connection:
            revenue_rows = await connection.fetch("""
            SELECT DISTINCT ON(symbol) symbol,revenue_month,monthly_change_percent,
                   yearly_change_percent,yearly_acceleration_percent,source
            FROM monthly_revenue WHERE symbol=ANY($1::varchar[])
            ORDER BY symbol,revenue_month DESC
        """, list(symbols))
            theme_rows = await connection.fetch(
                "SELECT symbol,theme FROM security_theme_tags WHERE symbol=ANY($1::varchar[])", list(symbols)
            )
    except RuntimeError:
        return {}, {}
    revenues = {str(row["symbol"]): dict(row) for row in revenue_rows}
    themes: dict[str, list[str]] = {}
    for row in theme_rows:
        themes.setdefault(str(row["symbol"]), []).append(str(row["theme"]))
    return revenues, themes


async def _theme_market_context() -> dict[str, dict[str, Any]]:
    """Calculate real daily price/volume breadth for every stored theme."""
    try:
        await ensure_factor_schema()
        async with stock_database.acquire() as connection:
            rows = await connection.fetch("""
                WITH latest AS (
                    SELECT MAX(trade_date) AS trade_date FROM daily_indicators
                )
                SELECT t.theme, COUNT(*) AS member_count,
                       AVG(CASE WHEN b.close >= i.ma20 THEN 100.0 ELSE 0.0 END)
                           AS above_ma20_percent,
                       AVG(CASE WHEN COALESCE(b.change_percent,0) > 0
                                THEN 100.0 ELSE 0.0 END)
                           AS advancing_percent,
                       AVG(CASE WHEN COALESCE(i.volume_ratio,0) >= 1
                                THEN 100.0 ELSE 0.0 END)
                           AS active_volume_percent,
                       AVG(COALESCE(i.technical_score,0)) AS average_technical_score
                FROM latest
                JOIN daily_indicators i ON i.trade_date=latest.trade_date
                JOIN daily_bars b ON b.symbol=i.symbol AND b.trade_date=i.trade_date
                JOIN security_theme_tags t ON t.symbol=i.symbol
                GROUP BY t.theme
            """)
    except RuntimeError:
        return {}
    context: dict[str, dict[str, Any]] = {}
    for row in rows:
        members = int(row["member_count"] or 0)
        if members < 3:
            continue
        above = _float(row["above_ma20_percent"], 0.0) or 0.0
        advancing = _float(row["advancing_percent"], 0.0) or 0.0
        active = _float(row["active_volume_percent"], 0.0) or 0.0
        technical = _float(row["average_technical_score"], 0.0) or 0.0
        score = _clamp(above*.45 + advancing*.25 + active*.15 + technical*.15)
        context[str(row["theme"])] = {
            "memberCount": members,
            "aboveMA20Percent": round(above, 2),
            "advancingPercent": round(advancing, 2),
            "activeVolumePercent": round(active, 2),
            "averageTechnicalScore": round(technical, 2),
            "heatScore": round(score, 2),
        }
    return context


def _fundamental_factor(
    row: Mapping[str, Any] | None,
    advanced: Mapping[str, Any] | None = None,
    cashflow: Mapping[str, Any] | None = None,
) -> tuple[float | None, dict[str, Any]]:
    yoy = _float((row or {}).get("yearly_change_percent"))
    mom = _float((row or {}).get("monthly_change_percent"))
    acceleration = _float((row or {}).get("yearly_acceleration_percent"))
    components: list[tuple[float, float]] = []
    if yoy is not None:
        components.append((_clamp(50 + max(-40, min(40, yoy))*1.2), .55))
    if mom is not None:
        components.append((_clamp(50 + max(-30, min(30, mom))*.8), .20))
    if acceleration is not None:
        components.append((_clamp(50 + max(-30, min(30, acceleration))), .25))
    revenue_score = None
    if components:
        total_weight = sum(weight for _, weight in components)
        revenue_score = sum(value*weight for value, weight in components) / total_weight

    advanced = dict(advanced or {})
    financial_industry = bool(advanced.get("isFinancialIndustry"))
    profitability_components: list[float] = []
    gross_margin = _float(advanced.get("grossMarginPercent"))
    operating_margin = _float(advanced.get("operatingMarginPercent"))
    net_margin = _float(advanced.get("netMarginPercent"))
    roe = _float(advanced.get("annualizedRoePercent"))
    eps = _float(advanced.get("eps"))
    if gross_margin is not None and not financial_industry:
        profitability_components.append(_clamp(45 + gross_margin * 0.65))
    if operating_margin is not None and not financial_industry:
        profitability_components.append(_clamp(48 + operating_margin * 1.1))
    if net_margin is not None and not financial_industry:
        profitability_components.append(_clamp(48 + net_margin * 1.0))
    if roe is not None:
        profitability_components.append(_clamp(45 + roe * 1.1))
    if eps is not None:
        profitability_components.append(_clamp(50 + math.tanh(eps / 3.0) * 18))
    profitability_score = (
        sum(profitability_components) / len(profitability_components)
        if profitability_components else None
    )

    balance_components: list[float] = []
    debt_ratio = _float(advanced.get("debtRatioPercent"))
    current_ratio = _float(advanced.get("currentRatio"))
    if debt_ratio is not None and not financial_industry:
        balance_components.append(_clamp(85 - debt_ratio * 0.65))
    if current_ratio is not None and not financial_industry:
        balance_components.append(_clamp(35 + min(3.0, current_ratio) * 20))
    balance_score = (
        sum(balance_components) / len(balance_components)
        if balance_components else None
    )

    valuation_components: list[float] = []
    pe = _float(advanced.get("peRatio"))
    pb = _float(advanced.get("pbRatio"))
    dividend_yield = _float(advanced.get("dividendYieldPercent"))
    if pe is not None:
        if pe <= 0:
            valuation_components.append(30.0)
        elif pe <= 25:
            valuation_components.append(_clamp(75 - abs(pe - 14) * 1.4))
        else:
            valuation_components.append(_clamp(55 - (pe - 25) * 0.5))
    if pb is not None:
        valuation_components.append(_clamp(72 - max(0, pb - 1) * 9))
    if dividend_yield is not None:
        valuation_components.append(_clamp(45 + min(8, dividend_yield) * 4))
    valuation_score = (
        sum(valuation_components) / len(valuation_components)
        if valuation_components else None
    )

    cashflow = dict(cashflow or {})
    cashflow_score = None
    if cashflow.get("available"):
        operating_cash = _float(cashflow.get("operatingCashFlow"))
        free_cash = _float(cashflow.get("freeCashFlow"))
        cashflow_components = []
        if operating_cash is not None:
            cashflow_components.append(68.0 if operating_cash > 0 else 28.0)
        if free_cash is not None:
            cashflow_components.append(65.0 if free_cash > 0 else 35.0)
        if cashflow_components:
            cashflow_score = sum(cashflow_components) / len(cashflow_components)

    group_scores: list[tuple[float, float, str]] = []
    if revenue_score is not None:
        group_scores.append((revenue_score, 0.35, "revenueGrowth"))
    if profitability_score is not None:
        group_scores.append((profitability_score, 0.30, "profitability"))
    if balance_score is not None:
        group_scores.append((balance_score, 0.18, "balanceSheet"))
    if cashflow_score is not None:
        group_scores.append((cashflow_score, 0.10, "cashFlow"))
    if valuation_score is not None:
        group_scores.append((valuation_score, 0.07, "valuation"))
    if not group_scores:
        return None, {"reason": "月營收、季度財報、現金流與估值皆無可用資料"}
    available_weight = sum(weight for _, weight, _ in group_scores)
    score = sum(value * weight for value, weight, _ in group_scores) / available_weight
    return round(_clamp(score), 2), {
        "revenueMonth": str((row or {}).get("revenue_month") or "") or None,
        "revenueYoYPercent": yoy,
        "revenueMoMPercent": mom,
        "yoyAccelerationPercent": acceleration,
        "accelerationAvailable": acceleration is not None,
        "officialQuarterlyFinancials": advanced,
        "cashFlowQuality": cashflow,
        "componentScores": {
            name: round(value, 2) for value, _, name in group_scores
        },
        "coverage": [name for _, _, name in group_scores],
        "industryAccountingRule": (
            "金融業不套用一般產業毛利率、負債比與流動比率門檻"
            if financial_industry else "一般產業財務比率"
        ),
        "pointInTimeGuard": "只使用公布／擷取日不晚於雷達交易日的資料",
    }


def _sector_factor(candidate: Mapping[str, Any], market_context: Mapping[str, Any]) -> tuple[float | None, dict[str, Any]]:
    industry = str(candidate.get("industry") or "").strip()
    sector = (market_context.get("industries") or {}).get(industry) if industry else None
    if not isinstance(sector, Mapping):
        return None, {"reason": "產業分類或產業樣本不足"}
    relative = _float(sector.get("relativeBreadthPercent"), 0.0) or 0.0
    median = _float(sector.get("medianFiveDayChangePercent"), 0.0) or 0.0
    score = _clamp(50 + relative*.8 + median*2.0)
    return round(score, 2), {"industry": industry, **dict(sector)}


async def enrich_candidates_v12_3(
    candidates: Sequence[Mapping[str, Any]],
    trade_date: date,
    market_context: Mapping[str, Any],
    config: Any,
) -> list[dict[str, Any]]:
    """Add every V12.4 point-in-time factor to formal radar rows."""
    items = [dict(item) for item in candidates]
    symbols = [str(item.get("symbol") or "").strip() for item in items]
    revenues, themes = await _stored_factors(symbols)
    theme_market = await _theme_market_context()

    if stock_database.config.enabled and symbols:
        official, tdcc_result, global_context, derivatives_context = await asyncio.gather(
            fetch_official_context(trade_date),
            fetch_tdcc_context(symbols, trade_date),
            build_global_market_context(trade_date, _finmind_rows),
            build_derivatives_context(trade_date, _finmind_rows),
        )
        tdcc, tdcc_status = tdcc_result
        portfolio = await portfolio_risk_map(items, themes)
    else:
        official = {
            "financials": {}, "insider": {}, "events": {}, "attention": {},
            "disposition": {}, "sourceStatus": {},
            "asOfTradeDate": trade_date.isoformat(),
        }
        tdcc, tdcc_status = {}, {"ok": False, "rows": 0, "error": "local mode"}
        global_context = {
            "score": None, "assets": {}, "macro": {},
            "sourceStatus": {"ok": False, "errors": ["local mode"]},
            "taiwanImpactTiming": {},
        }
        derivatives_context = {"score": None, "features": {}, "available": False}
        portfolio = {
            symbol: {
                "score": 100.0, "adjustment": 0.0,
                "reason": "local mode does not query portfolio correlations",
            }
            for symbol in symbols
        }

    if isinstance(market_context, dict):
        market_context["crossMarket"] = global_context
        market_context["derivatives"] = derivatives_context
        market_context["advancedSourceStatus"] = {
            "official": official.get("sourceStatus") or {},
            "tdcc": tdcc_status,
            "factorModel": V12_4_FACTOR_MODEL,
        }

    semaphore = asyncio.Semaphore(max(1, int(getattr(config, "factor_api_concurrency", 3))))

    async def remote(symbol: str) -> tuple[Any, Any, Any, Any]:
        cache_key = (symbol, trade_date.isoformat())
        if cache_key in _REMOTE_CACHE:
            cached = _REMOTE_CACHE[cache_key]
            if len(cached) == 4:
                return cached  # type: ignore[return-value]
        async with semaphore:
            value = await asyncio.gather(
                _chip_factor(symbol, trade_date),
                _intraday_factor(symbol, trade_date),
                _sentiment_factor(symbol, trade_date),
                _cashflow_context(symbol, trade_date),
            )
            packed = tuple(value)
            _REMOTE_CACHE[cache_key] = packed
            return packed  # type: ignore[return-value]

    remote_rows = await asyncio.gather(*(remote(symbol) for symbol in symbols))
    weights = {
        "technical": float(getattr(config, "factor_weight_technical", 25.0)),
        "chip": float(getattr(config, "factor_weight_chip", 15.0)),
        "fundamental": float(getattr(config, "factor_weight_fundamental", 12.0)),
        "event": float(getattr(config, "factor_weight_event", 10.0)),
        "theme": float(getattr(config, "factor_weight_theme", 8.0)),
        "intraday": float(getattr(config, "factor_weight_intraday", 10.0)),
        "market": float(getattr(config, "factor_weight_market", 5.0)),
        "cross_market": float(getattr(config, "factor_weight_cross_market", 5.0)),
        "derivatives": float(getattr(config, "factor_weight_derivatives", 3.0)),
        "sector_driver": float(getattr(config, "factor_weight_sector_driver", 3.0)),
        "sentiment": float(getattr(config, "factor_weight_sentiment", 2.0)),
        "history": float(getattr(config, "factor_weight_history", 2.0)),
    }
    output: list[dict[str, Any]] = []
    snapshot_rows: list[tuple[Any, ...]] = []
    official_event_sources_ok = any(
        bool((official.get("sourceStatus") or {}).get(name, {}).get("ok"))
        for name in (
            "twse_events", "tpex_events", "twse_attention",
            "tpex_attention", "twse_disposition", "tpex_disposition",
        )
    )
    for item, remote_row in zip(items, remote_rows):
        (base_chip_score, base_chip_features), (intraday_score, intraday_features), (sentiment_score, sentiment_features), cashflow_features = remote_row
        symbol = str(item.get("symbol") or "").strip()
        chip_score, chip_features = _merge_ownership_chip_factor(
            base_chip_score,
            base_chip_features,
            tdcc.get(symbol),
            (official.get("insider") or {}).get(symbol),
        )
        fundamental_score, fundamental_features = _fundamental_factor(
            revenues.get(symbol),
            (official.get("financials") or {}).get(symbol),
            cashflow_features,
        )
        event_score, event_features = score_event_context(
            symbol, official, trade_date
        )
        if not official_event_sources_ok:
            event_score = None
            event_features = {
                **event_features,
                "checked": False,
                "reason": "TWSE/TPEx事件來源皆不可用",
            }
        sector_score, sector_features = _sector_factor(item, market_context)
        sector_driver_score, sector_driver_features = score_sector_driver(
            symbol,
            str(item.get("industry") or ""),
            global_context,
        )
        tags = themes.get(symbol, [])
        available_themes = [theme_market[tag] for tag in tags if tag in theme_market]
        if available_themes:
            strongest = sorted(
                available_themes,
                key=lambda value: float(value.get("heatScore") or 0),
                reverse=True,
            )[:2]
            theme_score = round(
                sum(float(value["heatScore"]) for value in strongest) / len(strongest),
                2,
            )
            theme_features = {
                "themes": tags,
                "marketHeat": {
                    tag: theme_market[tag] for tag in tags if tag in theme_market
                },
            }
        elif tags:
            theme_score = None
            theme_features = {
                "themes": tags,
                "reason": "題材已有標籤，但全市場有效樣本少於3檔",
            }
        else:
            theme_score = None
            theme_features = {"reason": "尚未建立題材標籤"}
        regime = str(market_context.get("regime") or "NEUTRAL")
        market_score = {"STRONG": 70.0, "NEUTRAL": 50.0, "WEAK": 30.0}.get(regime, 50.0)
        cross_market_score = _float(global_context.get("score"))
        adr_ticker = ADR_MAP.get(symbol)
        adr_features = (global_context.get("assets") or {}).get(adr_ticker) if adr_ticker else None
        if cross_market_score is not None and isinstance(adr_features, Mapping):
            adr_change = _float(adr_features.get("change1dPercent"))
            if adr_change is not None:
                adr_score = _clamp(50 + adr_change * 4)
                cross_market_score = cross_market_score * 0.72 + adr_score * 0.28
        derivatives_score = _float(derivatives_context.get("score"))
        history_adjustment = _float(item.get("historical_execution_adjustment"), 0.0) or 0.0
        history_score = _clamp(50 + history_adjustment*5)
        portfolio_features = dict(portfolio.get(symbol) or {})
        portfolio_score = _float(portfolio_features.get("score"), 100.0) or 100.0
        portfolio_adjustment = min(
            0.0, _float(portfolio_features.get("adjustment"), 0.0) or 0.0
        )
        values: dict[str, float | None] = {
            "technical": _float(item.get("bullish_score"), 50.0), "chip": chip_score,
            "fundamental": fundamental_score, "event": event_score,
            "theme": theme_score, "intraday": intraday_score,
            "market": market_score, "cross_market": cross_market_score,
            "derivatives": derivatives_score,
            "sector_driver": sector_driver_score,
            "sentiment": sentiment_score, "history": history_score,
        }
        available_weight = sum(weights[name] for name, value in values.items() if value is not None)
        raw_final_score = sum((values[name] or 0)*weights[name] for name in weights if values[name] is not None) / max(available_weight, 1)
        final_score = _clamp(raw_final_score + portfolio_adjustment)
        missing = [name for name, value in values.items() if value is None]
        confidence = available_weight / max(sum(weights.values()), 1) * 100
        minimum_confidence = float(getattr(config, "factor_minimum_confidence", 60.0))
        # Local/unit-test mode may intentionally run without PostgreSQL or any
        # provider credentials. Production has STOCK_DB_ENABLED=true and must
        # enforce the confidence gate.
        enforce_confidence = bool(stock_database.config.enabled)
        hard_event_risk = bool(event_features.get("hardRisk"))
        execution_blocked = bool(event_features.get("executionBlocked"))
        qualified = bool(item.get("forwardQualified", True)) and (
            confidence >= minimum_confidence or not enforce_confidence
        ) and final_score >= float(getattr(config, "forward_min_bullish_score", 65.0)) and not hard_event_risk and not execution_blocked
        warnings = list(item.get("warnings") or [])
        if confidence < minimum_confidence and enforce_confidence:
            warnings.append(f"多因子資料完整度{confidence:.0f}%低於{minimum_confidence:.0f}%，只列觀察")
        if hard_event_risk:
            warnings.append("官方重大訊息出現硬風險，只列觀察")
        if execution_blocked:
            warnings.append("目前為處置證券，成交與滑價風險過高，只列觀察")
        if event_features.get("attention"):
            warnings.append("近期為注意證券，事件分數已降權")
        qualification = dict(item.get("forwardQualification") or {})
        failed_rules = list(qualification.get("failedRules") or [])
        if final_score < float(getattr(config, "forward_min_bullish_score", 65.0)):
            failed_rules.append(
                f"完整因子最終分數{final_score:.1f}低於正式門檻"
            )
            warnings.append("完整因子最終分數不足，只列觀察")
        if hard_event_risk:
            failed_rules.append("官方重大訊息硬風險")
        if execution_blocked:
            failed_rules.append("處置證券禁止列入正式買進")
        qualification.update({
            "qualified": qualified,
            "engine": V12_3_ACCURACY_ENGINE,
            "failedRules": failed_rules,
        })
        cross_market_features = {
            "score": round(cross_market_score, 2) if cross_market_score is not None else None,
            "coreAssets": {
                ticker: value
                for ticker, value in (global_context.get("assets") or {}).items()
                if ticker in {"^GSPC", "^IXIC", "^SOX", "EWT", adr_ticker}
            },
            "macro": global_context.get("macro") or {},
            "taiwanImpactTiming": global_context.get("taiwanImpactTiming") or {},
            "sourceStatus": global_context.get("sourceStatus") or {},
        }
        features = {
            "chip": chip_features,
            "fundamental": fundamental_features,
            "event": event_features,
            "theme": theme_features,
            "sector": sector_features,
            "intraday": intraday_features,
            "crossMarket": cross_market_features,
            "derivatives": derivatives_context,
            "sectorDriver": sector_driver_features,
            "sentiment": sentiment_features,
            "portfolioRisk": portfolio_features,
        }
        item.update({
            "accuracyEngine": V12_3_ACCURACY_ENGINE,
            "factorModelRevision": V12_4_FACTOR_MODEL,
            "technical_score": round(values["technical"] or 0, 2),
            "chip_score": chip_score, "chipScore": chip_score,
            "fundamental_score": fundamental_score, "fundamentalScore": fundamental_score,
            "event_score": event_score, "eventScore": event_score,
            "theme_score": theme_score, "themeScore": theme_score,
            "sector_score": sector_score, "sectorScore": sector_score,
            "intraday_score": intraday_score, "intradayScore": intraday_score,
            "cross_market_score": (
                round(cross_market_score, 2)
                if cross_market_score is not None else None
            ),
            "crossMarketScore": (
                round(cross_market_score, 2)
                if cross_market_score is not None else None
            ),
            "derivativesScore": derivatives_score,
            "sectorDriverScore": sector_driver_score,
            "sentimentScore": sentiment_score,
            "portfolioRiskScore": round(portfolio_score, 2),
            "portfolioAdjustment": round(portfolio_adjustment, 2),
            "factorScores": {name: (round(value, 2) if value is not None else None) for name, value in values.items()},
            "factorWeights": weights, "factorFeatures": features,
            "dataConfidence": round(confidence, 2), "missingFactors": missing,
            "bullish_score": round(final_score, 2), "total_score": round(final_score, 2),
            "finalScore": round(final_score, 2), "ranking_score": round(final_score, 2),
            "forwardQualified": qualified,
            "forwardQualification": qualification,
            "warnings": warnings,
        })
        output.append(item)
        snapshot_rows.append((
            symbol, trade_date, chip_score, fundamental_score, theme_score,
            sector_score, intraday_score, event_score, cross_market_score,
            derivatives_score, sector_driver_score, sentiment_score,
            portfolio_score, confidence,
            json.dumps(missing, ensure_ascii=False),
            json.dumps(features, ensure_ascii=False, default=str),
        ))
    if snapshot_rows:
        try:
            async with stock_database.acquire() as connection:
                await connection.executemany("""
                INSERT INTO daily_factor_snapshots(
                    symbol,trade_date,chip_score,fundamental_score,theme_score,
                    sector_score,intraday_score,event_score,cross_market_score,
                    derivatives_score,sector_driver_score,sentiment_score,
                    portfolio_risk_score,data_confidence,missing_factors,features,updated_at
                ) VALUES(
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                    $15::jsonb,$16::jsonb,NOW()
                )
                ON CONFLICT(symbol,trade_date) DO UPDATE SET
                    chip_score=EXCLUDED.chip_score,fundamental_score=EXCLUDED.fundamental_score,
                    theme_score=EXCLUDED.theme_score,sector_score=EXCLUDED.sector_score,
                    intraday_score=EXCLUDED.intraday_score,event_score=EXCLUDED.event_score,
                    cross_market_score=EXCLUDED.cross_market_score,
                    derivatives_score=EXCLUDED.derivatives_score,
                    sector_driver_score=EXCLUDED.sector_driver_score,
                    sentiment_score=EXCLUDED.sentiment_score,
                    portfolio_risk_score=EXCLUDED.portfolio_risk_score,
                    data_confidence=EXCLUDED.data_confidence,
                    missing_factors=EXCLUDED.missing_factors,features=EXCLUDED.features,updated_at=NOW()
                """, snapshot_rows)
        except RuntimeError:
            pass
    return output
