"""High-level V10 database operations used by scripts and MCP tools."""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from .connection import StockDatabase, stock_database
from .indicators import calculate_indicators
from .models import DailyIndicator
from .repository import StockRepository, stock_repository


class StockDatabaseService:
    def __init__(
        self, database: StockDatabase | None = None,
        repository: StockRepository | None = None
    ) -> None:
        self.database = database or stock_database
        self.repository = repository or stock_repository

    async def initialize(self) -> dict[str, Any]:
        if not self.database.config.enabled:
            return {
                "ok": False, "status": "disabled",
                "message": "STOCK_DB_ENABLED=false；尚未啟用 PostgreSQL。",
            }
        health = await self.database.health()
        if health.get("status") != "healthy":
            return {"ok": False, **health}
        schema = await self.repository.initialize_schema()
        return {"ok": True, "health": health, **schema}

    async def health(self) -> dict[str, Any]:
        return await self.database.health()

    async def statistics(self) -> dict[str, Any]:
        health = await self.database.health()
        if health.get("status") != "healthy":
            return {"ok": False, "health": health}
        return {"ok": True, "health": health,
                "statistics": await self.repository.statistics()}

    async def calculate_symbol_indicators(
        self,
        symbol: str,
        latest_only: bool = False,
    ) -> dict[str, Any]:
        bars = await self.repository.get_daily_bars(symbol, limit=5000)
        calculated = calculate_indicators(bars)
        if latest_only and calculated:
            calculated = calculated[-1:]
        rows = [
            DailyIndicator(
                symbol=item["symbol"],
                trade_date=item["trade_date"],
                values={k: v for k, v in item.items()
                        if k not in {"symbol", "trade_date"}},
            )
            for item in calculated
        ]
        count = await self.repository.bulk_upsert_indicators(rows)
        return {
            "ok": True,
            "symbol": symbol,
            "processed": count,
            "latestOnly": latest_only,
        }

    async def calculate_latest_indicators_bulk(
        self,
        symbols: Sequence[str],
        lookback_bars: int = 61,
    ) -> dict[str, Any]:
        """Calculate and write one latest indicator row per symbol in bulk.

        This keeps the existing Python indicator formulas, but avoids fetching
        thousands of historical bars and opening one write transaction for
        every symbol.
        """
        unique_symbols = list(dict.fromkeys(
            str(symbol).strip() for symbol in symbols if str(symbol).strip()
        ))
        if not unique_symbols:
            return {
                "ok": True,
                "requestedSymbols": 0,
                "processedSymbols": 0,
                "failedSymbols": 0,
                "indicatorRowsWritten": 0,
                "failures": [],
                "lookbackBars": lookback_bars,
            }

        bars_by_symbol = (
            await self.repository.get_recent_daily_bars_for_symbols(
                unique_symbols,
                limit_per_symbol=lookback_bars,
            )
        )
        rows: list[DailyIndicator] = []
        failures: list[dict[str, str]] = []

        for symbol in unique_symbols:
            try:
                bars = bars_by_symbol.get(symbol) or []
                if not bars:
                    raise ValueError("no daily bars")
                latest = calculate_indicators(bars)[-1]
                rows.append(DailyIndicator(
                    symbol=latest["symbol"],
                    trade_date=latest["trade_date"],
                    values={
                        key: value
                        for key, value in latest.items()
                        if key not in {"symbol", "trade_date"}
                    },
                ))
            except Exception as exc:
                failures.append({
                    "symbol": symbol,
                    "error": f"{type(exc).__name__}: {exc}",
                })

        written = await self.repository.bulk_upsert_indicators(rows)
        return {
            "ok": not failures,
            "requestedSymbols": len(unique_symbols),
            "processedSymbols": len(rows),
            "failedSymbols": len(failures),
            "indicatorRowsWritten": written,
            "failures": failures[:100],
            "lookbackBars": lookback_bars,
        }

    async def cleanup(
        self,
        retention_years: int = 5,
        radar_retention_days: int = 180,
        job_retention_days: int = 90,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        health = await self.database.health()
        if health.get("status") != "healthy":
            return {"ok": False, "health": health}
        result = await self.repository.cleanup_old_data(
            retention_years=retention_years,
            radar_retention_days=radar_retention_days,
            job_retention_days=job_retention_days,
            vacuum=vacuum,
        )
        return {"health": health, **result}

    async def save_radar_result(
        self, strategy: str, candidates: Sequence[dict[str, Any]],
        run_date: date | None = None,
        configuration: dict[str, Any] | None = None,
        universe_count: int = 0,
    ) -> dict[str, Any]:
        run_id = await self.repository.save_radar_run(
            strategy=strategy,
            run_date=run_date or date.today(),
            candidates=candidates,
            configuration=configuration,
            universe_count=universe_count,
        )
        return {"ok": True, "radarRunId": run_id,
                "candidateCount": len(candidates)}


stock_database_service = StockDatabaseService()
