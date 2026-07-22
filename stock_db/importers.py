"""CSV/JSON import helpers for historical daily bars."""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import DailyBar, Security


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        raise ValueError("Missing date")

    # TPEx may return Republic of China calendar dates such as ``1150721``
    # or ``115/07/21``.  Convert those to Gregorian dates before falling back
    # to the ISO parser used by TWSE and FinMind.
    digits = re.sub(r"\D", "", text)
    if len(digits) == 7:
        roc_year = int(digits[:3])
        return date(roc_year + 1911, int(digits[3:5]), int(digits[5:7]))
    if len(digits) == 8 and text[:4].isdigit():
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))

    normalized = text.replace("/", "-")
    return datetime.fromisoformat(normalized[:10]).date()


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
