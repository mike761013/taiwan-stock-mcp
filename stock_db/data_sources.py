"""Market-data sources for V10.5.

Historical backfill uses FinMind TaiwanStockPrice when FINMIND_TOKEN is
available. Daily whole-market updates use official TWSE/TPEx OpenAPI data.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
TWSE_DAILY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_DAILY_FALLBACK_URL = (
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    "?date={date}&type=ALLBUT0999&response=json"
)
TPEX_DAILY_FALLBACK_URL = (
    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
    "?date={date}&id=&response=json"
)
TWSE_SECURITIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_SECURITIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
COMMON_STOCK_SYMBOL_RE = re.compile(r"^[1-9][0-9]{3}$")
V12_MIN_TWSE_COMMON_STOCKS = int(
    os.getenv("V12_MIN_TWSE_COMMON_STOCKS", "950")
)
V12_MIN_TPEX_COMMON_STOCKS = int(
    os.getenv("V12_MIN_TPEX_COMMON_STOCKS", "750")
)


def _official_fallback_enabled() -> bool:
    value = os.getenv("STOCK_DB_FALLBACK_ENABLED")
    if value is None:
        return True
    return value.strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "---"):
        return None
    try:
        text = (
            str(value)
            .replace(",", "")
            .replace("＋", "+")
            .replace("－", "-")
            .replace("−", "-")
            .replace("▲", "+")
            .replace("△", "+")
            .replace("▼", "-")
            .replace("▽", "-")
            .strip()
        )
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    value = _number(value)
    return int(value) if value is not None else None


async def _get_json(url: str, *, params: dict[str, Any] | None = None,
                    headers: dict[str, str] | None = None,
                    timeout: float = 45) -> Any:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "TaiwanStockMCP/12 (+official-fallback)",
    }
    request_headers.update(headers or {})
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
    ) as client:
        response = await client.get(
            url,
            params=params,
            headers=request_headers,
        )
        response.raise_for_status()
        return response.json()


async def fetch_security_master() -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for market, url in (("TWSE", TWSE_SECURITIES_URL), ("TPEx", TPEX_SECURITIES_URL)):
        try:
            rows = await _get_json(url)
        except Exception:
            continue
        for row in rows if isinstance(rows, list) else []:
            symbol = str(
                row.get("公司代號") or row.get("SecuritiesCompanyCode")
                or row.get("Code") or row.get("股票代號") or ""
            ).strip()
            name = str(
                row.get("公司簡稱") or row.get("CompanyAbbreviation")
                or row.get("Name") or row.get("股票名稱") or symbol
            ).strip()
            if not symbol or not symbol[:1].isdigit():
                continue
            results[symbol] = {
                "symbol": symbol,
                "name": name or symbol,
                "market": market,
                "industry": str(
                    row.get("產業別") or row.get("Industry") or ""
                ).strip() or None,
                "is_active": True,
            }
    return sorted(results.values(), key=lambda x: x["symbol"])


async def fetch_finmind_history(
    symbol: str, start_date: date, end_date: date
) -> list[dict[str, Any]]:
    token = (os.getenv("FINMIND_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("FINMIND_TOKEN is required for three-year backfill.")
    body = await _get_json(
        FINMIND_URL,
        params={
            "dataset": "TaiwanStockPrice",
            "data_id": symbol,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=90,
    )
    if not isinstance(body, dict) or int(body.get("status", 200)) != 200:
        raise RuntimeError(str(body.get("msg") if isinstance(body, dict) else body))
    rows = body.get("data") or []
    output = []
    for row in rows:
        output.append({
            "symbol": symbol,
            "date": row.get("date"),
            "open": row.get("open"),
            "high": row.get("max"),
            "low": row.get("min"),
            "close": row.get("close"),
            "volume": row.get("Trading_Volume"),
            "turnover": row.get("Trading_money"),
            "change_percent": row.get("spread"),
            "source": "FinMind TaiwanStockPrice",
        })
    return output


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("\u3000", " ").strip()


def _normalised_field_name(value: Any) -> str:
    return re.sub(r"[\s\u3000]", "", _plain_text(value)).lower()


def _find_table_value(
    record: dict[str, Any],
    aliases: tuple[str, ...],
) -> tuple[Any, str | None]:
    normalised = {
        _normalised_field_name(key): key
        for key in record
    }
    for alias in aliases:
        key = normalised.get(_normalised_field_name(alias))
        if key is not None:
            return record.get(key), str(key)
    return None, None


def _normalise_trade_date(value: Any) -> str | None:
    """Normalise Gregorian and ROC dates to YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(TAIPEI_TZ).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    raw = _plain_text(value)
    if not raw:
        return None

    iso_match = re.fullmatch(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
        raw,
    )
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    embedded = re.search(
        r"(?<!\d)(\d{3,4})\s*[年/-]\s*(\d{1,2})"
        r"\s*[月/-]\s*(\d{1,2})\s*日?",
        raw,
    )
    if embedded:
        year, month, day = map(int, embedded.groups())
        if year < 1911:
            year += 1911
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    digits = re.sub(r"\D", "", raw)
    try:
        if len(digits) == 8:
            return date(
                int(digits[:4]),
                int(digits[4:6]),
                int(digits[6:8]),
            ).isoformat()
        if len(digits) == 7:
            return date(
                int(digits[:3]) + 1911,
                int(digits[3:5]),
                int(digits[5:7]),
            ).isoformat()
    except ValueError:
        return None
    return None


def _response_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [
            row
            for row in payload
            if isinstance(row, dict)
        ]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "Data", "rows", "result", "results"):
        value = payload.get(key)
        if isinstance(value, list) and (
            not value or isinstance(value[0], dict)
        ):
            return [
                row
                for row in value
                if isinstance(row, dict)
            ]
    return []


def _official_table_records(payload: Any) -> list[dict[str, Any]]:
    """Extract stock rows from nested TWSE/TPEx table-style payloads."""
    records: list[dict[str, Any]] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            fields: list[str] | None = None
            for key in (
                "fields",
                "Fields",
                "columns",
                "Columns",
                "columnNames",
            ):
                value = node.get(key)
                if isinstance(value, list) and value:
                    fields = [_plain_text(item) for item in value]
                    break
            data_rows: list[Any] | None = None
            for key in ("data", "Data", "rows", "aaData"):
                value = node.get(key)
                if isinstance(value, list):
                    data_rows = value
                    break
            if fields is not None and data_rows is not None:
                field_tokens = {
                    _normalised_field_name(item)
                    for item in fields
                }
                has_symbol = bool(
                    field_tokens
                    & {
                        _normalised_field_name("證券代號"),
                        _normalised_field_name("股票代號"),
                        _normalised_field_name("代號"),
                    }
                )
                has_close = bool(
                    field_tokens
                    & {
                        _normalised_field_name("收盤價"),
                        _normalised_field_name("收盤"),
                    }
                )
                if has_symbol and has_close:
                    for row in data_rows:
                        if isinstance(row, dict):
                            records.append(dict(row))
                        elif isinstance(row, (list, tuple)):
                            records.append({
                                fields[index]: (
                                    row[index]
                                    if index < len(row)
                                    else None
                                )
                                for index in range(len(fields))
                            })
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)

    visit(payload)
    return records


def _payload_trade_dates(payload: Any) -> list[str]:
    dates: set[str] = set()

    def maybe_add(value: Any) -> None:
        normalised = _normalise_trade_date(value)
        if normalised:
            dates.add(normalised)

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                key_token = _normalised_field_name(key)
                if any(
                    token in key_token
                    for token in ("date", "日期", "年月日")
                ):
                    maybe_add(value)
                elif key_token in {
                    "title",
                    "subtitle",
                    "說明",
                    "名稱",
                }:
                    maybe_add(value)
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)

    visit(payload)
    return sorted(dates)


def _signed_change(row: dict[str, Any]) -> float | None:
    raw = _pick(
        row,
        "Change",
        "ChangeAmount",
        "PriceChange",
        "漲跌價差",
        "漲跌",
        "漲跌幅",
    )
    value = _number(raw)
    if value is None:
        return None
    sign = str(_pick(
        row,
        "ChangeSign",
        "UpDown",
        "Trend",
        "漲跌符號",
        "漲跌註記",
        "漲跌(+/-)",
    ) or "").strip()
    if sign in {"-", "－", "跌", "down", "DOWN", "red_down"}:
        return -abs(value)
    if sign in {"+", "＋", "漲", "up", "UP", "red_up"}:
        return abs(value)
    return value


def _normalise_official_row(
    row: dict[str, Any],
    market: str,
    *,
    forced_date: str | None = None,
    source: str,
) -> dict[str, Any] | None:
    symbol = str(_pick(
        row,
        "Code",
        "SecuritiesCompanyCode",
        "SecuritiesCode",
        "StockCode",
        "證券代號",
        "股票代號",
        "代號",
    ) or "").strip()
    if not COMMON_STOCK_SYMBOL_RE.fullmatch(symbol):
        return None

    trade_date = forced_date or _normalise_trade_date(_pick(
        row,
        "Date",
        "TradeDate",
        "ReportDate",
        "資料日期",
        "日期",
    ))
    return {
        "symbol": symbol,
        "name": str(_pick(
            row,
            "Name",
            "CompanyName",
            "SecuritiesCompanyName",
            "StockName",
            "證券名稱",
            "股票名稱",
            "名稱",
        ) or symbol).strip(),
        "market": market,
        "date": trade_date,
        "open": _number(_pick(
            row,
            "OpeningPrice",
            "Open",
            "OpenPrice",
            "開盤價",
            "開盤",
        )),
        "high": _number(_pick(
            row,
            "HighestPrice",
            "High",
            "HighPrice",
            "最高價",
            "最高",
        )),
        "low": _number(_pick(
            row,
            "LowestPrice",
            "Low",
            "LowPrice",
            "最低價",
            "最低",
        )),
        "close": _number(_pick(
            row,
            "ClosingPrice",
            "Close",
            "ClosePrice",
            "收盤價",
            "收盤",
        )),
        "volume": _integer(_pick(
            row,
            "TradeVolume",
            "TradingShares",
            "TradingVolume",
            "Volume",
            "成交股數",
            "成交量",
        )),
        "turnover": _integer(_pick(
            row,
            "TradeValue",
            "TransactionAmount",
            "TradingAmount",
            "Amount",
            "成交金額",
            "成交值",
        )),
        # The existing database column stores the official price-change amount.
        "change_percent": _signed_change(row),
        "source": source,
    }


def _fallback_record_to_official_row(
    record: dict[str, Any],
    market: str,
    trade_date: str,
    source: str,
) -> dict[str, Any] | None:
    symbol, _ = _find_table_value(
        record,
        ("證券代號", "股票代號", "代號", "Code", "StockCode"),
    )
    name, _ = _find_table_value(
        record,
        ("證券名稱", "股票名稱", "名稱", "Name", "StockName"),
    )
    close, _ = _find_table_value(
        record,
        ("收盤價", "收盤", "ClosingPrice", "Close"),
    )
    open_price, _ = _find_table_value(
        record,
        ("開盤價", "開盤", "OpeningPrice", "Open"),
    )
    high, _ = _find_table_value(
        record,
        ("最高價", "最高", "HighestPrice", "High"),
    )
    low, _ = _find_table_value(
        record,
        ("最低價", "最低", "LowestPrice", "Low"),
    )
    volume, volume_key = _find_table_value(
        record,
        (
            "成交股數",
            "成交量",
            "成交股數(股)",
            "成交量(股)",
            "成交仟股",
            "成交千股",
            "成交量(千股)",
            "成交量(仟股)",
        ),
    )
    turnover, turnover_key = _find_table_value(
        record,
        (
            "成交金額",
            "成交值",
            "成交金額(元)",
            "成交值(元)",
            "成交仟元",
            "成交千元",
            "成交金額(千元)",
            "成交金額(仟元)",
        ),
    )
    change, _ = _find_table_value(
        record,
        ("漲跌價差", "漲跌", "Change", "ChangeAmount", "PriceChange"),
    )
    change_sign, _ = _find_table_value(
        record,
        (
            "漲跌(+/-)",
            "漲跌符號",
            "漲跌註記",
            "ChangeSign",
            "UpDown",
        ),
    )

    volume_number = _number(volume)
    if volume_key and any(
        unit in _normalised_field_name(volume_key)
        for unit in ("千股", "仟股")
    ):
        volume_number = (volume_number or 0) * 1000
    turnover_number = _number(turnover)
    if turnover_key and any(
        unit in _normalised_field_name(turnover_key)
        for unit in ("千元", "仟元")
    ):
        turnover_number = (turnover_number or 0) * 1000

    canonical = {
        "Code": _plain_text(symbol),
        "Name": _plain_text(name),
        "ClosingPrice": _plain_text(close),
        "OpeningPrice": _plain_text(open_price),
        "HighestPrice": _plain_text(high),
        "LowestPrice": _plain_text(low),
        "TradeVolume": volume_number,
        "TradeValue": turnover_number,
        "Change": _plain_text(change),
        "ChangeSign": _plain_text(change_sign),
        "Date": trade_date,
    }
    return _normalise_official_row(
        canonical,
        market,
        forced_date=trade_date,
        source=source,
    )


def _dataset_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(rows)
    if count == 0:
        return {
            "count": 0,
            "uniqueSymbols": 0,
            "ohlcCoverage": 0.0,
            "liquidityCoverage": 0.0,
        }
    unique_symbols = len({
        str(row.get("symbol"))
        for row in rows
    })
    ohlc_count = sum(
        1
        for row in rows
        if all(
            (_number(row.get(key)) or 0) > 0
            for key in ("open", "high", "low", "close")
        )
    )
    liquidity_count = sum(
        1
        for row in rows
        if (_number(row.get("volume")) or 0) > 0
        and (_number(row.get("turnover")) or 0) > 0
    )
    return {
        "count": count,
        "uniqueSymbols": unique_symbols,
        "ohlcCoverage": round(ohlc_count / count, 4),
        "liquidityCoverage": round(liquidity_count / count, 4),
    }


async def _fetch_primary_market_snapshot(
    market: str,
) -> dict[str, Any]:
    market = market.strip()
    if market == "TWSE":
        url = TWSE_DAILY_URL
        source = "TWSE OpenAPI STOCK_DAY_ALL"
    elif market == "TPEx":
        url = TPEX_DAILY_URL
        # ``daily_bars.source`` is VARCHAR(32).  Keep the human-readable
        # provider label within that database limit.
        source = "TPEx OpenAPI daily_close"
    else:
        raise ValueError("market only supports TWSE or TPEx")

    payload = await _get_json(url)
    raw_rows = _response_rows(payload)
    rows = [
        item
        for row in raw_rows
        if (
            item := _normalise_official_row(
                row,
                market,
                source=source,
            )
        ) is not None
        and item.get("close") is not None
    ]
    report_dates = sorted({
        str(row["date"])
        for row in rows
        if row.get("date")
    })
    if not report_dates:
        report_dates = _payload_trade_dates(payload)
        if len(report_dates) == 1:
            for row in rows:
                row["date"] = report_dates[0]
    if len(report_dates) != 1:
        raise RuntimeError(
            f"{market} 主來源交易日期異常：{report_dates or '無日期'}"
        )
    if not rows:
        raise RuntimeError(f"{market} 主來源沒有解析到普通股")
    return {
        "market": market,
        "date": report_dates[0],
        "rows": rows,
        "source": source,
        "validation": _dataset_quality(rows),
        "fallback": False,
    }


async def _fetch_reference_trade_date() -> str | None:
    """Use one optional Fugle quote to detect two equally stale primary feeds."""
    api_key = (os.getenv("FUGLE_API_KEY") or "").strip()
    if not api_key:
        return None
    symbol = (
        os.getenv("V12_REFERENCE_SYMBOL")
        or "2330"
    ).strip()
    try:
        quote = await _get_json(
            f"{FUGLE_BASE_URL}/intraday/quote/{symbol}",
            headers={"X-API-KEY": api_key},
            timeout=25,
        )
    except Exception:
        return None
    for candidate in (
        quote.get("date") if isinstance(quote, dict) else None,
        (
            quote.get("lastTrade", {}).get("date")
            if isinstance(quote, dict)
            and isinstance(quote.get("lastTrade"), dict)
            else None
        ),
    ):
        normalised = _normalise_trade_date(candidate)
        if normalised:
            return normalised
    updated = quote.get("lastUpdated") if isinstance(quote, dict) else None
    try:
        epoch = float(updated)
        if epoch > 10_000_000_000:
            epoch /= 1000
        return datetime.fromtimestamp(
            epoch,
            TAIPEI_TZ,
        ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


async def _fetch_fallback_market_snapshot(
    market: str,
    target_date: str,
) -> dict[str, Any]:
    market = market.strip()
    normalised_target = _normalise_trade_date(target_date)
    if not normalised_target:
        raise ValueError("備援行情 target_date 格式不正確")
    if market == "TWSE":
        url = TWSE_DAILY_FALLBACK_URL.format(
            date=normalised_target.replace("-", ""),
        )
        source = "TWSE RWD MI_INDEX ALLBUT0999"
        minimum_count = V12_MIN_TWSE_COMMON_STOCKS
    elif market == "TPEx":
        url = TPEX_DAILY_FALLBACK_URL.format(
            date=normalised_target.replace("-", "/"),
        )
        source = "TPEx afterTrading dailyQuotes"
        minimum_count = V12_MIN_TPEX_COMMON_STOCKS
    else:
        raise ValueError("market only supports TWSE or TPEx")

    payload = await _get_json(url)
    if isinstance(payload, dict):
        status = str(
            payload.get("stat")
            or payload.get("status")
            or ""
        ).strip().upper()
        if status and status not in {"OK", "SUCCESS", "200"}:
            raise RuntimeError(
                f"{market} 官方備援端點回傳狀態：{status}"
            )

    payload_dates = _payload_trade_dates(payload)
    if normalised_target not in payload_dates:
        raise RuntimeError(
            f"{market} 備援日期驗證失敗；"
            f"目標={normalised_target}，"
            f"回傳={payload_dates or '無日期'}"
        )

    rows: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for record in _official_table_records(payload):
        item = _fallback_record_to_official_row(
            record,
            market,
            normalised_target,
            source,
        )
        if item is None:
            continue
        symbol = str(item["symbol"])
        if symbol in seen_symbols or item.get("close") is None:
            continue
        seen_symbols.add(symbol)
        rows.append(item)

    quality = _dataset_quality(rows)
    errors: list[str] = []
    if quality["count"] < minimum_count:
        errors.append(
            f"普通股家數 {quality['count']} 低於門檻 {minimum_count}"
        )
    if quality["uniqueSymbols"] != quality["count"]:
        errors.append("股票代號有重複")
    if quality["ohlcCoverage"] < 0.80:
        errors.append(
            f"OHLC完整率僅 {quality['ohlcCoverage']:.1%}"
        )
    if quality["liquidityCoverage"] < 0.75:
        errors.append(
            "成交量／成交值完整率僅 "
            f"{quality['liquidityCoverage']:.1%}"
        )
    if errors:
        raise RuntimeError(
            f"{market} 官方備援資料未通過安全檢查："
            + "；".join(errors)
        )

    return {
        "market": market,
        "date": normalised_target,
        "rows": rows,
        "source": source,
        "validation": quality,
        "fallback": True,
        "fallbackTargetDate": normalised_target,
    }


async def fetch_official_daily_snapshot_with_fallback() -> dict[str, Any]:
    """Resolve TWSE/TPEx to one trade date before allowing database writes."""
    markets = ("TWSE", "TPEx")
    fallback_enabled = _official_fallback_enabled()
    primary_results = await asyncio.gather(
        *(_fetch_primary_market_snapshot(market) for market in markets),
        return_exceptions=True,
    )
    primary_by_market: dict[str, dict[str, Any]] = {}
    primary_errors: dict[str, str] = {}
    for market, result in zip(markets, primary_results):
        if isinstance(result, BaseException):
            primary_errors[market] = (
                f"{type(result).__name__}: {result}"
            )
        else:
            primary_by_market[market] = result

    primary_dates = {
        market: (
            primary_by_market.get(market, {}).get("date")
        )
        for market in markets
    }
    reference_date = await _fetch_reference_trade_date()
    target_candidates = [
        str(value)
        for value in (
            *primary_dates.values(),
            reference_date,
        )
        if value
    ]
    target_date = max(target_candidates) if target_candidates else None

    final_by_market = dict(primary_by_market)
    fallback_attempts: list[dict[str, Any]] = []
    fallback_markets: list[str] = []
    if target_date and fallback_enabled:
        for market in markets:
            if primary_dates.get(market) == target_date:
                continue
            attempt: dict[str, Any] = {
                "market": market,
                "primaryDate": primary_dates.get(market),
                "targetDate": target_date,
                "used": False,
            }
            try:
                fallback = await _fetch_fallback_market_snapshot(
                    market,
                    target_date,
                )
                final_by_market[market] = fallback
                fallback_markets.append(market)
                attempt.update({
                    "used": True,
                    "source": fallback["source"],
                    "date": fallback["date"],
                    "validation": fallback["validation"],
                })
            except Exception as exc:
                attempt["error"] = f"{type(exc).__name__}: {exc}"
            fallback_attempts.append(attempt)

    final_dates = {
        market: final_by_market.get(market, {}).get("date")
        for market in markets
    }
    unique_final_dates = {
        str(value)
        for value in final_dates.values()
        if value
    }
    all_markets_present = all(
        market in final_by_market
        for market in markets
    )
    all_markets_same_date = (
        all_markets_present
        and len(unique_final_dates) == 1
    )
    matches_reference_date = (
        reference_date is None
        or unique_final_dates == {reference_date}
    )
    ok = all_markets_same_date and matches_reference_date

    if not all_markets_present:
        error_code = "OFFICIAL_DATA_UNAVAILABLE"
    elif not all_markets_same_date:
        error_code = "MARKET_DATE_MISMATCH"
    elif not matches_reference_date:
        error_code = "OFFICIAL_DATA_NOT_LATEST"
    else:
        error_code = None

    rows = [
        row
        for market in markets
        for row in final_by_market.get(market, {}).get("rows", [])
    ] if ok else []
    return {
        "ok": ok,
        "errorCode": error_code,
        "error": (
            None
            if ok
            else "上市與上櫃官方資料未能安全對齊同一交易日，已拒絕寫入。"
        ),
        "rows": rows,
        "primaryMarketDates": primary_dates,
        "finalMarketDates": final_dates,
        "referenceDate": reference_date,
        "targetDate": target_date,
        "fallbackEnabled": fallback_enabled,
        "fallbackUsed": bool(fallback_markets),
        "fallbackMarkets": fallback_markets,
        "fallbackAttempts": fallback_attempts,
        "primaryErrors": primary_errors,
        "dataIntegrity": {
            "allMarketsPresent": all_markets_present,
            "allMarketsSameDate": all_markets_same_date,
            "matchesReferenceDate": matches_reference_date,
            "rowCounts": {
                market: len(
                    final_by_market.get(market, {}).get("rows", [])
                )
                for market in markets
            },
        },
    }


async def fetch_official_daily_snapshot() -> list[dict[str, Any]]:
    """Compatibility wrapper returning only a validated same-date snapshot."""
    result = await fetch_official_daily_snapshot_with_fallback()
    if not result["ok"]:
        raise RuntimeError(
            f"{result['errorCode']}: {result['error']}"
        )
    return list(result["rows"])
