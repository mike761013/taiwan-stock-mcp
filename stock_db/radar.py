"""Database-first V11 market radar."""

from __future__ import annotations

from datetime import date
from typing import Any

from .connection import stock_database
from .service import stock_database_service
from .v12 import (
    V12_STRATEGIES,
    load_v12_config,
    screen_v12_rows,
)


_STRATEGIES = {"early_stage", "breakout", "pullback"}


async def screen_database_market(
    strategy: str = "early_stage",
    limit: int = 30,
    minimum_score: float = 45,
    save_result: bool = True,
) -> dict[str, Any]:
    strategy = strategy.strip().lower()
    if strategy not in _STRATEGIES:
        raise ValueError(f"strategy must be one of {sorted(_STRATEGIES)}")
    limit = max(1, min(limit, 200))
    minimum_score = max(0.0, min(float(minimum_score), 100.0))

    strategy_filter = {
        "early_stage": """
            AND i.ma5 >= i.ma20
            AND b.close >= i.ma20
            AND COALESCE(i.volume_ratio, 0) BETWEEN 0.8 AND 2.5
        """,
        "breakout": """
            AND b.close >= COALESCE(i.bollinger_upper, b.close)
            AND COALESCE(i.volume_ratio, 0) >= 1.2
        """,
        "pullback": """
            AND b.close >= i.ma20
            AND b.close <= i.ma5 * 1.03
            AND i.ma20 >= i.ma60
        """,
    }[strategy]

    query = f"""
        WITH latest AS (
            SELECT MAX(trade_date) AS trade_date FROM daily_indicators
        )
        SELECT b.symbol, s.name, s.market, b.trade_date, b.close, b.volume,
               i.ma5, i.ma20, i.ma60, i.volume_ratio,
               i.bollinger_upper, i.large_volume_low,
               i.technical_score,
               CASE
                 WHEN i.large_volume_low IS NOT NULL AND b.close >= i.large_volume_low
                 THEN 8 ELSE 0
               END AS large_volume_bonus,
               LEAST(100,
                 COALESCE(i.technical_score, 0)
                 + CASE WHEN COALESCE(i.volume_ratio,0) >= 1.2 THEN 8 ELSE 0 END
                 + CASE WHEN i.ma5 > i.ma20 THEN 5 ELSE 0 END
               ) AS total_score
        FROM latest
        JOIN daily_indicators i ON i.trade_date=latest.trade_date
        JOIN daily_bars b ON b.symbol=i.symbol AND b.trade_date=i.trade_date
        JOIN securities s ON s.symbol=b.symbol
        WHERE s.is_active=TRUE
          AND COALESCE(i.technical_score,0) >= $1
          {strategy_filter}
        ORDER BY total_score DESC, i.volume_ratio DESC NULLS LAST
        LIMIT $2
    """
    async with stock_database.acquire() as connection:
        rows = await connection.fetch(query, minimum_score, limit)
        universe_count = int(await connection.fetchval(
            "SELECT COUNT(*) FROM securities WHERE is_active=TRUE"
        ) or 0)
        latest_trade_date = await connection.fetchval(
            "SELECT MAX(trade_date) FROM daily_indicators"
        )

    candidates: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, 1):
        item = dict(row)
        item["rank"] = rank
        item["reasons"] = [
            reason for condition, reason in (
                (
                    item.get("ma5") is not None
                    and item.get("ma20") is not None
                    and item["ma5"] > item["ma20"],
                    "MA5高於MA20",
                ),
                (
                    item.get("volume_ratio") is not None
                    and item["volume_ratio"] >= 1.2,
                    "量比放大",
                ),
                (
                    item.get("large_volume_low") is not None
                    and item.get("close") is not None
                    and item["close"] >= item["large_volume_low"],
                    "守住大量低點",
                ),
            ) if condition
        ]
        candidates.append(item)

    saved = None
    # Save every requested run, including zero-candidate runs, so database history
    # accurately records that the strategy executed successfully.
    if save_result:
        saved = await stock_database_service.save_radar_result(
            strategy=strategy,
            candidates=candidates,
            run_date=date.today(),
            universe_count=universe_count,
            configuration={
                "minimumScore": minimum_score,
                "limit": limit,
                "engine": "postgres-v11",
                "latestTradeDate": str(latest_trade_date) if latest_trade_date else None,
            },
        )

    return {
        "ok": True,
        "strategy": strategy,
        "candidateCount": len(candidates),
        "universeCount": universe_count,
        "latestTradeDate": latest_trade_date,
        "results": candidates,
        "record": saved,
        "source": "PostgreSQL V11",
    }


async def run_full_bullish_radar(
    limit_each: int = 20,
    minimum_score: float = 45,
    save_result: bool = True,
) -> dict[str, Any]:
    limit_each = max(1, min(limit_each, 200))
    grouped: dict[str, dict[str, Any]] = {}
    merged: dict[str, dict[str, Any]] = {}

    for strategy in ("early_stage", "breakout", "pullback"):
        result = await screen_database_market(
            strategy=strategy,
            limit=limit_each,
            minimum_score=minimum_score,
            save_result=save_result,
        )
        grouped[strategy] = result
        for item in result["results"]:
            symbol = item["symbol"]
            existing = merged.get(symbol)
            if existing is None or float(item.get("total_score") or 0) > float(
                existing.get("total_score") or 0
            ):
                merged[symbol] = {**item, "strategies": [strategy]}
            elif strategy not in existing["strategies"]:
                existing["strategies"].append(strategy)

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            len(item.get("strategies", [])),
            float(item.get("total_score") or 0),
            float(item.get("volume_ratio") or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, 1):
        item["combinedRank"] = index

    return {
        "ok": True,
        "candidateCount": len(ranked),
        "minimumScore": minimum_score,
        "top10": ranked[:10],
        "top5": ranked[:5],
        "watchlistCandidates": ranked[:3],
        "byStrategy": grouped,
        "source": "PostgreSQL V11",
    }


# ---------------------------------------------------------------------------
# V12: V7 liquidity + V11 strategies + early reversal + ATR trading plan
# ---------------------------------------------------------------------------

_V12_SNAPSHOT_QUERY = """
    WITH latest_date AS (
        SELECT MAX(trade_date) AS trade_date FROM daily_indicators
    ),
    recent_bars AS (
        SELECT b.*
        FROM daily_bars b
        CROSS JOIN latest_date d
        WHERE b.trade_date >= d.trade_date - INTERVAL '120 days'
    ),
    bar_windows AS (
        SELECT b.*,
               LAG(b.close) OVER (
                   PARTITION BY b.symbol ORDER BY b.trade_date
               ) AS previous_close_for_tr,
               ROW_NUMBER() OVER (
                   PARTITION BY b.symbol ORDER BY b.trade_date DESC
               ) AS reverse_rank
        FROM recent_bars b
    ),
    true_ranges AS (
        SELECT symbol, trade_date,
               GREATEST(
                   COALESCE(high - low, 0),
                   COALESCE(ABS(high - previous_close_for_tr), 0),
                   COALESCE(ABS(low - previous_close_for_tr), 0)
               ) AS true_range
        FROM bar_windows
    ),
    atr_history AS (
        SELECT symbol, trade_date,
               CASE
                 WHEN COUNT(true_range) OVER (
                     PARTITION BY symbol ORDER BY trade_date
                     ROWS BETWEEN 13 PRECEDING AND CURRENT ROW
                 ) >= 14
                 THEN AVG(true_range) OVER (
                     PARTITION BY symbol ORDER BY trade_date
                     ROWS BETWEEN 13 PRECEDING AND CURRENT ROW
                 )
                 ELSE NULL
               END AS atr14
        FROM true_ranges
    ),
    previous_bars AS (
        SELECT symbol,
               open AS prev_open,
               high AS prev_high,
               low AS prev_low,
               close AS prev_close,
               volume AS prev_volume
        FROM bar_windows
        WHERE reverse_rank = 2
    )
    SELECT b.symbol, s.name, s.market, b.trade_date,
           b.open, b.high, b.low, b.close, b.volume, b.turnover,
           b.change_percent,
           i.ma5, i.ma10, i.ma20, i.ma60,
           i.volume_ma5, i.volume_ma20, i.volume_ratio,
           i.bollinger_mid, i.bollinger_upper, i.bollinger_lower,
           i.volatility_20, i.large_volume_low, i.technical_score,
           p.prev_open, p.prev_high, p.prev_low, p.prev_close, p.prev_volume,
           a.atr14
    FROM latest_date d
    JOIN daily_indicators i ON i.trade_date = d.trade_date
    JOIN daily_bars b ON b.symbol = i.symbol AND b.trade_date = i.trade_date
    JOIN securities s ON s.symbol = b.symbol
    LEFT JOIN previous_bars p ON p.symbol = b.symbol
    LEFT JOIN atr_history a ON a.symbol = b.symbol AND a.trade_date = b.trade_date
    WHERE s.is_active = TRUE
"""


async def _fetch_v12_snapshot() -> tuple[list[dict[str, Any]], int, Any]:
    async with stock_database.acquire() as connection:
        rows = await connection.fetch(_V12_SNAPSHOT_QUERY)
        universe_count = int(await connection.fetchval(
            "SELECT COUNT(*) FROM securities WHERE is_active=TRUE"
        ) or 0)
        latest_trade_date = await connection.fetchval(
            "SELECT MAX(trade_date) FROM daily_indicators"
        )
    return [dict(row) for row in rows], universe_count, latest_trade_date


async def _save_v12_strategy(
    strategy: str,
    candidates: list[dict[str, Any]],
    universe_count: int,
    latest_trade_date: Any,
    minimum_score: float,
    limit: int,
    config: Any,
) -> dict[str, Any]:
    return await stock_database_service.save_radar_result(
        strategy=f"v12_{strategy}",
        candidates=candidates,
        run_date=date.today(),
        universe_count=universe_count,
        configuration={
            "minimumScore": minimum_score,
            "limit": limit,
            "engine": "postgres-v12",
            "latestTradeDate": str(latest_trade_date) if latest_trade_date else None,
            "v12": config.public_dict(),
        },
    )


async def screen_database_market_v12(
    strategy: str = "reversal_reclaim",
    limit: int = 30,
    minimum_score: float = 45,
    save_result: bool = True,
) -> dict[str, Any]:
    """Run one V12 strategy against the latest PostgreSQL market snapshot."""
    strategy = strategy.strip().lower()
    if strategy not in V12_STRATEGIES:
        raise ValueError(f"strategy must be one of {list(V12_STRATEGIES)}")
    limit = max(1, min(limit, 200))
    minimum_score = max(0.0, min(float(minimum_score), 100.0))
    config = load_v12_config()
    rows, universe_count, latest_trade_date = await _fetch_v12_snapshot()
    candidates, rejection_summary = screen_v12_rows(
        rows=rows,
        strategy=strategy,
        minimum_score=minimum_score,
        limit=limit,
        config=config,
    )

    saved = None
    if save_result:
        saved = await _save_v12_strategy(
            strategy,
            candidates,
            universe_count,
            latest_trade_date,
            minimum_score,
            limit,
            config,
        )

    return {
        "ok": True,
        "version": "V12",
        "strategy": strategy,
        "candidateCount": len(candidates),
        "universeCount": universe_count,
        "snapshotCount": len(rows),
        "latestTradeDate": latest_trade_date,
        "minimumScore": minimum_score,
        "liquidityRules": {
            "minDailyVolumeLots": config.min_daily_volume_lots,
            "minAverageVolume20Lots": config.min_average_volume20_lots,
            "minTradeValue": config.effective_min_trade_value,
        },
        "rejectionSummary": rejection_summary,
        "results": candidates,
        "record": saved,
        "source": "PostgreSQL V12",
    }


async def run_full_bullish_radar_v12(
    limit_each: int = 20,
    minimum_score: float = 45,
    save_result: bool = True,
) -> dict[str, Any]:
    """Run all four V12 bullish strategies and merge their tradable candidates."""
    limit_each = max(1, min(limit_each, 200))
    minimum_score = max(0.0, min(float(minimum_score), 100.0))
    config = load_v12_config()
    rows, universe_count, latest_trade_date = await _fetch_v12_snapshot()

    grouped: dict[str, dict[str, Any]] = {}
    merged: dict[str, dict[str, Any]] = {}

    for strategy in V12_STRATEGIES:
        candidates, rejection_summary = screen_v12_rows(
            rows=rows,
            strategy=strategy,
            minimum_score=minimum_score,
            limit=limit_each,
            config=config,
        )
        record = None
        if save_result:
            record = await _save_v12_strategy(
                strategy,
                candidates,
                universe_count,
                latest_trade_date,
                minimum_score,
                limit_each,
                config,
            )
        grouped[strategy] = {
            "ok": True,
            "version": "V12",
            "strategy": strategy,
            "candidateCount": len(candidates),
            "universeCount": universe_count,
            "latestTradeDate": latest_trade_date,
            "rejectionSummary": rejection_summary,
            "results": candidates,
            "record": record,
            "source": "PostgreSQL V12",
        }

        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "").strip()
            if not symbol:
                continue
            existing = merged.get(symbol)
            if existing is None:
                merged[symbol] = dict(candidate)
                merged[symbol]["strategies"] = [strategy]
                continue

            strategies = set(existing.get("strategies") or [])
            strategies.add(strategy)
            if float(candidate.get("total_score") or 0) > float(
                existing.get("total_score") or 0
            ):
                replacement = dict(candidate)
                replacement["strategies"] = sorted(strategies)
                merged[symbol] = replacement
            else:
                existing["strategies"] = sorted(strategies)

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            float(item.get("total_score") or 0),
            len(item.get("strategies") or []),
            -float((item.get("tradingPlan") or {}).get("maximumRiskPercent") or 999),
            float(item.get("volume_ratio") or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(ranked, start=1):
        item["combinedRank"] = index

    combined_record = None
    if save_result:
        combined_record = await stock_database_service.save_radar_result(
            strategy="v12_combined",
            candidates=ranked,
            run_date=date.today(),
            universe_count=universe_count,
            configuration={
                "minimumScore": minimum_score,
                "limitEach": limit_each,
                "engine": "postgres-v12",
                "latestTradeDate": str(latest_trade_date) if latest_trade_date else None,
                "v12": config.public_dict(),
            },
        )

    return {
        "ok": True,
        "version": "V12",
        "strategies": list(V12_STRATEGIES),
        "candidateCount": len(ranked),
        "minimumScore": minimum_score,
        "limitEach": limit_each,
        "universeCount": universe_count,
        "snapshotCount": len(rows),
        "latestTradeDate": latest_trade_date,
        "liquidityRules": {
            "minDailyVolumeLots": config.min_daily_volume_lots,
            "minAverageVolume20Lots": config.min_average_volume20_lots,
            "minTradeValue": config.effective_min_trade_value,
        },
        "top10": ranked[:10],
        "top5": ranked[:5],
        "watchlistCandidates": ranked[:3],
        "byStrategy": grouped,
        "record": combined_record,
        "source": "PostgreSQL V12",
    }
