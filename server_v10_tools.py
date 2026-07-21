"""MCP tools for the PostgreSQL stock database (V11 final)."""

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
    """Keep the existing registration function so current server imports work."""

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
        """回補指定股票，或以 nextStartAfter 分批回補全市場。"""
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
        """分批回補全市場歷史日K，支援續傳。"""
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
        """重新計算指定股票，或分批重算全市場技術指標。"""
        return await calculate_all_indicators(
            symbols=symbols,
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )

    @mcp.tool()
    async def update_stock_database_daily(
        batch_size: int = 50,
        start_after: str | None = None,
        concurrency: int = 6,
    ) -> dict:
        """更新官方當日日K，並分批計算最新指標以避免免費版逾時。"""
        return await update_official_daily(
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )

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

    async def _screen(
        strategy: str,
        limit: int,
        minimum_score: float,
        save_result: bool,
    ) -> dict:
        return await screen_database_market(
            strategy=strategy,
            limit=limit,
            minimum_score=minimum_score,
            save_result=save_result,
        )

    @mcp.tool()
    async def screen_market_v11(
        strategy: str = "early_stage",
        limit: int = 30,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """V11 PostgreSQL 全市場雷達。"""
        return await _screen(strategy, limit, minimum_score, save_result)

    @mcp.tool()
    async def screen_market_v10(
        strategy: str = "early_stage",
        limit: int = 30,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """舊名稱相容入口；功能與 screen_market_v11 相同。"""
        return await _screen(strategy, limit, minimum_score, save_result)

    @mcp.tool()
    async def run_full_bullish_radar_v11(
        limit_each: int = 20,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """執行三種 V11 多頭策略並合併排名。"""
        return await run_full_bullish_radar(
            limit_each=limit_each,
            minimum_score=minimum_score,
            save_result=save_result,
        )

    @mcp.tool()
    async def run_full_bullish_radar_v10(
        limit_each: int = 20,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """舊名稱相容入口；功能與 V11 雷達相同。"""
        return await run_full_bullish_radar(
            limit_each=limit_each,
            minimum_score=minimum_score,
            save_result=save_result,
        )

    @mcp.tool()
    async def update_radar_signal_performance(limit: int = 500) -> dict:
        """更新雷達訊號 1/3/5/10/20 日績效。"""
        return await update_signal_performance(limit)

    async def _maintenance(
        run_radar: bool,
        update_performance: bool,
        batch_size: int,
        start_after: str | None,
        concurrency: int,
        radar_limit_each: int,
        radar_minimum_score: float,
    ) -> dict:
        return await run_daily_maintenance(
            run_radar=run_radar,
            update_performance=update_performance,
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
            radar_limit_each=radar_limit_each,
            radar_minimum_score=radar_minimum_score,
        )

    @mcp.tool()
    async def run_v11_daily_maintenance(
        run_radar: bool = True,
        update_performance: bool = True,
        batch_size: int = 50,
        start_after: str | None = None,
        concurrency: int = 6,
        radar_limit_each: int = 20,
        radar_minimum_score: float = 45,
    ) -> dict:
        """免費版可續傳的每日更新；重複傳入 nextStartAfter 直到 completed。"""
        return await _maintenance(
            run_radar,
            update_performance,
            batch_size,
            start_after,
            concurrency,
            radar_limit_each,
            radar_minimum_score,
        )

    @mcp.tool()
    async def run_v10_daily_maintenance(
        run_radar: bool = True,
        update_performance: bool = True,
        batch_size: int = 50,
        start_after: str | None = None,
        concurrency: int = 6,
        radar_limit_each: int = 20,
        radar_minimum_score: float = 45,
    ) -> dict:
        """舊名稱相容入口；功能與 V11 每日維護相同。"""
        return await _maintenance(
            run_radar,
            update_performance,
            batch_size,
            start_after,
            concurrency,
            radar_limit_each,
            radar_minimum_score,
        )

    @mcp.tool()
    async def get_radar_performance_summary(
        strategy: str | None = None,
    ) -> dict:
        """查詢雷達策略績效摘要。"""
        return await performance_summary(strategy)

    @mcp.tool()
    async def validate_v11_release(
        limit_each: int = 5,
        minimum_score: float = 0,
    ) -> dict:
        """一次驗證資料庫、三策略雷達及雷達寫入是否可用。"""
        health = await stock_database_service.health()
        before = await stock_database_service.statistics()
        strategies: dict[str, dict] = {}
        for strategy in ("early_stage", "breakout", "pullback"):
            strategies[strategy] = await screen_database_market(
                strategy=strategy,
                limit=limit_each,
                minimum_score=minimum_score,
                save_result=True,
            )
        full = await run_full_bullish_radar(
            limit_each=limit_each,
            minimum_score=minimum_score,
            save_result=True,
        )
        after = await stock_database_service.statistics()

        before_stats = before.get("statistics", {})
        after_stats = after.get("statistics", {})
        total_candidates = sum(
            int(result.get("candidateCount", 0))
            for result in strategies.values()
        )
        checks = {
            "databaseHealthy": health.get("status") == "healthy",
            "allStrategiesOk": all(
                result.get("ok") for result in strategies.values()
            ),
            "radarRunsWritten": int(after_stats.get("radar_runs", 0))
                > int(before_stats.get("radar_runs", 0)),
            "radarCandidatesWritten": (
                total_candidates == 0
                or int(after_stats.get("radar_candidates", 0))
                    > int(before_stats.get("radar_candidates", 0))
            ),
            "fullRadarOk": bool(full.get("ok")),
        }
        return {
            "ok": all(checks.values()),
            "releaseReady": all(checks.values()),
            "checks": checks,
            "candidateCount": total_candidates,
            "strategies": strategies,
            "fullRadar": full,
            "statisticsBefore": before_stats,
            "statisticsAfter": after_stats,
        }
