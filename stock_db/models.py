"""Typed input models used by the repository layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    market: str
    industry: str | None = None
    is_active: bool = True


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    trade_date: date
    open: Decimal | float | int | None
    high: Decimal | float | int | None
    low: Decimal | float | int | None
    close: Decimal | float | int | None
    volume: int | None
    turnover: int | None = None
    change_percent: Decimal | float | int | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class DailyIndicator:
    symbol: str
    trade_date: date
    values: dict[str, Any]
