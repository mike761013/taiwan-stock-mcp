"""MCP tools for the PostgreSQL stock database (V11 milestone 1)."""

from __future__ import annotations

from typing import Any

from stock_db.maintenance import run_daily_maintenance
from stock_db.performance import performance_summary, update_signal_performance
from stock_db.pipeline import (
    backfill_all_market,
    backfill_symbols,
    calculate_all_indicators,
    sync_security_master,
    update_official_daily,
)
from stock_db.radar import run_full_bullish_radar, screen_database_market
from stock_db.service import stock_database_service


def register_v10_tools(mcp: Any) -> None:
    """Keep the existing registration name so current server imports do not break."""

    @mcp.tool()
    async def initialize_stock_database() -> dict:
        """初始化資料庫與索引，可安全重複執行。"""
        return await stock_database_service.initialize()

    @mcp.tool()
    async def get_stock_database_health() -> dict:
        """資料庫健康檢查。"""
        return await stock_database_service.health()

    @mcp.tool()
    async def get_stock_database_statistics() -> dict:
        """資料庫筆數、日期、容量與剩餘空間。"""
        return await stock_database_service.statistics()

    @mcp.tool()
    async def sync_stock_security_master() -> dict:
        """同步上市櫃股票基本清單。"""
        return await sync_security_master()

    @mcp.tool()
    async def backfill_stock_history(
        symbols: str | None = None,
        years: int = 3,
        concurrency: int = 3,
        batch_size: int = 20,
        start_after: str | None = None,
    ) -> dict:
        """
        回補歷史日K。

        有 symbols 時只處理指定股票，例如 2330,2313,4977。
        未提供 symbols 時，每次只處理一個小批次並回傳 nextStartAfter，
        避免免費 Web Service 因長時間請求而出現 504。
        """
        if symbols:
            parsed = [item.strip() for item in symbols.split(",") if item.strip()]
            return await backfill_symbols(parsed, years, concurrency)
        return await backfill_all_market(
            years=years,
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )

    @mcp.tool()
    async def backfill_all_stock_history(
        years: int = 3,
        batch_size: int = 20,
        start_after: str | None = None,
        concurrency: int = 3,
    ) -> dict:
        """
        分批回補全市場歷史日K。

        每次只跑 batch_size 檔；若 hasMore=true，下一次把
        nextStartAfter 傳入 start_after 即可續傳。
        """
        return await backfill_all_market(
            years=years,
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )

    @mcp.tool()
    async def calculate_stock_indicators(
        symbols: str | None = None,
        batch_size: int = 20,
        start_after: str | None = None,
        concurrency: int = 3,
    ) -> dict:
        """
        重新計算技術指標。

        有 symbols 時處理指定股票；未提供時分批處理全市場，
        並用 nextStartAfter 續傳。
        """
        return await calculate_all_indicators(
            symbols=symbols,
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )

    @mcp.tool()
    async def update_stock_database_daily() -> dict:
        """以官方 TWSE/TPEx OpenAPI 更新當日日K與指標。"""
        return await update_official_daily()

    @mcp.tool()
    async def cleanup_stock_database(
        retention_years: int = 3,
        radar_retention_days: int = 180,
        job_retention_days: int = 90,
        vacuum: bool = True,
    ) -> dict:
        """刪除過期資料，並視需要執行 VACUUM ANALYZE。"""
        return await stock_database_service.cleanup(
            retention_years=retention_years,
            radar_retention_days=radar_retention_days,
            job_retention_days=job_retention_days,
            vacuum=vacuum,
        )

    @mcp.tool()
    async def screen_market_v10(
        strategy: str = "early_stage",
        limit: int = 30,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """PostgreSQL 優先的全市場雷達。"""
        return await screen_database_market(
            strategy, limit, minimum_score, save_result
        )

    @mcp.tool()
    async def run_full_bullish_radar_v10(limit_each: int = 20) -> dict:
        """一次執行 early_stage、breakout、pullback 並合併排名。"""
        return await run_full_bullish_radar(limit_each)

    @mcp.tool()
    async def update_radar_signal_performance(limit: int = 500) -> dict:
        """更新雷達訊號 1/3/5/10/20 日績效。"""
        return await update_signal_performance(limit)

    @mcp.tool()
    async def run_v10_daily_maintenance(
        run_radar: bool = True,
        update_performance: bool = True,
    ) -> dict:
        """手動執行每日更新、雷達與績效；不需要 Background Worker。"""
        return await run_daily_maintenance(run_radar, update_performance)

    @mcp.tool()
    async def get_radar_performance_summary(
        strategy: str | None = None,
    ) -> dict:
        """查詢雷達策略績效摘要。"""
        return await performance_summary(strategy)
