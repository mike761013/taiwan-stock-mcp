"""Free-tier-safe V11 daily maintenance orchestration."""

from __future__ import annotations

from typing import Any

from .performance import update_signal_performance
from .pipeline import update_official_daily
from .radar import run_full_bullish_radar


async def run_daily_maintenance(
    run_radar: bool = True,
    update_performance: bool = True,
    batch_size: int = 50,
    start_after: str | None = None,
    concurrency: int = 6,
    radar_limit_each: int = 20,
    radar_minimum_score: float = 45,
) -> dict[str, Any]:
    """Run one resumable daily-maintenance step.

    Repeatedly call the same tool with nextStartAfter until completed=true.
    Radar and performance are executed only after all daily indicators finish.
    """
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
        "hasMore": bool(market_update.get("hasMore")),
        "nextStartAfter": market_update.get("nextStartAfter"),
        "remainingSymbols": int(market_update.get("remainingSymbols", 0)),
    }

    if market_update.get("hasMore"):
        return result

    result["stage"] = "finalize"
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
            result["performance"] = await update_signal_performance()
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
