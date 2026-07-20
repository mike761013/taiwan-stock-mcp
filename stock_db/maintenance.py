"""Manual V10.5 maintenance orchestration.

This project does not require a Render Background Worker. Run this workflow
manually through MCP, Render Shell, or an optional Render Cron Job.
"""

from __future__ import annotations

from typing import Any

from .performance import update_signal_performance
from .pipeline import update_official_daily
from .radar import run_full_bullish_radar


async def run_daily_maintenance(
    run_radar: bool = True,
    update_performance: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True}

    try:
        result["marketUpdate"] = await update_official_daily()
    except Exception as exc:
        return {
            "ok": False,
            "stage": "market_update",
            "error": f"{type(exc).__name__}: {exc}",
        }

    if run_radar:
        try:
            result["radar"] = await run_full_bullish_radar()
        except Exception as exc:
            result["ok"] = False
            result["radarError"] = f"{type(exc).__name__}: {exc}"

    if update_performance:
        try:
            result["performance"] = await update_signal_performance()
        except Exception as exc:
            result["ok"] = False
            result["performanceError"] = f"{type(exc).__name__}: {exc}"

    return result
