"""MCP tool registration for the V10 PostgreSQL subsystem.

Import this module after the `mcp = FastMCP(...)` object exists.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from stock_db.service import stock_database_service


def register_v10_tools(mcp: Any) -> None:
    @mcp.tool()
    async def initialize_stock_database() -> dict:
        """V10：初始化 PostgreSQL schema；可安全重複執行，不刪除資料。"""
        return await stock_database_service.initialize()

    @mcp.tool()
    async def get_stock_database_health() -> dict:
        """V10：檢查 PostgreSQL 是否啟用、可連線及版本。"""
        return await stock_database_service.health()

    @mcp.tool()
    async def get_stock_database_statistics() -> dict:
        """V10：查看資料庫筆數、日期範圍及容量。"""
        return await stock_database_service.statistics()

    @mcp.tool()
    async def calculate_stock_indicators_to_database(symbol: str) -> dict:
        """V10：依資料庫日K計算並保存指定股票的技術指標。"""
        return await stock_database_service.calculate_symbol_indicators(
            symbol.strip().upper()
        )

    @mcp.tool()
    async def save_radar_result_to_database(
        strategy: str,
        candidates: list[dict],
        universe_count: int = 0,
        run_date: str | None = None,
    ) -> dict:
        """V10：保存一次雷達候選股、排名、分數與完整快照。"""
        parsed_date = date.fromisoformat(run_date) if run_date else None
        return await stock_database_service.save_radar_result(
            strategy=strategy,
            candidates=candidates,
            run_date=parsed_date,
            universe_count=universe_count,
        )
