"""Database-first V11 market radar."""

from __future__ import annotations

from datetime import date
from typing import Any

from .connection import stock_database
from .performance import execution_strategy_priors
from .service import stock_database_service
from .v12 import (
    V12_ACTIONABLE_STATUS_CODES,
    V12_STRATEGIES,
    apply_execution_prior,
    load_v12_config,
    screen_v12_rows,
    split_v12_price_tiers,
)


_STRATEGIES = {"early_stage", "breakout", "pullback", "reversal_reclaim"}
_COMMON_STOCK_FILTER = """
          UPPER(s.market) IN ('TWSE', 'TPEX', 'OTC')
          AND s.symbol ~ '^[1-9][0-9]{3}$'
"""
_COMMON_STOCK_UNIVERSE_COUNT_QUERY = f"""
    SELECT COUNT(*)
    FROM securities s
    WHERE s.is_active = TRUE
      AND {_COMMON_STOCK_FILTER}
"""


async def screen_database_market(
    strategy: str = "early_stage",
    limit: int = 30,
    minimum_score: float = 45,
    save_result: bool = True,
) -> dict[str, Any]:
    strategy = strategy.strip().lower()
    if strategy not in _STRATEGIES:
        raise ValueError(f"strategy must be one of {sorted(_STRATEGIES)}")
    # reversal_reclaim is a V12 pattern and needs the richer V12 snapshot.
    if strategy == "reversal_reclaim":
        return await screen_database_market_v12(
            strategy=strategy,
            limit=limit,
            minimum_score=minimum_score,
            save_result=save_result,
        )
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
          AND {_COMMON_STOCK_FILTER}
          AND COALESCE(i.technical_score,0) >= $1
          {strategy_filter}
        ORDER BY total_score DESC, i.volume_ratio DESC NULLS LAST
        LIMIT $2
    """
    async with stock_database.acquire() as connection:
        rows = await connection.fetch(query, minimum_score, limit)
        universe_count = int(await connection.fetchval(
            _COMMON_STOCK_UNIVERSE_COUNT_QUERY
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
# V12: V7 liquidity + V11 strategies + bottom reversal + ATR trading plan
# ---------------------------------------------------------------------------

_V12_SNAPSHOT_QUERY = f"""
    WITH global_latest_date AS (
        SELECT MAX(trade_date) AS trade_date FROM daily_indicators
    ),
    market_latest_dates AS (
        SELECT UPPER(s.market) AS market_key,
               MAX(i.trade_date) AS trade_date
        FROM daily_indicators i
        JOIN securities s ON s.symbol = i.symbol
        WHERE s.is_active = TRUE
          AND {_COMMON_STOCK_FILTER}
        GROUP BY UPPER(s.market)
    ),
    recent_bars AS (
        SELECT b.*
        FROM daily_bars b
        CROSS JOIN global_latest_date d
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
    indicator_windows AS (
        SELECT i.*,
               ROW_NUMBER() OVER (
                   PARTITION BY i.symbol ORDER BY i.trade_date DESC
               ) AS reverse_rank
        FROM daily_indicators i
        CROSS JOIN global_latest_date d
        WHERE i.trade_date >= d.trade_date - INTERVAL '120 days'
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
               MAX(open) FILTER (WHERE reverse_rank = 2) AS prev_open,
               MAX(high) FILTER (WHERE reverse_rank = 2) AS prev_high,
               MAX(low) FILTER (WHERE reverse_rank = 2) AS prev_low,
               MAX(close) FILTER (WHERE reverse_rank = 2) AS prev_close,
               MAX(volume) FILTER (WHERE reverse_rank = 2) AS prev_volume,
               MAX(low) FILTER (WHERE reverse_rank = 3) AS prev2_low,
               MAX(close) FILTER (WHERE reverse_rank = 3) AS prev2_close,
               MAX(close) FILTER (WHERE reverse_rank = 6) AS close5,
               MAX(close) FILTER (WHERE reverse_rank = 11) AS close10,
               MAX(close) FILTER (WHERE reverse_rank = 21) AS close20,
               MAX(high) FILTER (WHERE reverse_rank <= 20) AS high20,
               MIN(low) FILTER (WHERE reverse_rank <= 20) AS low20,
               MAX(high) FILTER (WHERE reverse_rank <= 60) AS high60,
               MIN(low) FILTER (WHERE reverse_rank <= 60) AS low60
        FROM bar_windows
        WHERE reverse_rank <= 60
        GROUP BY symbol
    ),
    previous_indicators AS (
        SELECT symbol,
               MAX(ma5) FILTER (WHERE reverse_rank = 2) AS prev_ma5,
               MAX(ma10) FILTER (WHERE reverse_rank = 2) AS prev_ma10,
               MAX(ma20) FILTER (WHERE reverse_rank = 2) AS prev_ma20,
               MAX(ma60) FILTER (WHERE reverse_rank = 2) AS prev_ma60
        FROM indicator_windows
        WHERE reverse_rank <= 2
        GROUP BY symbol
    )
    SELECT b.symbol, s.name, s.market, b.trade_date,
           b.open, b.high, b.low, b.close, b.volume, b.turnover,
           b.change_percent,
           i.ma5, i.ma10, i.ma20, i.ma60,
           i.volume_ma5, i.volume_ma20, i.volume_ratio,
           i.bollinger_mid, i.bollinger_upper, i.bollinger_lower,
           i.volatility_20, i.large_volume_low, i.technical_score,
           p.prev_open, p.prev_high, p.prev_low, p.prev_close, p.prev_volume,
           p.prev2_low, p.prev2_close,
           p.close5, p.close10, p.close20,
           p.high20, p.low20, p.high60, p.low60,
           pi.prev_ma5, pi.prev_ma10, pi.prev_ma20, pi.prev_ma60,
           a.atr14
    FROM daily_indicators i
    JOIN daily_bars b ON b.symbol = i.symbol AND b.trade_date = i.trade_date
    JOIN securities s ON s.symbol = b.symbol
    JOIN market_latest_dates d
      ON d.market_key = UPPER(s.market)
    AND d.trade_date = i.trade_date
    LEFT JOIN previous_bars p ON p.symbol = b.symbol
    LEFT JOIN previous_indicators pi ON pi.symbol = b.symbol
    LEFT JOIN atr_history a ON a.symbol = b.symbol AND a.trade_date = b.trade_date
    WHERE s.is_active = TRUE
      AND {_COMMON_STOCK_FILTER}
"""

_V12_MARKET_DATES_QUERY = f"""
    SELECT CASE
             WHEN UPPER(s.market) = 'OTC' THEN 'TPEX'
             ELSE UPPER(s.market)
           END AS market_key,
           MAX(i.trade_date) AS trade_date
    FROM daily_indicators i
    JOIN securities s ON s.symbol = i.symbol
    WHERE s.is_active = TRUE
      AND {_COMMON_STOCK_FILTER}
    GROUP BY CASE
               WHEN UPPER(s.market) = 'OTC' THEN 'TPEX'
               ELSE UPPER(s.market)
             END
"""


async def _fetch_v12_snapshot() -> tuple[list[dict[str, Any]], int, Any]:
    async with stock_database.acquire() as connection:
        market_date_rows = await connection.fetch(
            _V12_MARKET_DATES_QUERY
        )
        market_dates = {
            str(row["market_key"]): row["trade_date"]
            for row in market_date_rows
            if row["market_key"] in {"TWSE", "TPEX"}
            and row["trade_date"] is not None
        }
        if set(market_dates) != {"TWSE", "TPEX"}:
            raise RuntimeError(
                "V12_MARKET_DATA_INCOMPLETE: "
                f"marketDates={market_dates}; "
                "請先完成具同日備援的收盤作業。"
            )
        if len(set(market_dates.values())) != 1:
            raise RuntimeError(
                "V12_MARKET_DATE_MISMATCH: "
                f"marketDates={market_dates}; "
                "上市與上櫃日期不同，正式雷達已拒絕執行。"
            )
        rows = await connection.fetch(_V12_SNAPSHOT_QUERY)
        universe_count = int(await connection.fetchval(
            _COMMON_STOCK_UNIVERSE_COUNT_QUERY
        ) or 0)
        latest_trade_date = next(iter(market_dates.values()))
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
    priors = (
        await execution_strategy_priors(
            minimum_samples=config.execution_prior_min_samples,
            full_confidence_samples=config.execution_prior_full_confidence_samples,
            maximum_adjustment=config.execution_prior_max_adjustment,
        )
        if rows else {}
    )
    raw_candidates, rejection_summary = screen_v12_rows(
        rows=rows,
        strategy=strategy,
        minimum_score=minimum_score,
        limit=200,
        config=config,
    )
    raw_candidates = [
        apply_execution_prior(candidate, priors.get(strategy), config)
        for candidate in raw_candidates
    ]
    raw_candidates.sort(
        key=lambda item: (
            float(item.get("ranking_score") or 0),
            float(item.get("bullish_score") or 0),
        ),
        reverse=True,
    )
    tiers = split_v12_price_tiers(raw_candidates, config)
    primary_results = tiers["main"][:limit]
    high_price_results = tiers["highPrice"][
        : min(limit, config.high_price_limit)
    ]
    candidates = primary_results + high_price_results
    actionable_results = [
        candidate for candidate in candidates
        if str(candidate.get("actionCode") or "")
        in V12_ACTIONABLE_STATUS_CODES
    ]
    watch_results = [
        candidate for candidate in candidates
        if str(candidate.get("actionCode") or "")
        not in V12_ACTIONABLE_STATUS_CODES
    ]
    for rank, candidate in enumerate(primary_results, start=1):
        candidate["rank"] = rank
    for rank, candidate in enumerate(high_price_results, start=1):
        candidate["highPriceRank"] = rank

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
        "accuracyEngine": "V12.1_FORWARD_BULLISH",
        "strategy": strategy,
        "candidateCount": len(candidates),
        "rawCandidateCount": len(raw_candidates),
        "mainCandidateCount": len(primary_results),
        "highPriceCandidateCount": len(high_price_results),
        "excludedHighPriceCount": len(tiers["rejectedHighPrice"]),
        "actionableCandidateCount": len(actionable_results),
        "watchCandidateCount": len(watch_results),
        "universeCount": universe_count,
        "snapshotCount": len(rows),
        "latestTradeDate": latest_trade_date,
        "minimumScore": minimum_score,
        "liquidityRules": {
            "minDailyVolumeLots": config.min_daily_volume_lots,
            "minAverageVolume20Lots": config.min_average_volume20_lots,
            "minTradeValue": config.effective_min_trade_value,
        },
        "priceRules": {
            "primaryMaxPrice": config.primary_max_price,
            "highPriceMinScore": config.high_price_min_score,
            "highPriceMaxRiskPercent": config.high_price_max_risk_pct,
            "highPriceLimit": config.high_price_limit,
            "highPriceRequiresNoWarnings": True,
        },
        "rejectionSummary": rejection_summary,
        "results": candidates,
        "actionableResults": actionable_results,
        "watchResults": watch_results,
        "primaryResults": primary_results,
        "highPriceStrongResults": high_price_results,
        "record": saved,
        "executionPriors": priors,
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
    priors = (
        await execution_strategy_priors(
            minimum_samples=config.execution_prior_min_samples,
            full_confidence_samples=config.execution_prior_full_confidence_samples,
            maximum_adjustment=config.execution_prior_max_adjustment,
        )
        if rows else {}
    )

    grouped: dict[str, dict[str, Any]] = {}
    merged: dict[str, dict[str, Any]] = {}
    rejected_high_price_symbols: set[str] = set()

    for strategy in V12_STRATEGIES:
        raw_candidates, rejection_summary = screen_v12_rows(
            rows=rows,
            strategy=strategy,
            minimum_score=minimum_score,
            limit=200,
            config=config,
        )
        raw_candidates = [
            apply_execution_prior(candidate, priors.get(strategy), config)
            for candidate in raw_candidates
        ]
        raw_candidates.sort(
            key=lambda item: (
                float(item.get("ranking_score") or 0),
                float(item.get("bullish_score") or 0),
            ),
            reverse=True,
        )
        tiers = split_v12_price_tiers(raw_candidates, config)
        primary_results = tiers["main"][:limit_each]
        high_price_results = tiers["highPrice"][
            : min(limit_each, config.high_price_limit)
        ]
        rejected_high_price_symbols.update(
            str(candidate.get("symbol") or "").strip()
            for candidate in tiers["rejectedHighPrice"]
            if str(candidate.get("symbol") or "").strip()
        )
        candidates = primary_results + high_price_results
        for rank, candidate in enumerate(primary_results, start=1):
            candidate["rank"] = rank
        for rank, candidate in enumerate(high_price_results, start=1):
            candidate["highPriceRank"] = rank
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
            "rawCandidateCount": len(raw_candidates),
            "mainCandidateCount": len(primary_results),
            "highPriceCandidateCount": len(high_price_results),
            "excludedHighPriceCount": len(tiers["rejectedHighPrice"]),
            "universeCount": universe_count,
            "latestTradeDate": latest_trade_date,
            "rejectionSummary": rejection_summary,
            "results": candidates,
            "primaryResults": primary_results,
            "highPriceStrongResults": high_price_results,
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
            if float(
                candidate.get("ranking_score")
                or candidate.get("total_score")
                or 0
            ) > float(
                existing.get("ranking_score")
                or existing.get("total_score")
                or 0
            ):
                replacement = dict(candidate)
                replacement["strategies"] = sorted(strategies)
                merged[symbol] = replacement
            else:
                existing["strategies"] = sorted(strategies)

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            float(item.get("ranking_score") or item.get("total_score") or 0)
            + min(
                config.consensus_bonus_cap,
                max(0, len(item.get("strategies") or []) - 1)
                * config.consensus_bonus_per_extra_strategy,
            ),
            float(item.get("bullish_score") or item.get("total_score") or 0),
            len(item.get("strategies") or []),
            -float((item.get("tradingPlan") or {}).get("maximumRiskPercent") or 999),
            float(item.get("volume_ratio") or 0),
        ),
        reverse=True,
    )
    for item in ranked:
        consensus_bonus = min(
            config.consensus_bonus_cap,
            max(0, len(item.get("strategies") or []) - 1)
            * config.consensus_bonus_per_extra_strategy,
        )
        item["consensusBonus"] = round(consensus_bonus, 2)
        item["ranking_score"] = round(
            min(
                100.0,
                float(item.get("ranking_score") or 0) + consensus_bonus,
            ),
            2,
        )
    combined_tiers = split_v12_price_tiers(ranked, config)
    primary_ranked = combined_tiers["main"]
    high_price_ranked = combined_tiers["highPrice"][: config.high_price_limit]
    for index, item in enumerate(primary_ranked, start=1):
        item["combinedRank"] = index
        item["rank"] = index
    for index, item in enumerate(high_price_ranked, start=1):
        item["highPriceRank"] = index
    actionable_primary = [
        item for item in primary_ranked
        if str(item.get("actionCode") or "")
        in V12_ACTIONABLE_STATUS_CODES
    ]
    watch_primary = [
        item for item in primary_ranked
        if str(item.get("actionCode") or "")
        not in V12_ACTIONABLE_STATUS_CODES
    ]
    for index, item in enumerate(actionable_primary, start=1):
        item["actionableRank"] = index
    bullish_ranked = sorted(
        primary_ranked,
        key=lambda item: (
            float(item.get("bullish_score") or item.get("total_score") or 0),
            float(item.get("ranking_score") or 0),
        ),
        reverse=True,
    )
    for index, item in enumerate(bullish_ranked, start=1):
        item["bullishRank"] = index
    displayed_candidates = primary_ranked + high_price_ranked
    accepted_high_price_symbols = {
        str(candidate.get("symbol") or "").strip()
        for candidate in high_price_ranked
        if str(candidate.get("symbol") or "").strip()
    }
    excluded_high_price_count = len(
        rejected_high_price_symbols - accepted_high_price_symbols
    )

    combined_record = None
    if save_result:
        combined_record = await stock_database_service.save_radar_result(
            strategy="v12_combined",
            candidates=displayed_candidates,
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
        "accuracyEngine": "V12.1_FORWARD_BULLISH",
        "strategies": list(V12_STRATEGIES),
        "candidateCount": len(displayed_candidates),
        "rawCandidateCount": len(ranked),
        "mainCandidateCount": len(primary_ranked),
        "highPriceCandidateCount": len(high_price_ranked),
        "actionableCandidateCount": len(actionable_primary),
        "watchCandidateCount": len(watch_primary),
        "excludedHighPriceCount": excluded_high_price_count,
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
        "priceRules": {
            "primaryMaxPrice": config.primary_max_price,
            "highPriceMinScore": config.high_price_min_score,
            "highPriceMaxRiskPercent": config.high_price_max_risk_pct,
            "highPriceLimit": config.high_price_limit,
            "highPriceRequiresNoWarnings": True,
        },
        "top10": actionable_primary[:10],
        "top5": actionable_primary[:5],
        "actionableCandidates": actionable_primary,
        "bullishTop10": bullish_ranked[:10],
        "watchlistCandidates": watch_primary[:10],
        "highPriceStrongCandidates": high_price_ranked,
        "byStrategy": grouped,
        "record": combined_record,
        "executionPriors": priors,
        "rankingMethod": {
            "bullishWeight": config.ranking_bullish_weight,
            "executionWeight": config.ranking_execution_weight,
            "top10Excludes": [
                "WAIT_PULLBACK",
                "DO_NOT_CHASE",
                "BOTTOM_REVERSAL_WATCH",
                "SMALL_POSITION_OR_SKIP",
            ],
            "historicalAdjustmentUsesExecutionOnly": True,
        },
        "source": "PostgreSQL V12",
    }
