"""MCP tools for the PostgreSQL stock database (V11 final)."""

from __future__ import annotations

from typing import Any

from stock_db.maintenance import run_daily_maintenance
from stock_db.performance import (
    DEFAULT_PERFORMANCE_UPDATE_LIMIT,
    execution_performance_summary,
    performance_summary,
    update_signal_execution_performance,
    update_signal_performance,
    weekly_performance_report,
)
from stock_db.pipeline import (
    backfill_all_market,
    backfill_symbols,
    calculate_all_indicators,
    sync_security_master,
    update_official_daily,
)
from stock_db.radar import (
    run_full_bullish_radar,
    run_full_bullish_radar_v12 as run_full_bullish_radar_v12_core,
    screen_database_market,
    screen_database_market_v12,
)
from stock_db.v12 import (
    V12_STRATEGIES,
    load_v12_config,
    validate_v12_candidates,
)
from stock_db.service import stock_database_service


async def validate_v12_release_core(
    limit_each: int = 5,
    minimum_score: float = 0,
) -> dict:
    """Validate V12 from one shared market snapshot.

    ``run_full_bullish_radar_v12_core`` already evaluates and persists every
    individual strategy before it writes the combined run.  Re-running the
    four strategies here used to execute the expensive market snapshot query
    five times and could exceed the free-tier statement timeout.  Reuse the
    per-strategy results returned by the full radar instead.
    """
    health = await stock_database_service.health()
    before = await stock_database_service.statistics()
    full = await run_full_bullish_radar_v12_core(
        limit_each=limit_each,
        minimum_score=minimum_score,
        save_result=True,
    )
    strategies = dict(full.get("byStrategy") or {})
    after = await stock_database_service.statistics()
    before_stats = before.get("statistics", {})
    after_stats = after.get("statistics", {})
    total_candidates = sum(
        int(result.get("candidateCount", 0))
        for result in strategies.values()
    )
    semantic_issues: list[dict[str, Any]] = []
    for strategy, result in strategies.items():
        semantic_issues.extend(
            validate_v12_candidates(
                result.get("results") or [],
                context=f"strategies.{strategy}.results",
            )
        )
    semantic_issues.extend(
        validate_v12_candidates(
            full.get("top10") or [],
            context="fullRadar.top10",
        )
    )
    expected_strategies = set(V12_STRATEGIES)
    checks = {
        "databaseHealthy": health.get("status") == "healthy",
        "allStrategiesOk": (
            expected_strategies.issubset(strategies)
            and all(
                strategies[strategy].get("ok")
                for strategy in expected_strategies
            )
        ),
        "fullRadarOk": bool(full.get("ok")),
        "accuracyEngineLoaded": (
            full.get("accuracyEngine") == "V12.1_FORWARD_BULLISH"
        ),
        "radarRunsWritten": int(after_stats.get("radar_runs", 0))
            > int(before_stats.get("radar_runs", 0)),
        "radarCandidatesWritten": (
            total_candidates == 0
            or int(after_stats.get("radar_candidates", 0))
                > int(before_stats.get("radar_candidates", 0))
        ),
        "semanticConsistency": not semantic_issues,
    }
    return {
        "ok": all(checks.values()),
        "releaseReady": all(checks.values()),
        "version": "V12",
        "accuracyEngine": full.get("accuracyEngine"),
        "snapshotScanCount": 1,
        "checks": checks,
        "candidateCount": total_candidates,
        "semanticValidation": {
            "ok": not semantic_issues,
            "issueCount": len(semantic_issues),
            "issues": semantic_issues,
        },
        "strategies": strategies,
        "fullRadar": full,
        "statisticsBefore": before_stats,
        "statisticsAfter": after_stats,
    }


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
        batch_size: int = 500,
        start_after: str | None = None,
        concurrency: int = 6,
    ) -> dict:
        """更新官方當日日K，以最多500檔批次計算最新指標。"""
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
    async def screen_market_v12(
        strategy: str = "reversal_reclaim",
        limit: int = 30,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """V12全市場雷達：V7流動性、V11三策略、反轉收復與ATR交易計畫。"""
        return await screen_database_market_v12(
            strategy=strategy,
            limit=limit,
            minimum_score=minimum_score,
            save_result=save_result,
        )

    @mcp.tool()
    async def run_full_bullish_radar_v12(
        limit_each: int = 20,
        minimum_score: float = 45,
        save_result: bool = True,
    ) -> dict:
        """執行V12四種看漲策略，輸出激進低接、確認買點、分批比例與失敗條件。"""
        return await run_full_bullish_radar_v12_core(
            limit_each=limit_each,
            minimum_score=minimum_score,
            save_result=save_result,
        )

    @mcp.tool()
    async def get_v12_radar_config() -> dict:
        """查看目前V12流動性、反轉收復、ATR與過熱扣分設定。"""
        config = load_v12_config()
        return {
            "ok": True,
            "version": "V12",
            "strategies": list(V12_STRATEGIES),
            "config": config.public_dict(),
        }

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
    async def update_radar_signal_performance(
        limit: int = DEFAULT_PERFORMANCE_UPDATE_LIMIT,
    ) -> dict:
        """優先更新新訊號，再更新最久未計算的1/3/5/10/20日績效。"""
        return await update_signal_performance(limit)

    @mcp.tool()
    async def update_v12_execution_performance(
        limit: int = DEFAULT_PERFORMANCE_UPDATE_LIMIT,
        entry_window_sessions: int = 3,
    ) -> dict:
        """依激進低接、確認買點、分批及收盤失敗條件更新真實可執行績效。"""
        return await update_signal_execution_performance(
            limit=limit,
            entry_window_sessions=entry_window_sessions,
        )

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
        batch_size: int = 500,
        start_after: str | None = None,
        concurrency: int = 6,
        radar_limit_each: int = 20,
        radar_minimum_score: float = 45,
    ) -> dict:
        """500檔批次每日更新；重複傳入 nextStartAfter 直到 completed。"""
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
        batch_size: int = 500,
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
    async def get_v12_execution_performance_summary(
        strategy: str | None = None,
    ) -> dict:
        """只統計真正觸及V12雙買點的訊號，不把未成交當成虧損。"""
        return await execution_performance_summary(strategy)
    @mcp.tool()
    async def get_radar_weekly_report(
        start_date: str | None = None,
        end_date: str | None = None,
        version: str = "V12",
        top_n: int = 10,
    ) -> dict:
        """依日期與雷達版本產生週報，空值不計失敗且同日同股去重。"""
        return await weekly_performance_report(
            start_date=start_date,
            end_date=end_date,
            version=version,
            top_n=top_n,
        )

    @mcp.tool()
    async def validate_v12_release(
        limit_each: int = 5,
        minimum_score: float = 0,
    ) -> dict:
        """單次快照驗證資料庫、V12四策略、合併雷達及寫入功能。"""
        return await validate_v12_release_core(limit_each, minimum_score)

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
