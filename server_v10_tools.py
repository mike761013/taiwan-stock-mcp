"""MCP tools for V10.5."""

from __future__ import annotations

from datetime import date
from typing import Any

from stock_db.performance import performance_summary, update_signal_performance
from stock_db.maintenance import run_daily_maintenance
from stock_db.pipeline import (
    backfill_all_market,
    backfill_symbols,
    sync_security_master,
    update_official_daily,
)
from stock_db.radar import run_full_bullish_radar, screen_database_market
from stock_db.service import stock_database_service


def register_v10_tools(mcp: Any) -> None:
    @mcp.tool()
    async def initialize_stock_database() -> dict:
        """V10.5：初始化資料庫與索引，可安全重複執行。"""
        return await stock_database_service.initialize()

    @mcp.tool()
    async def get_stock_database_health() -> dict:
        """V10.5：資料庫健康檢查。"""
        return await stock_database_service.health()

    @mcp.tool()
    async def get_stock_database_statistics() -> dict:
        """V10.5：資料庫筆數、日期與容量。"""
        return await stock_database_service.statistics()

    @mcp.tool()
    async def sync_stock_security_master() -> dict:
        """V10.5：同步上市櫃股票基本清單。"""
        return await sync_security_master()

    @mcp.tool()
    async def backfill_stock_history(
        symbols: str, years: int = 3, concurrency: int = 3
    ) -> dict:
        """V10.5：回補指定股票歷史日K。symbols 例：2330,2313,4977"""
        parsed = [item.strip() for item in symbols.split(",") if item.strip()]
        return await backfill_symbols(parsed, years, concurrency)

    @mcp.tool()
    async def backfill_all_stock_history(
        years: int = 3, batch_size: int = 50, start_after: str | None = None
    ) -> dict:
        """V10.5：回補全市場歷史日K，支援 start_after 續傳。"""
        return await backfill_all_market(years, batch_size, start_after)

    @mcp.tool()
    async def update_stock_database_daily() -> dict:
        """V10.5：以官方 TWSE/TPEx OpenAPI 更新當日日K與指標。"""
        return await update_official_daily()

    @mcp.tool()
    async def screen_market_v10(
        strategy: str = "early_stage", limit: int = 30,
        minimum_score: float = 45, save_result: bool = True
    ) -> dict:
        """V10.5：PostgreSQL 優先的全市場雷達。"""
        return await screen_database_market(
            strategy, limit, minimum_score, save_result
        )

    @mcp.tool()
    async def run_full_bullish_radar_v10(limit_each: int = 20) -> dict:
        """V10.5：一次執行 early_stage、breakout、pullback 並合併排名。"""
        return await run_full_bullish_radar(limit_each)

    @mcp.tool()
    async def update_radar_signal_performance(limit: int = 500) -> dict:
        """V10.5：更新雷達訊號 1/3/5/10/20 日績效。"""
        return await update_signal_performance(limit)


    @mcp.tool()
    async def run_v10_daily_maintenance(
        run_radar: bool = True,
        update_performance: bool = True,
    ) -> dict:
        """V10.5：手動執行每日更新、雷達與績效；不需要 Background Worker。"""
        return await run_daily_maintenance(run_radar, update_performance)

    @mcp.tool()
    async def get_radar_performance_summary(
        strategy: str | None = None
    ) -> dict:
        """V10.5：查詢雷達策略績效摘要。"""
        return await performance_summary(strategy)
