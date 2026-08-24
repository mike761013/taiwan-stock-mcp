"""Free-tier-safe V11 daily maintenance orchestration."""

from __future__ import annotations

from typing import Any

from .performance import (
    DEFAULT_PERFORMANCE_UPDATE_LIMIT,
    update_signal_execution_performance,
    update_signal_performance,
)
from .pipeline import sync_security_master, update_official_daily
from .factors import (
    DEFAULT_FUNDAMENTAL_REFRESH_INTERVAL_DAYS,
    refresh_monthly_revenue_if_due,
)
from .radar import run_full_bullish_radar_v12

# Compatibility name kept for older tests/imports and third-party callers.
run_full_bullish_radar = run_full_bullish_radar_v12


async def run_daily_maintenance(
    run_radar: bool = True,
    update_performance: bool = True,
    batch_size: int = 500,
    start_after: str | None = None,
    concurrency: int = 6,
    radar_limit_each: int = 20,
    radar_minimum_score: float = 45,
    fundamental_refresh_interval_days: int = (
        DEFAULT_FUNDAMENTAL_REFRESH_INTERVAL_DAYS
    ),
    force_fundamental_refresh: bool = False,
) -> dict[str, Any]:
    """Run one resumable daily-maintenance step.

    Repeatedly call the same tool with nextStartAfter until completed=true.
    Daily indicators use a bulk latest-61-bars path, so 500-symbol batches do
    not consume Fugle quota or open one database transaction per symbol.
    Radar and performance are executed only after all daily indicators finish.
    Fundamentals and automatic industry-theme tags are refreshed only when
    their interval is due; the default is once every seven days.
    """
    master_update = None
    if not start_after:
        try:
            master_update = await sync_security_master()
        except Exception as exc:
            master_update = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        market_update = await update_official_daily(
            batch_size=batch_size,
            start_after=start_after,
            concurrency=concurrency,
        )
    except Exception as exc:
        return {
            "ok": False,
            "completed": False,
            "stage": "market_update",
            "error": f"{type(exc).__name__}: {exc}",
        }

    result: dict[str, Any] = {
        "ok": bool(market_update.get("ok")),
        "completed": False,
        "stage": "market_update",
        "marketUpdate": market_update,
        "securityMasterUpdate": master_update,
        "hasMore": bool(market_update.get("hasMore")),
        "nextStartAfter": market_update.get("nextStartAfter"),
        "remainingSymbols": int(market_update.get("remainingSymbols", 0)),
    }

    # Do not run radar or performance finalisation against a rejected or
    # partially failed market snapshot. In particular, a failed same-date
    # fallback must never produce an apparently formal V12 radar result.
    if not market_update.get("ok"):
        return result

    if market_update.get("hasMore"):
        return result

    result["stage"] = "finalize"
    try:
        result["fundamentalUpdate"] = await refresh_monthly_revenue_if_due(
            interval_days=fundamental_refresh_interval_days,
            force=force_fundamental_refresh,
        )
    except Exception as exc:
        # The radar can still run with transparent missing-factor confidence.
        result["fundamentalUpdate"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if run_radar:
        try:
            result["radar"] = await run_full_bullish_radar(
                limit_each=radar_limit_each,
                minimum_score=radar_minimum_score,
                save_result=True,
            )
        except Exception as exc:
            result["ok"] = False
            result["radarError"] = f"{type(exc).__name__}: {exc}"

    if update_performance:
        try:
            result["performance"] = await update_signal_performance(
                limit=DEFAULT_PERFORMANCE_UPDATE_LIMIT,
            )
            result["executionPerformance"] = (
                await update_signal_execution_performance(
                    limit=DEFAULT_PERFORMANCE_UPDATE_LIMIT,
                )
            )
        except Exception as exc:
            result["ok"] = False
            result["performanceError"] = f"{type(exc).__name__}: {exc}"

    result.update({
        "completed": True,
        "hasMore": False,
        "nextStartAfter": None,
        "remainingSymbols": 0,
    })
    return result
