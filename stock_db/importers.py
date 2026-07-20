"""CSV/JSON import helpers for historical daily bars."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import DailyBar, Security


def parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    return datetime.fromisoformat(text[:10]).date()


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "---"):
        return None
    return float(str(value).replace(",", ""))


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def row_to_daily_bar(row: dict[str, Any], default_source: str = "import") -> DailyBar:
    symbol = str(row.get("symbol") or row.get("stockNo") or row.get("code") or "").strip()
    if not symbol:
        raise ValueError("Missing symbol")
    return DailyBar(
        symbol=symbol,
        trade_date=parse_date(row.get("trade_date") or row.get("date")),
        open=_number(row.get("open")),
        high=_number(row.get("high")),
        low=_number(row.get("low")),
        close=_number(row.get("close")),
        volume=_integer(row.get("volume")),
        turnover=_integer(row.get("turnover") or row.get("amount")),
        change_percent=_number(row.get("change_percent") or row.get("changePercent")),
        source=str(row.get("source") or default_source),
    )


def load_rows(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("data") or data.get("rows") or []
        if not isinstance(data, list):
            raise ValueError("JSON must contain an array")
        return [dict(item) for item in data]
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            return list(csv.DictReader(file))
    raise ValueError("Only .json and .csv are supported")
