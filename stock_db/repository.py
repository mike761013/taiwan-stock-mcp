"""PostgreSQL repository with idempotent bulk upserts."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from .connection import StockDatabase, stock_database
from .models import DailyBar, DailyIndicator, Security

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value, preserving valid zero scores."""
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


class StockRepository:
    def __init__(self, database: StockDatabase | None = None) -> None:
        self.database = database or stock_database

    async def initialize_schema(self) -> dict[str, Any]:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        async with self.database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(sql)
                version = await connection.fetchval(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_versions"
                )
        return {"ok": True, "schemaVersion": int(version or 0)}

    async def upsert_securities(self, rows: Sequence[Security]) -> int:
        if not rows:
            return 0
        values = [
            (r.symbol, r.name, r.market, r.industry, r.is_active)
            for r in rows
        ]
        sql = """
        INSERT INTO securities(symbol, name, market, industry, is_active)
        SELECT * FROM UNNEST(
            $1::varchar[], $2::varchar[], $3::varchar[], $4::varchar[], $5::boolean[]
        )
        ON CONFLICT(symbol) DO UPDATE SET
            name=EXCLUDED.name,
            market=EXCLUDED.market,
            industry=EXCLUDED.industry,
            is_active=EXCLUDED.is_active,
            updated_at=NOW()
        """
        columns = list(zip(*values))
        async with self.database.acquire() as connection:
            await connection.execute(sql, *[list(c) for c in columns])
        return len(rows)

    async def bulk_upsert_daily_bars(self, rows: Sequence[DailyBar]) -> int:
        if not rows:
            return 0
        records = [
            (
                r.symbol, r.trade_date, r.open, r.high, r.low, r.close,
                r.volume, r.turnover, r.change_percent, r.source,
            )
            for r in rows
        ]
        async with self.database.acquire() as connection:
            async with connection.transaction():
                await connection.execute("""
                    CREATE TEMP TABLE IF NOT EXISTS tmp_daily_bars (
                        symbol varchar(16), trade_date date,
                        open numeric(14,4), high numeric(14,4),
                        low numeric(14,4), close numeric(14,4),
                        volume bigint, turnover bigint,
                        change_percent numeric(10,4), source varchar(32)
                    ) ON COMMIT DROP
                """)
                await connection.copy_records_to_table(
                    "tmp_daily_bars",
                    records=records,
                    columns=[
                        "symbol", "trade_date", "open", "high", "low", "close",
                        "volume", "turnover", "change_percent", "source",
                    ],
                )
                await connection.execute("""
                    INSERT INTO daily_bars(
                        symbol, trade_date, open, high, low, close,
                        volume, turnover, change_percent, source
                    )
                    SELECT symbol, trade_date, open, high, low, close,
                           volume, turnover, change_percent, source
                    FROM tmp_daily_bars
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                        open=EXCLUDED.open, high=EXCLUDED.high,
                        low=EXCLUDED.low, close=EXCLUDED.close,
                        volume=EXCLUDED.volume, turnover=EXCLUDED.turnover,
                        change_percent=EXCLUDED.change_percent,
                        source=EXCLUDED.source, updated_at=NOW()
                """)
        return len(rows)

    async def bulk_upsert_indicators(
        self, rows: Sequence[DailyIndicator]
    ) -> int:
        if not rows:
            return 0
        fields = [
            "ma5", "ma10", "ma20", "ma60", "volume_ma5", "volume_ma20",
            "bollinger_mid", "bollinger_upper", "bollinger_lower",
            "volume_ratio", "volatility_20", "large_volume_low",
            "technical_score",
        ]
        records = [
            (r.symbol, r.trade_date, *[r.values.get(field) for field in fields])
            for r in rows
        ]
        async with self.database.acquire() as connection:
            async with connection.transaction():
                await connection.execute("""
                    CREATE TEMP TABLE IF NOT EXISTS tmp_daily_indicators (
                        symbol varchar(16), trade_date date,
                        ma5 numeric(14,4), ma10 numeric(14,4),
                        ma20 numeric(14,4), ma60 numeric(14,4),
                        volume_ma5 numeric(20,2), volume_ma20 numeric(20,2),
                        bollinger_mid numeric(14,4),
                        bollinger_upper numeric(14,4),
                        bollinger_lower numeric(14,4),
                        volume_ratio numeric(12,4),
                        volatility_20 numeric(12,6),
                        large_volume_low numeric(14,4),
                        technical_score numeric(8,4)
                    ) ON COMMIT DROP
                """)
                await connection.copy_records_to_table(
                    "tmp_daily_indicators",
                    records=records,
                    columns=["symbol", "trade_date", *fields],
                )
                assignments = ", ".join(
                    f"{field}=EXCLUDED.{field}" for field in fields
                )
                await connection.execute(f"""
                    INSERT INTO daily_indicators(symbol, trade_date, {", ".join(fields)})
                    SELECT symbol, trade_date, {", ".join(fields)}
                    FROM tmp_daily_indicators
                    ON CONFLICT(symbol, trade_date) DO UPDATE SET
                        {assignments}, updated_at=NOW()
                """)
        return len(rows)

    async def get_daily_bars(
        self, symbol: str, start_date: date | None = None,
        end_date: date | None = None, limit: int = 1500
    ) -> list[dict[str, Any]]:
        clauses = ["symbol=$1"]
        args: list[Any] = [symbol]
        if start_date is not None:
            args.append(start_date)
            clauses.append(f"trade_date>=${len(args)}")
        if end_date is not None:
            args.append(end_date)
            clauses.append(f"trade_date<=${len(args)}")
        args.append(max(1, min(limit, 5000)))
        query = f"""
            SELECT symbol, trade_date, open, high, low, close, volume,
                   turnover, change_percent, source
            FROM daily_bars
            WHERE {" AND ".join(clauses)}
            ORDER BY trade_date ASC
            LIMIT ${len(args)}
        """
        async with self.database.acquire() as connection:
            rows = await connection.fetch(query, *args)
        return [dict(row) for row in rows]

    async def get_recent_daily_bars_for_symbols(
        self,
        symbols: Sequence[str],
        limit_per_symbol: int = 61,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch each symbol's latest bars with one database round trip.

        Sixty-one bars are enough to reproduce the latest MA60, Bollinger,
        volume-ratio, 20-day volatility and large-volume-low values.
        """
        unique_symbols = list(dict.fromkeys(
            str(symbol).strip() for symbol in symbols if str(symbol).strip()
        ))
        if not unique_symbols:
            return {}

        limit_per_symbol = max(1, min(int(limit_per_symbol), 120))
        query = """
            WITH target_symbols AS (
                SELECT UNNEST($1::varchar[]) AS symbol
            )
            SELECT
                bars.symbol,
                bars.trade_date,
                bars.open,
                bars.high,
                bars.low,
                bars.close,
                bars.volume,
                bars.turnover,
                bars.change_percent,
                bars.source
            FROM target_symbols AS target
            CROSS JOIN LATERAL (
                SELECT
                    daily.symbol,
                    daily.trade_date,
                    daily.open,
                    daily.high,
                    daily.low,
                    daily.close,
                    daily.volume,
                    daily.turnover,
                    daily.change_percent,
                    daily.source
                FROM daily_bars AS daily
                WHERE daily.symbol = target.symbol
                ORDER BY daily.trade_date DESC
                LIMIT $2
            ) AS bars
            ORDER BY bars.symbol, bars.trade_date ASC
        """
        async with self.database.acquire() as connection:
            rows = await connection.fetch(
                query,
                unique_symbols,
                limit_per_symbol,
            )

        grouped = {symbol: [] for symbol in unique_symbols}
        for row in rows:
            grouped.setdefault(str(row["symbol"]), []).append(dict(row))
        return grouped

    async def get_symbols_with_daily_bars(self) -> list[str]:
        """Return active symbols that currently have at least one daily bar."""
        async with self.database.acquire() as connection:
            rows = await connection.fetch("""
                SELECT DISTINCT s.symbol
                FROM securities s
                JOIN daily_bars b ON b.symbol=s.symbol
                WHERE s.is_active=TRUE
                ORDER BY s.symbol
            """)
        return [str(row["symbol"]) for row in rows]

    async def get_latest_trade_date(self, symbol: str | None = None) -> date | None:
        async with self.database.acquire() as connection:
            if symbol:
                return await connection.fetchval(
                    "SELECT MAX(trade_date) FROM daily_bars WHERE symbol=$1", symbol
                )
            return await connection.fetchval("SELECT MAX(trade_date) FROM daily_bars")

    async def statistics(self) -> dict[str, Any]:
        async with self.database.acquire() as connection:
            row = await connection.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM securities) AS securities,
                    (SELECT COUNT(*) FROM daily_bars) AS daily_bars,
                    (SELECT COUNT(*) FROM daily_indicators) AS daily_indicators,
                    (SELECT COUNT(*) FROM radar_runs) AS radar_runs,
                    (SELECT COUNT(*) FROM radar_candidates) AS radar_candidates,
                    (SELECT MIN(trade_date) FROM daily_bars) AS first_date,
                    (SELECT MAX(trade_date) FROM daily_bars) AS latest_date
            """)
            size_bytes = int(await connection.fetchval(
                "SELECT pg_database_size(current_database())"
            ) or 0)
            size = await connection.fetchval(
                "SELECT pg_size_pretty(pg_database_size(current_database()))"
            )

        try:
            limit_bytes = max(1, int(os.getenv(
                "STOCK_DB_MAX_BYTES", str(1024 * 1024 * 1024)
            )))
        except (TypeError, ValueError):
            limit_bytes = 1024 * 1024 * 1024

        result = dict(row)
        result.update({
            "databaseSize": size,
            "databaseSizeBytes": size_bytes,
            "databaseLimitBytes": limit_bytes,
            "databaseUsagePercent": round(size_bytes / limit_bytes * 100, 2),
            "remainingMB": round(max(limit_bytes - size_bytes, 0) / 1024 / 1024, 2),
        })
        return result

    async def cleanup_old_data(
        self,
        retention_years: int = 3,
        radar_retention_days: int = 180,
        job_retention_days: int = 90,
        vacuum: bool = True,
    ) -> dict[str, Any]:
        """Delete expired rows and optionally run VACUUM ANALYZE."""
        retention_years = max(1, min(retention_years, 10))
        radar_retention_days = max(1, radar_retention_days)
        job_retention_days = max(1, job_retention_days)

        def affected(command_tag: str) -> int:
            try:
                return int(command_tag.rsplit(" ", 1)[-1])
            except (TypeError, ValueError):
                return 0

        async with self.database.acquire() as connection:
            async with connection.transaction():
                deleted_radar_candidates = affected(await connection.execute("""
                    DELETE FROM radar_candidates c
                    USING radar_runs r
                    WHERE c.radar_run_id=r.id
                      AND r.run_date < CURRENT_DATE
                          - ($1::int * INTERVAL '1 day')
                """, radar_retention_days))
                deleted_radar_runs = affected(await connection.execute("""
                    DELETE FROM radar_runs
                    WHERE run_date < CURRENT_DATE
                        - ($1::int * INTERVAL '1 day')
                """, radar_retention_days))
                deleted_indicators = affected(await connection.execute("""
                    DELETE FROM daily_indicators
                    WHERE trade_date < CURRENT_DATE
                        - ($1::int * INTERVAL '1 year')
                """, retention_years))
                deleted_bars = affected(await connection.execute("""
                    DELETE FROM daily_bars
                    WHERE trade_date < CURRENT_DATE
                        - ($1::int * INTERVAL '1 year')
                """, retention_years))
                deleted_jobs = affected(await connection.execute("""
                    DELETE FROM database_jobs
                    WHERE started_at < NOW()
                        - ($1::int * INTERVAL '1 day')
                """, job_retention_days))

            vacuum_errors: list[dict[str, str]] = []
            if vacuum:
                for table in (
                    "daily_bars",
                    "daily_indicators",
                    "radar_runs",
                    "radar_candidates",
                    "database_jobs",
                ):
                    try:
                        await connection.execute(f"VACUUM (ANALYZE) {table}")
                    except Exception as exc:
                        vacuum_errors.append({
                            "table": table,
                            "error": f"{type(exc).__name__}: {exc}",
                        })

        return {
            "ok": True,
            "retentionYears": retention_years,
            "radarRetentionDays": radar_retention_days,
            "jobRetentionDays": job_retention_days,
            "deleted": {
                "dailyBars": deleted_bars,
                "dailyIndicators": deleted_indicators,
                "radarRuns": deleted_radar_runs,
                "radarCandidates": deleted_radar_candidates,
                "databaseJobs": deleted_jobs,
            },
            "vacuumRequested": vacuum,
            "vacuumOk": not vacuum_errors,
            "vacuumErrors": vacuum_errors,
            "statistics": await self.statistics(),
        }

    async def start_job(
        self, job_type: str, trade_date: date | None = None,
        metadata: dict[str, Any] | None = None
    ) -> int:
        async with self.database.acquire() as connection:
            return int(await connection.fetchval("""
                INSERT INTO database_jobs(
                    job_type, trade_date, status, started_at, metadata
                ) VALUES($1, $2, 'running', NOW(), $3::jsonb)
                RETURNING id
            """, job_type, trade_date, json.dumps(metadata or {})))

    async def finish_job(
        self, job_id: int, processed: int, failed: int = 0,
        error: str | None = None
    ) -> None:
        status = "failed" if error else "completed"
        async with self.database.acquire() as connection:
            await connection.execute("""
                UPDATE database_jobs
                SET status=$2, finished_at=NOW(), processed_count=$3,
                    failed_count=$4, error_message=$5
                WHERE id=$1
            """, job_id, status, processed, failed, error)

    async def save_radar_run(
        self, strategy: str, run_date: date, candidates: Sequence[dict[str, Any]],
        configuration: dict[str, Any] | None = None,
        universe_count: int = 0
    ) -> int:
        async with self.database.acquire() as connection:
            async with connection.transaction():
                run_id = int(await connection.fetchval("""
                    INSERT INTO radar_runs(
                        strategy, run_date, status, universe_count,
                        candidate_count, configuration, finished_at
                    ) VALUES($1,$2,'completed',$3,$4,$5::jsonb,NOW())
                    RETURNING id
                """, strategy, run_date, universe_count, len(candidates),
                    json.dumps(configuration or {})))
                for rank, item in enumerate(candidates, start=1):
                    symbol = str(item.get("symbol") or item.get("stockNo") or "").strip()
                    if not symbol:
                        continue
                    await connection.execute("""
                        INSERT INTO radar_candidates(
                            radar_run_id, symbol, rank, total_score,
                            technical_score, chip_score, theme_score,
                            fundamental_score, risk_score, reasons, snapshot
                        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11::jsonb)
                        ON CONFLICT(radar_run_id, symbol) DO UPDATE SET
                            rank=EXCLUDED.rank, total_score=EXCLUDED.total_score,
                            snapshot=EXCLUDED.snapshot
                    """, run_id, symbol, rank,
                        _pick(item, "total_score", "totalScore", "score"),
                        _pick(item, "technical_score", "technicalScore"),
                        _pick(item, "chip_score", "chipScore"),
                        _pick(item, "theme_score", "themeScore"),
                        _pick(item, "fundamental_score", "fundamentalScore"),
                        _pick(item, "risk_score", "riskScore"),
                        json.dumps(item.get("reasons") or [], ensure_ascii=False),
                        json.dumps(item, ensure_ascii=False, default=str))
        return run_id


stock_repository = StockRepository()
