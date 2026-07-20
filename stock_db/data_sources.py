"""Market-data sources for V10.5.

Historical backfill uses FinMind TaiwanStockPrice when FINMIND_TOKEN is
available. Daily whole-market updates use official TWSE/TPEx OpenAPI data.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_DAILY_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_SECURITIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_SECURITIES_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "---"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    value = _number(value)
    return int(value) if value is not None else None


async def _get_json(url: str, *, params: dict[str, Any] | None = None,
                    headers: dict[str, str] | None = None,
                    timeout: float = 45) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, params=params, headers=headers)
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


async def fetch_official_daily_snapshot() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    today = date.today().isoformat()
    for market, url in (("TWSE", TWSE_DAILY_URL), ("TPEx", TPEX_DAILY_URL)):
        rows = await _get_json(url)
        for row in rows if isinstance(rows, list) else []:
            symbol = str(_pick(
                row, "Code", "SecuritiesCompanyCode", "股票代號", "SecuritiesCompanyCode"
            ) or "").strip()
            if not symbol:
                continue
            output.append({
                "symbol": symbol,
                "name": str(_pick(row, "Name", "CompanyName", "股票名稱") or symbol),
                "market": market,
                "date": str(_pick(row, "Date", "TradeDate", "日期") or today).replace("/", "-"),
                "open": _number(_pick(row, "OpeningPrice", "Open", "開盤價")),
                "high": _number(_pick(row, "HighestPrice", "High", "最高價")),
                "low": _number(_pick(row, "LowestPrice", "Low", "最低價")),
                "close": _number(_pick(row, "ClosingPrice", "Close", "收盤價")),
                "volume": _integer(_pick(row, "TradeVolume", "TradingShares", "成交股數")),
                "turnover": _integer(_pick(row, "TradeValue", "TransactionAmount", "成交金額")),
                "change_percent": _number(_pick(row, "Change", "漲跌價差")),
                "source": f"{market} OpenAPI",
            })
    return output
