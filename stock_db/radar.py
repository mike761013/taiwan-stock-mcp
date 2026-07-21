"""Database-first V11 market radar."""

from __future__ import annotations

from datetime import date
from typing import Any

from .connection import stock_database
from .service import stock_database_service


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
