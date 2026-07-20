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

    async def calculate_symbol_indicators(self, symbol: str) -> dict[str, Any]:
        bars = await self.repository.get_daily_bars(symbol, limit=5000)
        calculated = calculate_indicators(bars)
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
        return {"ok": True, "symbol": symbol, "processed": count}

    async def cleanup(
        self,
        retention_years: int = 3,
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
