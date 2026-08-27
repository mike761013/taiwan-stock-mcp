"""Point-in-time advanced factors used by the V12.4 formal radar.

The public TWSE/TPEx feeds are fetched once per radar date.  Per-symbol paid
or rate-limited feeds are supplied by :mod:`stock_db.factors`, so this module
can remain deterministic in unit tests.  Every returned feature includes its
source date; rows newer than the requested Taiwan trading date are rejected.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import httpx

from .connection import stock_database


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TWSE = "https://openapi.twse.com.tw/v1"
TPEX = "https://www.tpex.org.tw/openapi/v1"
TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"

OFFICIAL_ENDPOINTS: dict[str, str] = {
    "twse_events": f"{TWSE}/opendata/t187ap04_L",
    "tpex_events": f"{TPEX}/mopsfin_t187ap04_O",
    "twse_valuation": f"{TWSE}/exchangeReport/BWIBBU_ALL",
    "tpex_valuation": f"{TPEX}/tpex_mainboard_peratio_analysis",
    "twse_insiders": f"{TWSE}/opendata/t187ap11_L",
    "tpex_insiders": f"{TPEX}/mopsfin_t187ap11_O",
    "twse_transfers": f"{TWSE}/opendata/t187ap12_L",
    "tpex_transfers": f"{TPEX}/mopsfin_t187ap12_O",
    "twse_attention": f"{TWSE}/announcement/notice",
    "tpex_attention": f"{TPEX}/tpex_trading_warning_information",
    "twse_disposition": f"{TWSE}/announcement/punish",
    "tpex_disposition": f"{TPEX}/tpex_disposal_information",
}
_STATEMENT_LAYOUTS = ("ci", "basi", "bd", "fh", "ins", "mim")
for _layout in _STATEMENT_LAYOUTS:
    OFFICIAL_ENDPOINTS.update({
        f"twse_income_{_layout}": (
            f"{TWSE}/opendata/t187ap06_L_{_layout}"
        ),
        f"tpex_income_{_layout}": (
            f"{TPEX}/mopsfin_t187ap06_O_{_layout}"
        ),
        f"twse_balance_{_layout}": (
            f"{TWSE}/opendata/t187ap07_L_{_layout}"
        ),
        f"tpex_balance_{_layout}": (
            f"{TPEX}/mopsfin_t187ap07_O_{_layout}"
        ),
    })

ADVANCED_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tdcc_distribution_snapshots (
    symbol VARCHAR(16) NOT NULL,
    snapshot_date DATE NOT NULL,
    under_100_lots_percent NUMERIC(10,4),
    over_400_lots_percent NUMERIC(10,4),
    holder_count BIGINT,
    source VARCHAR(80) NOT NULL DEFAULT 'TDCC OpenData 1-5',
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_tdcc_distribution_symbol_date
    ON tdcc_distribution_snapshots(symbol, snapshot_date DESC);
"""

_OFFICIAL_CACHE: dict[str, dict[str, Any]] = {}
_TDCC_CACHE: dict[str, Any] = {"expires": None, "data": None, "status": None}
_TDCC_LOCK = asyncio.Lock()


def _float(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "-", "--", "---", "N/A"):
        return default
    try:
        parsed = float(str(value).replace(",", "").replace("%", "").strip())
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _clean_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key or "").strip(): value for key, value in row.items()}


def _pick(row: Mapping[str, Any], *names: str) -> Any:
    clean = _clean_row(row)
    for name in names:
        value = clean.get(name.strip())
        if value not in (None, ""):
            return value
    return None


def _symbol(row: Mapping[str, Any]) -> str:
    value = str(_pick(row, "公司代號", "SecuritiesCompanyCode", "Code") or "").strip()
    return value if len(value) == 4 and value.isdigit() else ""


def parse_roc_date(value: Any) -> date | None:
    """Parse YYYYMMDD, YYYMMDD and common slash/dash formats."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    western = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})", text)
    roc = re.match(r"^(\d{3})[-/]?(\d{2})[-/]?(\d{2})", text)
    try:
        if western:
            return date(*(int(value) for value in western.groups()))
        if roc:
            year, month, day = (int(value) for value in roc.groups())
            return date(year + 1911, month, day)
    except ValueError:
        return None
    return None


def parse_roc_period(value: Any) -> tuple[date | None, date | None]:
    """Parse an official disposition period such as 115/08/24～115/08/28."""
    text = str(value or "")
    parts = re.split(r"[～~至—–]+", text)
    if len(parts) < 2:
        parts = re.findall(
            r"(?:\d{3,4}/\d{2}/\d{2}|\d{7,8})",
            text,
        )
    if len(parts) < 2:
        return None, None
    return parse_roc_date(parts[0]), parse_roc_date(parts[1])


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            day = parse_roc_date(text)
            return (
                datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ)
                if day else None
            )
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def _published_at(day_value: Any, time_value: Any = None) -> datetime | None:
    day = parse_roc_date(day_value)
    if day is None:
        return None
    digits = re.sub(r"\D", "", str(time_value or "")).zfill(6)[-6:]
    try:
        return datetime(
            day.year, day.month, day.day,
            int(digits[:2]), int(digits[2:4]), int(digits[4:6]),
            tzinfo=TAIPEI_TZ,
        )
    except ValueError:
        return datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ)


async def _json_get(url: str, timeout: float = 45.0) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "TaiwanStockMCP/12.4",
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
        follow_redirects=True,
    ) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


async def ensure_advanced_schema() -> None:
    async with stock_database.acquire() as connection:
        await connection.execute(ADVANCED_SCHEMA_SQL)


_POSITIVE_EVENT_RULES: tuple[tuple[str, float, str], ...] = (
    ("上修財測", 18, "財測上修"),
    ("獲利創新高", 16, "獲利創高"),
    ("營收創新高", 14, "營收創高"),
    ("取得重大訂單", 14, "重大訂單"),
    ("取得訂單", 10, "新增訂單"),
    ("庫藏股", 10, "庫藏股"),
    ("正式量產", 10, "量產進度"),
    ("策略聯盟", 7, "策略合作"),
    ("處分利益", 6, "處分利益"),
    ("現金股利", 4, "股利"),
)
_NEGATIVE_EVENT_RULES: tuple[tuple[str, float, str], ...] = (
    ("下修財測", -20, "財測下修"),
    ("重大損失", -20, "重大損失"),
    ("現金增資", -14, "現金增資稀釋"),
    ("私募", -12, "私募稀釋"),
    ("可轉換公司債", -8, "可轉債稀釋"),
    ("停工", -14, "停工"),
    ("重大訴訟", -9, "重大訴訟風險"),
    ("資安事件", -8, "資安事件"),
    ("財務預測差異", -8, "財測差異"),
    ("更換會計師", -8, "更換會計師"),
)
_HARD_EVENT_KEYWORDS = (
    "破產", "重整", "存款不足", "拒絕往來", "終止上市",
    "停止買賣", "掏空", "重大舞弊", "無法表示意見", "繼續經營重大疑慮",
)


def classify_event_text(title: str, description: str = "") -> dict[str, Any]:
    # MOPS descriptions often repeat legal boilerplate (for example, the
    # spokesman's authority to act in litigation).  Scoring that boilerplate
    # creates severe false negatives, so deterministic impact rules use the
    # announcement headline.  The full description is retained only for the
    # narrowly defined hard-risk terms.
    text = str(title or "").strip()
    risk_text = f"{text}\n{description}".strip()
    impact = 0.0
    reasons: list[str] = []
    for keyword, value, label in _POSITIVE_EVENT_RULES:
        if keyword in text:
            impact += value
            reasons.append(label)
    for keyword, value, label in _NEGATIVE_EVENT_RULES:
        if keyword in text:
            impact += value
            reasons.append(label)
    hard = [
        keyword for keyword in _HARD_EVENT_KEYWORDS if keyword in risk_text
    ]
    if hard:
        impact = min(impact, -35.0)
        reasons.extend(f"重大風險:{keyword}" for keyword in hard)
    if hard:
        event_type = "HARD_RISK"
    elif "增資" in text or "私募" in text or "可轉換公司債" in text:
        event_type = "CAPITAL_ACTION"
    elif any(word in text for word in ("財務", "營收", "獲利", "財測", "股利")):
        event_type = "FINANCIAL"
    elif any(word in text for word in ("訂單", "量產", "產品", "合作", "聯盟")):
        event_type = "OPERATING_CATALYST"
    else:
        event_type = "CORPORATE"
    return {
        "impact": _clamp(impact, -40, 30),
        "hardRisk": bool(hard),
        "hardRiskKeywords": hard,
        "eventType": event_type,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _financial_row(
    income: Mapping[str, Any] | None,
    balance: Mapping[str, Any] | None,
    valuation: Mapping[str, Any] | None,
    layout: str = "ci",
) -> dict[str, Any]:
    income = _clean_row(income or {})
    balance = _clean_row(balance or {})
    valuation = _clean_row(valuation or {})
    revenue = _float(_pick(
        income,
        "營業收入", "淨收益", "收益", "收入",
    ))
    if revenue is None and layout == "basi":
        interest = _float(_pick(income, "利息淨收益"), 0.0) or 0.0
        non_interest = _float(_pick(income, "利息以外淨損益"), 0.0) or 0.0
        revenue = interest + non_interest
    gross = _float(_pick(income, "營業毛利（毛損）淨額", "營業毛利（毛損）"))
    operating = _float(_pick(
        income, "營業利益（損失）", "營業利益",
    ))
    net_income = _float(_pick(
        income,
        "淨利（淨損）歸屬於母公司業主",
        "淨利（損）歸屬於母公司業主",
        "本期稅後淨利（淨損）",
        "本期淨利（淨損）",
    ))
    eps = _float(_pick(income, "基本每股盈餘（元）"))
    current_assets = _float(_pick(balance, "流動資產"))
    assets = _float(_pick(balance, "資產總計", "資產總額"))
    current_liabilities = _float(_pick(balance, "流動負債"))
    liabilities = _float(_pick(balance, "負債總計", "負債總額"))
    equity = _float(_pick(
        balance,
        "歸屬於母公司業主之權益合計",
        "歸屬於母公司業主權益合計",
        "歸屬於母公司業主之權益",
        "權益總計", "權益總額",
    ))
    year = int(_float(_pick(income, "年度", "Year"), 0) or 0)
    if 0 < year < 1911:
        year += 1911
    quarter = int(_float(_pick(income, "季別", "Season"), 0) or 0)
    published = parse_roc_date(_pick(income, "出表日期", "Date"))
    if published is None:
        published = parse_roc_date(_pick(balance, "出表日期", "Date"))
    return {
        "reportYear": year or None,
        "reportQuarter": quarter or None,
        "statementLayout": layout,
        "isFinancialIndustry": layout in {"basi", "bd", "fh", "ins"},
        "statementSnapshotDate": published.isoformat() if published else None,
        "publishedDate": None,
        "revenue": revenue,
        "grossProfit": gross,
        "operatingIncome": operating,
        "netIncome": net_income,
        "eps": eps,
        "grossMarginPercent": (
            gross / revenue * 100
            if gross is not None and revenue and layout == "ci" else None
        ),
        "operatingMarginPercent": (
            operating / revenue * 100
            if operating is not None and revenue and layout in {"ci", "bd", "ins"}
            else None
        ),
        "netMarginPercent": (
            net_income / revenue * 100
            if net_income is not None and revenue and layout in {"ci", "bd", "ins", "mim"}
            else None
        ),
        "currentRatio": (
            current_assets / current_liabilities
            if current_assets is not None and current_liabilities and current_liabilities > 0
            else None
        ),
        "debtRatioPercent": (
            liabilities / assets * 100
            if liabilities is not None and assets and assets > 0 else None
        ),
        "annualizedRoePercent": (
            net_income / equity * (4 / quarter) * 100
            if net_income is not None and equity and equity > 0 and quarter > 0 else None
        ),
        "bookValuePerShare": _float(_pick(balance, "每股參考淨值")),
        "peRatio": _float(_pick(valuation, "PEratio", "PriceEarningRatio")),
        "pbRatio": _float(_pick(valuation, "PBratio", "PriceBookRatio")),
        "dividendYieldPercent": _float(_pick(valuation, "DividendYield", "YieldRatio")),
        "valuationDate": (
            parse_roc_date(_pick(valuation, "Date")).isoformat()
            if parse_roc_date(_pick(valuation, "Date")) else None
        ),
        "source": "TWSE/TPEx official OpenAPI",
        "pointInTimeStatus": "LIVE_CAPTURE_ONLY_NO_FILING_TIMESTAMP",
    }


async def fetch_official_context(trade_date: date) -> dict[str, Any]:
    """Return official events, financials and insider risk as-of trade_date."""
    cache_key = trade_date.isoformat()
    if cache_key in _OFFICIAL_CACHE:
        return _OFFICIAL_CACHE[cache_key]

    official_semaphore = asyncio.Semaphore(6)

    async def fetch_one(name: str, url: str) -> tuple[str, list[dict[str, Any]], str | None]:
        async with official_semaphore:
            try:
                body = await _json_get(url)
                rows = [
                    dict(row) for row in body if isinstance(row, Mapping)
                ] if isinstance(body, list) else []
                return name, rows, None
            except Exception as exc:
                return name, [], f"{type(exc).__name__}: {exc}"

    fetched = await asyncio.gather(
        *(fetch_one(name, url) for name, url in OFFICIAL_ENDPOINTS.items())
    )
    raw = {name: rows for name, rows, _ in fetched}
    source_status = {
        name: {"ok": error is None, "rows": len(rows), "error": error}
        for name, rows, error in fetched
    }

    incomes: dict[str, Mapping[str, Any]] = {}
    balances: dict[str, Mapping[str, Any]] = {}
    layouts: dict[str, str] = {}
    valuations: dict[str, Mapping[str, Any]] = {}
    captured_at = datetime.now(TAIPEI_TZ)
    capture_lag_days = (captured_at.date() - trade_date).days
    live_forward_capture = 0 <= capture_lag_days <= 3
    income_names = [
        name for name in raw
        if name.startswith(("twse_income_", "tpex_income_"))
    ]
    balance_names = [
        name for name in raw
        if name.startswith(("twse_balance_", "tpex_balance_"))
    ]
    for name in income_names:
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "出表日期", "Date"))
            if (
                symbol and published
                and (published <= trade_date or live_forward_capture)
            ):
                incomes[symbol] = row
                layouts[symbol] = name.rsplit("_", 1)[-1]
    for name in balance_names:
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "出表日期", "Date"))
            if (
                symbol and published
                and (published <= trade_date or live_forward_capture)
            ):
                balances[symbol] = row
    for name in ("twse_valuation", "tpex_valuation"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "Date"))
            if symbol and published and published <= trade_date:
                valuations[symbol] = row
    financials = {
        symbol: {
            **_financial_row(
                incomes.get(symbol),
                balances.get(symbol),
                valuations.get(symbol),
                layouts.get(symbol, "ci"),
            ),
            "capturedAt": captured_at.isoformat(),
            "marketImpactBucket": (
                "NEXT_TAIWAN_SESSION_LIVE_OVERLAY"
                if capture_lag_days > 0 else "POST_CLOSE_LIVE_CAPTURE"
            ),
        }
        for symbol in set(incomes) | set(balances) | set(valuations)
    }

    insider_acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"holdings": 0.0, "pledged": 0.0, "relatedHoldings": 0.0, "relatedPledged": 0.0}
    )
    insider_dates: dict[str, date] = {}
    for name in ("twse_insiders", "tpex_insiders"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "出表日期", "Date"))
            if not symbol or published is None or published > trade_date:
                continue
            value = insider_acc[symbol]
            value["holdings"] += _float(_pick(row, "目前持股"), 0.0) or 0.0
            value["pledged"] += _float(_pick(row, "設質股數"), 0.0) or 0.0
            value["relatedHoldings"] += _float(_pick(row, "內部人關係人目前持股合計"), 0.0) or 0.0
            value["relatedPledged"] += _float(_pick(row, "內部人關係人設質股數"), 0.0) or 0.0
            insider_dates[symbol] = max(published, insider_dates.get(symbol, published))

    transfers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in ("twse_transfers", "tpex_transfers"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "出表日期", "Date"))
            if not symbol or published is None or not trade_date - timedelta(days=10) <= published <= trade_date:
                continue
            method = str(_pick(row, "預定轉讓方式及股數-轉讓方式") or "").strip()
            shares = _float(_pick(row, "預定轉讓總股數-自有持股", "預定轉讓方式及股數-轉讓股數"), 0.0) or 0.0
            current = _float(_pick(row, "目前持有股數-自有持股"), 0.0) or 0.0
            transfers[symbol].append({
                "publishedDate": published.isoformat(),
                "identity": str(_pick(row, "申報人身分", "申請人身分") or "").strip(),
                "method": method,
                "shares": round(shares),
                "currentShares": round(current),
                "saleLike": not any(word in method for word in ("贈與", "信託")),
            })

    insider: dict[str, dict[str, Any]] = {}
    for symbol in set(insider_acc) | set(transfers):
        value = insider_acc[symbol]
        holdings = value["holdings"] + value["relatedHoldings"]
        pledged = value["pledged"] + value["relatedPledged"]
        sale_shares = sum(item["shares"] for item in transfers.get(symbol, []) if item["saleLike"])
        insider[symbol] = {
            "snapshotDate": insider_dates.get(symbol).isoformat() if symbol in insider_dates else None,
            "pledgeRatioPercent": round(pledged / holdings * 100, 4) if holdings > 0 else None,
            "plannedSaleShares": round(sale_shares),
            "plannedSaleToInsiderHoldingPercent": round(sale_shares / holdings * 100, 4) if holdings > 0 else None,
            "recentTransfers": transfers.get(symbol, []),
            "source": "TWSE/TPEx insider holdings and transfer OpenAPI",
        }

    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in ("twse_events", "tpex_events"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = _published_at(
                _pick(row, "發言日期", "Date", "出表日期"),
                _pick(row, "發言時間"),
            )
            if not symbol or published is None or not trade_date - timedelta(days=7) <= published.date() <= trade_date:
                continue
            title = str(_pick(row, "主旨") or "").strip()
            description = str(_pick(row, "說明") or "").strip()
            classification = classify_event_text(title, description)
            events[symbol].append({
                "publishedAt": published.isoformat(),
                "title": title,
                "clause": str(_pick(row, "符合條款") or "").strip(),
                "source": "TWSE MOPS" if name.startswith("twse") else "TPEx MOPS",
                **classification,
            })

    attention: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in ("twse_attention", "tpex_attention"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "Date"))
            if symbol and published and trade_date - timedelta(days=5) <= published <= trade_date:
                attention[symbol].append({
                    "date": published.isoformat(),
                    "detail": str(_pick(row, "TradingInfoForAttention", "TradingInformation") or "").strip(),
                    "source": "TWSE attention" if name.startswith("twse") else "TPEx attention",
                })

    disposition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in ("twse_disposition", "tpex_disposition"):
        for row in raw[name]:
            symbol = _symbol(row)
            published = parse_roc_date(_pick(row, "Date"))
            if (
                symbol and published
                and trade_date - timedelta(days=35) <= published <= trade_date
            ):
                period = str(_pick(row, "DispositionPeriod") or "").strip()
                period_start, period_end = parse_roc_period(period)
                disposition[symbol].append({
                    "date": published.isoformat(),
                    "period": period,
                    "periodStart": (
                        period_start.isoformat() if period_start else None
                    ),
                    "periodEnd": period_end.isoformat() if period_end else None,
                    "detail": str(_pick(row, "Detail", "DisposalCondition") or "").strip()[:600],
                    "source": "TWSE disposition" if name.startswith("twse") else "TPEx disposition",
                })

    context = {
        "financials": financials,
        "insider": insider,
        "events": dict(events),
        "attention": dict(attention),
        "disposition": dict(disposition),
        "sourceStatus": source_status,
        "asOfTradeDate": trade_date.isoformat(),
    }
    _OFFICIAL_CACHE[cache_key] = context
    return context


def score_event_context(symbol: str, official: Mapping[str, Any], trade_date: date) -> tuple[float, dict[str, Any]]:
    events = list((official.get("events") or {}).get(symbol, []))
    attention = list((official.get("attention") or {}).get(symbol, []))
    disposition = list((official.get("disposition") or {}).get(symbol, []))
    adjustment = 0.0
    hard_risks: list[str] = []
    for event in events:
        published = datetime.fromisoformat(str(event["publishedAt"])).date()
        age = max(0, (trade_date - published).days)
        decay = 1.0 if age <= 1 else 0.7 if age <= 3 else 0.4
        adjustment += float(event.get("impact") or 0) * decay
        if event.get("hardRisk"):
            hard_risks.extend(event.get("hardRiskKeywords") or [])
    adjustment -= min(12.0, len(attention) * 6.0)
    active_disposition: list[dict[str, Any]] = []
    ended_disposition: list[dict[str, Any]] = []
    for item in disposition:
        start = parse_roc_date(item.get("periodStart"))
        end = parse_roc_date(item.get("periodEnd"))
        published = parse_roc_date(item.get("date"))
        active = bool(start and end and start <= trade_date <= end)
        imminent = bool(
            published == trade_date and start
            and trade_date <= start <= trade_date + timedelta(days=4)
        )
        if active or imminent:
            active_disposition.append(item)
        else:
            ended_disposition.append(item)
    adjustment -= min(32.0, len(active_disposition) * 24.0)
    adjustment -= min(8.0, len(ended_disposition) * 4.0)
    score = _clamp(50 + adjustment)
    return round(score, 2), {
        "events": events[:8],
        "attention": attention[:4],
        "disposition": disposition[:4],
        "activeOrImminentDisposition": active_disposition[:4],
        "hardRisk": bool(hard_risks),
        "hardRiskKeywords": list(dict.fromkeys(hard_risks)),
        "executionBlocked": bool(active_disposition),
        "adjustment": round(adjustment, 2),
        "checked": True,
        "asOfTradeDate": trade_date.isoformat(),
        "source": "TWSE/TPEx MOPS + attention/disposition OpenAPI",
    }


TDCC_LEVEL_MAX = {
    1: 999, 2: 5_000, 3: 10_000, 4: 15_000, 5: 20_000,
    6: 30_000, 7: 40_000, 8: 50_000, 9: 100_000,
    10: 200_000, 11: 400_000, 12: 600_000, 13: 800_000,
    14: 1_000_000, 15: None,
}


def parse_tdcc_csv(content: bytes | str) -> dict[str, dict[str, Any]]:
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("big5", errors="replace")
    else:
        text = content.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"date": None, "under100": 0.0, "over400": 0.0, "holders": None}
    )
    for raw in reader:
        row = _clean_row(raw)
        symbol = str(_pick(row, "證券代號", "股票代號") or "").strip()
        level_match = re.search(r"\d+", str(_pick(row, "持股分級", "持股/單位數分級") or ""))
        if len(symbol) != 4 or not symbol.isdigit() or not level_match:
            continue
        level = int(level_match.group())
        if not 1 <= level <= 17:
            continue
        day = parse_roc_date(_pick(row, "資料日期", "日期"))
        percent = _float(_pick(
            row, "占集保庫存數比例%", "佔集保庫存數比例%",
            "占集保庫存數比例", "佔集保庫存數比例",
        ), 0.0) or 0.0
        target = grouped[symbol]
        if day:
            target["date"] = day
        if level == 17:
            target["holders"] = int(_float(_pick(row, "人數", "持有人數"), 0.0) or 0.0)
            continue
        if level == 16:
            continue
        upper = TDCC_LEVEL_MAX.get(level)
        if upper is not None and upper <= 100_000:
            target["under100"] += percent
        if level >= 12:
            target["over400"] += percent
    return {
        symbol: {
            "snapshotDate": value["date"].isoformat() if value["date"] else None,
            "under100LotsPercent": round(value["under100"], 4),
            "over400LotsPercent": round(value["over400"], 4),
            "holderCount": value["holders"],
            "source": "TDCC OpenData 1-5",
        }
        for symbol, value in grouped.items() if value["date"] is not None
    }


async def fetch_tdcc_context(symbols: Sequence[str], trade_date: date) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    now = datetime.now(TAIPEI_TZ)
    cached_data = _TDCC_CACHE.get("data")
    expires = _TDCC_CACHE.get("expires")
    if not cached_data or not isinstance(expires, datetime) or now >= expires:
        async with _TDCC_LOCK:
            cached_data = _TDCC_CACHE.get("data")
            expires = _TDCC_CACHE.get("expires")
            if not cached_data or not isinstance(expires, datetime) or now >= expires:
                try:
                    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                        response = await client.get(
                            TDCC_URL,
                            headers={"Accept": "text/csv,*/*;q=0.8", "User-Agent": "TaiwanStockMCP/12.4"},
                        )
                        response.raise_for_status()
                    cached_data = parse_tdcc_csv(response.content)
                    _TDCC_CACHE.update({
                        "data": cached_data,
                        "expires": now + timedelta(hours=12 if now.weekday() in {4, 5} else 24),
                        "status": {"ok": True, "rows": len(cached_data), "error": None},
                    })
                except Exception as exc:
                    cached_data = cached_data or {}
                    _TDCC_CACHE["status"] = {"ok": False, "rows": len(cached_data), "error": f"{type(exc).__name__}: {exc}"}

    selected: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        value = dict((cached_data or {}).get(symbol) or {})
        snapshot_date = parse_roc_date(value.get("snapshotDate"))
        if value and snapshot_date and snapshot_date <= trade_date:
            selected[symbol] = value

    try:
        await ensure_advanced_schema()
        rows = []
        for symbol, value in selected.items():
            day = parse_roc_date(value.get("snapshotDate"))
            if day:
                rows.append((symbol, day, value.get("under100LotsPercent"), value.get("over400LotsPercent"), value.get("holderCount")))
        async with stock_database.acquire() as connection:
            if rows:
                await connection.executemany("""
                    INSERT INTO tdcc_distribution_snapshots(
                        symbol,snapshot_date,under_100_lots_percent,
                        over_400_lots_percent,holder_count,captured_at
                    ) VALUES($1,$2,$3,$4,$5,NOW())
                    ON CONFLICT(symbol,snapshot_date) DO UPDATE SET
                        under_100_lots_percent=EXCLUDED.under_100_lots_percent,
                        over_400_lots_percent=EXCLUDED.over_400_lots_percent,
                        holder_count=EXCLUDED.holder_count,captured_at=NOW()
                """, rows)
            previous_rows = await connection.fetch("""
                SELECT DISTINCT ON(symbol) symbol,snapshot_date,
                       under_100_lots_percent,over_400_lots_percent
                FROM tdcc_distribution_snapshots
                WHERE symbol=ANY($1::varchar[]) AND snapshot_date < $2
                ORDER BY symbol,snapshot_date DESC
            """, list(symbols), trade_date)
        previous = {str(row["symbol"]): dict(row) for row in previous_rows}
        for symbol, value in selected.items():
            prior = previous.get(symbol)
            current_day = parse_roc_date(value.get("snapshotDate"))
            if prior and current_day and prior.get("snapshot_date") < current_day:
                prior_small = _float(prior.get("under_100_lots_percent"))
                prior_large = _float(prior.get("over_400_lots_percent"))
                value["previousSnapshotDate"] = prior["snapshot_date"].isoformat()
                value["under100LotsPercentChange"] = round(
                    (_float(value.get("under100LotsPercent"), 0.0) or 0.0) - (prior_small or 0.0), 4
                ) if prior_small is not None else None
                value["over400LotsPercentChange"] = round(
                    (_float(value.get("over400LotsPercent"), 0.0) or 0.0) - (prior_large or 0.0), 4
                ) if prior_large is not None else None
    except RuntimeError:
        pass
    return selected, dict(_TDCC_CACHE.get("status") or {"ok": False, "rows": 0, "error": "not fetched"})


def score_news_rows(rows: Sequence[Mapping[str, Any]], trade_date: date) -> tuple[float | None, dict[str, Any]]:
    if rows is None:
        return None, {"reason": "新聞資料未檢查"}
    positive = {
        "調升": 5, "升評": 6, "目標價上調": 6, "優於預期": 7,
        "創新高": 5, "接單": 4, "訂單": 3, "量產": 3, "成長": 2,
    }
    negative = {
        "調降": -5, "降評": -6, "目標價下調": -6, "不如預期": -7,
        "衰退": -3, "虧損": -4, "增資": -4, "裁員": -3, "停工": -5,
    }
    adjustment = 0.0
    sources: set[str] = set()
    headlines: list[dict[str, Any]] = []
    analyst_hits: list[str] = []
    seen_titles: set[str] = set()
    for row in rows:
        row_date = parse_roc_date(row.get("date"))
        if row_date and row_date > trade_date:
            continue
        title = str(row.get("title") or "").strip()
        title_key = re.sub(r"\s+", "", title).lower()
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        description = str(row.get("description") or "").strip()
        text = f"{title} {description}"
        local = sum(value for keyword, value in positive.items() if keyword in text)
        local += sum(value for keyword, value in negative.items() if keyword in text)
        age = (trade_date - row_date).days if row_date else 7
        decay = 1.0 if age <= 1 else 0.7 if age <= 3 else 0.4
        local = _clamp(local, -10, 10) * decay
        adjustment += local
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
        if any(word in text for word in ("調升", "調降", "升評", "降評", "目標價")):
            analyst_hits.append(title)
        headlines.append({
            "date": row_date.isoformat() if row_date else str(row.get("date") or ""),
            "title": title,
            "source": source,
            "link": row.get("link"),
            "impact": round(local, 2),
        })
    count = len(headlines)
    if count >= 6:
        adjustment += 5
    elif count >= 3:
        adjustment += 2
    if len(sources) >= 3:
        adjustment += 2
    adjustment = _clamp(adjustment, -20, 20)
    score = _clamp(50 + adjustment)
    headlines.sort(key=lambda item: (abs(float(item["impact"])), item["date"]), reverse=True)
    return round(score, 2), {
        "newsCount": count,
        "sourceCount": len(sources),
        "analystRevisionMentions": analyst_hits[:5],
        "topHeadlines": headlines[:6],
        "adjustment": round(adjustment, 2),
        "source": "FinMind TaiwanStockNews deterministic sentiment",
        "asOfTradeDate": trade_date.isoformat(),
        "manipulationGuard": "情緒權重受上限約束，不能單獨形成正式買進訊號",
    }


def score_cashflow_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"available": False, "reason": "現金流量資料無回傳"}
    latest_date = max(str(row.get("date") or "") for row in rows)
    latest = [row for row in rows if str(row.get("date") or "") == latest_date]
    operating = capex = None
    for row in latest:
        kind = str(row.get("type") or "")
        origin = str(row.get("origin_name") or "")
        value = _float(row.get("value"))
        text = f"{kind} {origin}"
        if any(word in text for word in ("CashFlowsFromOperatingActivities", "營業活動之淨現金")):
            operating = value
        if any(word in text for word in ("PropertyPlantAndEquipment", "不動產、廠房及設備")) and value is not None:
            capex = (capex or 0.0) + abs(value)
    free_cash = operating - capex if operating is not None and capex is not None else None
    return {
        "available": operating is not None or free_cash is not None,
        "statementDate": latest_date or None,
        "operatingCashFlow": operating,
        "capitalExpenditure": capex,
        "freeCashFlow": free_cash,
        "source": "FinMind TaiwanStockCashFlowsStatement",
    }


GLOBAL_ASSETS: tuple[str, ...] = (
    "^GSPC", "^IXIC", "^SOX", "EWT", "TSM", "UMC", "ASX",
    "SOXX", "SMH", "QQQ", "XBI", "TAN", "XLI", "XLB", "XLE",
    "XLF", "BDRY", "JETS", "ITB", "IYZ", "GLD",
)
ADR_MAP = {"2330": "TSM", "2303": "UMC", "3711": "ASX"}
INDUSTRY_DRIVER_MAP: tuple[tuple[tuple[str, ...], tuple[tuple[str, float], ...]], ...] = (
    (("半導體",), (("^SOX", 1), ("SOXX", 1), ("SMH", 1))),
    (("電子零組件", "電腦及週邊", "其他電子", "資訊服務", "數位雲端"), (("QQQ", 1), ("SOXX", 0.7))),
    (("光電",), (("TAN", 0.7), ("QQQ", 0.5))),
    (("通信網路",), (("IYZ", 1), ("QQQ", 0.5))),
    (("生技",), (("XBI", 1),)),
    (("航運",), (("BDRY", 1), ("JETS", 0.4))),
    (("鋼鐵", "玻璃", "水泥", "塑膠", "化學"), (("XLB", 1),)),
    (("油電燃氣",), (("XLE", 1),)),
    (("金融",), (("XLF", 1),)),
    (("建材營造",), (("ITB", 1),)),
    (("汽車",), (("XLI", 0.6), ("QQQ", 0.4))),
)

FinMindFetcher = Callable[[str, str, date, date], Awaitable[list[dict[str, Any]]]]


def _asset_summary(
    rows: Sequence[Mapping[str, Any]],
    before: date,
    *,
    inclusive: bool = False,
) -> dict[str, Any] | None:
    usable = []
    for row in rows:
        day = parse_roc_date(row.get("date"))
        close = _float(_pick(row, "Adj_Close", "Close", "close", "price", "value", "spot_sell"))
        within_cutoff = bool(
            day and (day <= before if inclusive else day < before)
        )
        if within_cutoff and close is not None and close > 0:
            usable.append((day, close))
    usable.sort(key=lambda value: value[0])
    if len(usable) < 2:
        return None
    latest_day, latest = usable[-1]
    prior = usable[-2][1]
    base5 = usable[-6][1] if len(usable) >= 6 else usable[0][1]
    return {
        "latestDate": latest_day.isoformat(),
        "last": round(latest, 4),
        "change1dPercent": round((latest / prior - 1) * 100, 4) if prior else None,
        "change5dPercent": round((latest / base5 - 1) * 100, 4) if base5 else None,
        "pointInTimeRule": (
            f"只使用日期不晚於台股交易日 {before.isoformat()} 的完整資料"
            if inclusive
            else f"只使用日期早於台股交易日 {before.isoformat()} 的完整資料"
        ),
    }


def _global_score(assets: Mapping[str, Mapping[str, Any]]) -> float | None:
    core = [
        assets[ticker]
        for ticker in ("^GSPC", "^IXIC", "^SOX", "EWT")
        if ticker in assets
    ]
    if not core:
        return None
    one_day = [
        float(item["change1dPercent"])
        for item in core if item.get("change1dPercent") is not None
    ]
    five_day = [
        float(item["change5dPercent"])
        for item in core if item.get("change5dPercent") is not None
    ]
    return _clamp(
        50
        + (mean(one_day) * 4 if one_day else 0)
        + (mean(five_day) if five_day else 0)
    )


async def build_global_market_context(
    trade_date: date,
    fetcher: FinMindFetcher,
    *,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    start = trade_date - timedelta(days=16)
    assets_results = await asyncio.gather(
        *(fetcher("USStockPrice", ticker, start, trade_date) for ticker in GLOBAL_ASSETS),
        return_exceptions=True,
    )
    completed_assets: dict[str, dict[str, Any]] = {}
    next_session_assets: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    now = current_time or datetime.now(TAIPEI_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI_TZ)
    now = now.astimezone(TAIPEI_TZ)
    days_after_trade = (now.date() - trade_date).days
    preopen_overlay_allowed = 1 <= days_after_trade <= 3
    for ticker, result in zip(GLOBAL_ASSETS, assets_results):
        if isinstance(result, Exception):
            errors.append(f"{ticker}: {type(result).__name__}: {result}")
            continue
        summary = _asset_summary(result, trade_date)
        if summary:
            completed_assets[ticker] = summary
        overlay = _asset_summary(result, trade_date, inclusive=True)
        if (
            preopen_overlay_allowed
            and overlay
            and overlay.get("latestDate") == trade_date.isoformat()
        ):
            next_session_assets[ticker] = {
                **overlay,
                "marketImpactBucket": "NEXT_TAIWAN_SESSION",
                "liveForwardOnly": True,
            }

    macro_specs = (
        ("usdTwd", "TaiwanExchangeRate", "USD"),
        ("us10y", "GovernmentBondsYield", "United States 10-Year"),
        ("wti", "CrudeOilPrices", "WTI"),
        ("brent", "CrudeOilPrices", "Brent"),
    )
    macro_results = await asyncio.gather(
        *(fetcher(dataset, data_id, trade_date - timedelta(days=30), trade_date) for _, dataset, data_id in macro_specs),
        return_exceptions=True,
    )
    completed_macro: dict[str, Any] = {}
    next_session_macro: dict[str, Any] = {}
    for (name, _, _), result in zip(macro_specs, macro_results):
        if isinstance(result, Exception):
            errors.append(f"{name}: {type(result).__name__}: {result}")
            continue
        summary = _asset_summary(result, trade_date)
        if summary:
            completed_macro[name] = summary
        overlay = _asset_summary(result, trade_date, inclusive=True)
        if (
            preopen_overlay_allowed
            and overlay
            and overlay.get("latestDate") == trade_date.isoformat()
        ):
            next_session_macro[name] = {
                **overlay,
                "marketImpactBucket": "NEXT_TAIWAN_SESSION",
                "liveForwardOnly": True,
            }

    assets = {**completed_assets, **next_session_assets}
    macro = {**completed_macro, **next_session_macro}
    score = _global_score(assets)
    completed_score = _global_score(completed_assets)
    next_core_count = sum(
        ticker in next_session_assets
        for ticker in ("^GSPC", "^IXIC", "^SOX", "EWT")
    )
    return {
        "score": round(score, 2) if score is not None else None,
        "completedSessionScore": (
            round(completed_score, 2) if completed_score is not None else None
        ),
        "assets": assets,
        "completedAssets": completed_assets,
        "nextSessionAssets": next_session_assets,
        "macro": macro,
        "completedMacro": completed_macro,
        "nextSessionMacro": next_session_macro,
        "source": "FinMind USStockPrice + macro datasets",
        "sourceStatus": {
            "ok": score is not None,
            "assetCount": len(assets),
            "nextSessionAssetCount": len(next_session_assets),
            "errors": errors[:10],
        },
        "taiwanImpactTiming": {
            "completedUSSession": (
                "日期早於台股訊號日：視為本交易日已反映的環境資料"
            ),
            "nextTaiwanSession": (
                "訊號日同日美股收盤資料只在台灣次日盤前即時重跑時納入"
            ),
            "effectivePhase": (
                "PREOPEN_NEXT_SESSION_OVERLAY"
                if next_session_assets else "POST_CLOSE_BASE"
            ),
            "nextSessionUSCloseAvailable": bool(next_session_assets),
            "nextSessionInputsFinal": next_core_count >= 2,
            "capturedAt": now.isoformat(),
        },
        "asOfTradeDate": trade_date.isoformat(),
    }


async def build_derivatives_context(trade_date: date, fetcher: FinMindFetcher) -> dict[str, Any]:
    start = trade_date - timedelta(days=12)
    futures, fut_inst, opt_inst, options, live = await asyncio.gather(
        fetcher("TaiwanFuturesDaily", "TX", start, trade_date),
        fetcher("TaiwanFuturesInstitutionalInvestors", "TX", start, trade_date),
        fetcher("TaiwanOptionInstitutionalInvestors", "TXO", start, trade_date),
        fetcher("TaiwanOptionDaily", "TXO", trade_date - timedelta(days=5), trade_date),
        fetcher("taiwan_futures_snapshot", "TXF", trade_date, trade_date),
        return_exceptions=True,
    )
    datasets = [value if not isinstance(value, Exception) else [] for value in (futures, fut_inst, opt_inst, options, live)]
    futures_rows, fut_inst_rows, opt_inst_rows, option_rows, live_rows = datasets
    score = 50.0
    features: dict[str, Any] = {}

    valid_futures = [row for row in futures_rows if (parse_roc_date(row.get("date")) or date.min) <= trade_date]
    if valid_futures:
        latest_date = max(parse_roc_date(row.get("date")) or date.min for row in valid_futures)
        latest = [row for row in valid_futures if parse_roc_date(row.get("date")) == latest_date]
        night = [row for row in latest if any(word in str(row.get("trading_session") or "").lower() for word in ("after", "night", "盤後"))]
        chosen = night[-1] if night else latest[-1]
        open_price = _float(chosen.get("open"))
        close = _float(chosen.get("close"))
        change = (close / open_price - 1) * 100 if close and open_price else _float(chosen.get("spread_per"))
        if change is not None:
            score += max(-10, min(10, change * 3))
        features["txFutures"] = {
            "date": latest_date.isoformat(), "session": chosen.get("trading_session"),
            "changePercent": round(change, 4) if change is not None else None,
            "isAfterHours": bool(night),
        }

    live_overlay = False
    if live_rows:
        row = live_rows[-1]
        live_at = parse_timestamp(row.get("date"))
        live_overlay = bool(
            live_at
            and (
                (live_at.date() == trade_date and live_at.hour >= 15)
                or (
                    live_at.date() == trade_date + timedelta(days=1)
                    and live_at.hour < 6
                )
            )
        )
        live_change = _float(_pick(row, "change_rate", "changePercent"))
        if live_overlay and live_change is not None:
            score += max(-10, min(10, live_change * 2.5))
        feature_name = (
            "nextSessionNightSnapshot"
            if live_overlay else "alreadyReflectedOrStaleLiveSnapshot"
        )
        features[feature_name] = {
            "date": row.get("date"),
            "changePercent": live_change,
            "close": _float(row.get("close")),
            "source": "FinMind taiwan_futures_snapshot",
            "includedInScore": live_overlay,
        }

    foreign_fut = [
        row for row in fut_inst_rows
        if "外資" in str(row.get("institutional_investors") or "")
        and (parse_roc_date(row.get("date")) or date.min) <= trade_date
    ]
    if foreign_fut:
        latest_day = max(str(row.get("date") or "") for row in foreign_fut)
        row = [
            value for value in foreign_fut
            if str(value.get("date") or "") == latest_day
        ][-1]
        net_oi = (_float(row.get("long_open_interest_balance_volume"), 0.0) or 0.0) - (_float(row.get("short_open_interest_balance_volume"), 0.0) or 0.0)
        score += math.tanh(net_oi / 20_000) * 8
        features["foreignFuturesNetOpenInterest"] = round(net_oi)

    foreign_opt = [
        row for row in opt_inst_rows
        if "外資" in str(row.get("institutional_investors") or "")
        and (parse_roc_date(row.get("date")) or date.min) <= trade_date
    ]
    if foreign_opt:
        latest_day = max(str(row.get("date") or "") for row in foreign_opt)
        latest = [row for row in foreign_opt if str(row.get("date") or "") == latest_day]
        call_net = put_net = 0.0
        for row in latest:
            net = (_float(row.get("long_open_interest_balance_volume"), 0.0) or 0.0) - (_float(row.get("short_open_interest_balance_volume"), 0.0) or 0.0)
            if "put" in str(row.get("call_put") or "").lower() or "賣" in str(row.get("call_put") or ""):
                put_net += net
            else:
                call_net += net
        option_direction = call_net - put_net
        score += math.tanh(option_direction / 20_000) * 5
        features["foreignOptionDirectionalOpenInterest"] = round(option_direction)

    usable_options = [row for row in option_rows if (parse_roc_date(row.get("date")) or date.min) <= trade_date]
    if usable_options:
        latest_day = max(str(row.get("date") or "") for row in usable_options)
        latest = [row for row in usable_options if str(row.get("date") or "") == latest_day]
        put_volume = sum(_float(row.get("volume"), 0.0) or 0.0 for row in latest if "put" in str(row.get("call_put") or "").lower() or "賣" in str(row.get("call_put") or ""))
        call_volume = sum(_float(row.get("volume"), 0.0) or 0.0 for row in latest if not ("put" in str(row.get("call_put") or "").lower() or "賣" in str(row.get("call_put") or "")))
        pc_ratio = put_volume / call_volume if call_volume > 0 else None
        if pc_ratio is not None:
            score += max(-5, min(5, (1.0 - pc_ratio) * 5))
        features["putCallVolumeRatio"] = round(pc_ratio, 4) if pc_ratio is not None else None

    available = any(
        key != "alreadyReflectedOrStaleLiveSnapshot" for key in features
    )
    return {
        "score": round(_clamp(score), 2) if available else None,
        "features": features,
        "source": "FinMind TAIFEX-derived datasets",
        "aggregateWarning": "法人期權數字是類別合計互抵結果，只作環境分數，不單獨觸發買點",
        "taiwanImpactTiming": {
            "dailyAndInstitutional": "交易日資料視為收盤後環境與籌碼訊號",
            "nightOverlayIncluded": live_overlay,
            "nightOverlayRule": "僅納入訊號日15:00後至次日06:00的即時台指期快照",
        },
        "asOfTradeDate": trade_date.isoformat(),
        "available": available,
    }


def score_sector_driver(symbol: str, industry: str, global_context: Mapping[str, Any]) -> tuple[float | None, dict[str, Any]]:
    assets = global_context.get("assets") or {}
    drivers: list[tuple[str, float]] = []
    for keywords, configured in INDUSTRY_DRIVER_MAP:
        if any(keyword in industry for keyword in keywords):
            drivers.extend(configured)
            break
    adr = ADR_MAP.get(symbol)
    if adr:
        drivers.append((adr, 1.2))
    observations: list[dict[str, Any]] = []
    weighted = weight_sum = 0.0
    for ticker, direction in drivers:
        value = assets.get(ticker)
        if not isinstance(value, Mapping):
            continue
        one = _float(value.get("change1dPercent"))
        five = _float(value.get("change5dPercent"))
        if one is None and five is None:
            continue
        impulse = (one or 0.0) * 0.75 + (five or 0.0) * 0.25
        weighted += impulse * direction
        weight_sum += abs(direction)
        observations.append({"ticker": ticker, "direction": direction, **dict(value)})
    if not observations or weight_sum <= 0:
        return None, {"reason": "此產業尚無可用的海外領先指標", "industry": industry}
    driver_change = weighted / weight_sum
    score = _clamp(50 + driver_change * 4)
    return round(score, 2), {
        "industry": industry,
        "driverChangePercent": round(driver_change, 4),
        "drivers": observations,
        "source": global_context.get("source"),
        "timing": (global_context.get("taiwanImpactTiming") or {}).get("completedUSSession"),
    }


def _pearson(left: Mapping[date, float], right: Mapping[date, float]) -> float | None:
    common = sorted(set(left) & set(right))
    if len(common) < 10:
        return None
    xs = [left[day] for day in common]
    ys = [right[day] for day in common]
    mx, my = mean(xs), mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return numerator / (dx * dy) if dx > 0 and dy > 0 else None


async def portfolio_risk_map(candidates: Sequence[Mapping[str, Any]], themes: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    symbols = [str(item.get("symbol") or "").strip() for item in candidates]
    industries = {str(item.get("symbol") or "").strip(): str(item.get("industry") or "").strip() for item in candidates}
    returns: dict[str, dict[date, float]] = defaultdict(dict)
    try:
        async with stock_database.acquire() as connection:
            rows = await connection.fetch("""
                WITH ranked AS (
                    SELECT symbol,trade_date,close,
                           ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                    FROM daily_bars WHERE symbol=ANY($1::varchar[])
                )
                SELECT symbol,trade_date,close FROM ranked
                WHERE rn<=31 ORDER BY symbol,trade_date
            """, symbols)
        closes: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for row in rows:
            close = _float(row["close"])
            if close and close > 0:
                closes[str(row["symbol"])].append((row["trade_date"], close))
        for symbol, values in closes.items():
            values.sort()
            for (day, close), (_, prior) in zip(values[1:], values[:-1]):
                if prior > 0:
                    returns[symbol][day] = close / prior - 1
    except RuntimeError:
        pass

    output: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        correlations: list[float] = []
        for other in symbols:
            if other == symbol:
                continue
            value = _pearson(returns.get(symbol, {}), returns.get(other, {}))
            if value is not None:
                correlations.append(value)
        avg_corr = mean(correlations) if correlations else None
        same_industry = sum(1 for other in symbols if other != symbol and industries.get(other) and industries.get(other) == industries.get(symbol))
        own_themes = set(themes.get(symbol, []))
        same_theme = sum(1 for other in symbols if other != symbol and own_themes.intersection(themes.get(other, [])))
        penalty = max(0, same_industry - 3) * 0.6 + max(0, same_theme - 2) * 0.5
        if avg_corr is not None and avg_corr > 0.78:
            penalty += (avg_corr - 0.78) * 12
        penalty = min(6.0, penalty)
        output[symbol] = {
            "score": round(_clamp(100 - penalty * 10), 2),
            "adjustment": round(-penalty, 2),
            "averageCandidateCorrelation20d": round(avg_corr, 4) if avg_corr is not None else None,
            "sameIndustryCandidates": same_industry,
            "sameThemeCandidates": same_theme,
            "reason": "只扣除集中與高相關風險，不因低相關額外灌分",
        }
    return output
