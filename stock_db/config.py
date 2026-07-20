"""Environment configuration for the optional V10 PostgreSQL subsystem."""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return default


def parse_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


@dataclass(frozen=True)
class StockDatabaseConfig:
    database_url: str | None
    enabled: bool
    read_preferred: bool
    daily_update: bool
    fallback_enabled: bool
    history_years: int
    pool_min: int
    pool_max: int
    statement_timeout_seconds: int

    @property
    def can_connect(self) -> bool:
        return self.enabled and bool(self.database_url)


def load_config() -> StockDatabaseConfig:
    pool_min = parse_int(os.getenv("STOCK_DB_POOL_MIN"), 1, 1, 5)
    pool_max = parse_int(os.getenv("STOCK_DB_POOL_MAX"), 3, pool_min, 10)
    return StockDatabaseConfig(
        database_url=(os.getenv("DATABASE_URL") or "").strip() or None,
        enabled=parse_bool(os.getenv("STOCK_DB_ENABLED"), False),
        read_preferred=parse_bool(os.getenv("STOCK_DB_READ_PREFERRED"), False),
        daily_update=parse_bool(os.getenv("STOCK_DB_DAILY_UPDATE"), False),
        fallback_enabled=parse_bool(os.getenv("STOCK_DB_FALLBACK_ENABLED"), True),
        history_years=parse_int(os.getenv("STOCK_DB_HISTORY_YEARS"), 3, 1, 10),
        pool_min=pool_min,
        pool_max=pool_max,
        statement_timeout_seconds=parse_int(
            os.getenv("STOCK_DB_STATEMENT_TIMEOUT_SECONDS"), 30, 5, 120
        ),
    )
