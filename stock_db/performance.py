"""Radar-signal performance updater."""

from __future__ import annotations

from typing import Any

from .connection import stock_database


async def update_signal_performance(limit: int = 500) -> dict[str, Any]:
    limit = max(1, min(limit, 5000))
    async with stock_database.acquire() as connection:
        signals = await connection.fetch("""
            SELECT c.radar_run_id, c.symbol, r.run_date
            FROM radar_candidates c
            JOIN radar_runs r ON r.id=c.radar_run_id
            LEFT JOIN signal_performance p
              ON p.radar_run_id=c.radar_run_id AND p.symbol=c.symbol
            WHERE p.radar_run_id IS NULL OR p.return_d20 IS NULL
            ORDER BY r.run_date ASC
            LIMIT $1
        """, limit)
        processed = 0
        for signal in signals:
            bars = await connection.fetch("""
                SELECT trade_date, close, high, low
                FROM daily_bars
                WHERE symbol=$1 AND trade_date >= $2
                ORDER BY trade_date ASC
                LIMIT 21
            """, signal["symbol"], signal["run_date"])
            if not bars:
                continue
            entry = float(bars[0]["close"])
            def ret(index: int):
                if len(bars) <= index or not entry:
                    return None
                return round((float(bars[index]["close"]) / entry - 1) * 100, 4)
            highs = [float(row["high"]) for row in bars if row["high"] is not None]
            lows = [float(row["low"]) for row in bars if row["low"] is not None]
            await connection.execute("""
                INSERT INTO signal_performance(
                    radar_run_id, symbol, entry_date, entry_close,
                    return_d1, return_d3, return_d5, return_d10, return_d20,
                    max_favorable_percent, max_adverse_percent, calculated_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
                ON CONFLICT(radar_run_id, symbol) DO UPDATE SET
                    return_d1=EXCLUDED.return_d1,
                    return_d3=EXCLUDED.return_d3,
                    return_d5=EXCLUDED.return_d5,
                    return_d10=EXCLUDED.return_d10,
                    return_d20=EXCLUDED.return_d20,
                    max_favorable_percent=EXCLUDED.max_favorable_percent,
                    max_adverse_percent=EXCLUDED.max_adverse_percent,
                    calculated_at=NOW()
            """, signal["radar_run_id"], signal["symbol"],
                bars[0]["trade_date"], entry, ret(1), ret(3), ret(5),
                ret(10), ret(20),
                round((max(highs)/entry-1)*100, 4) if highs else None,
                round((min(lows)/entry-1)*100, 4) if lows else None)
            processed += 1
    return {"ok": True, "processed": processed}


async def performance_summary(strategy: str | None = None) -> dict[str, Any]:
    where = "WHERE r.strategy=$1" if strategy else ""
    args = [strategy] if strategy else []
    async with stock_database.acquire() as connection:
        row = await connection.fetchrow(f"""
            SELECT COUNT(*) AS samples,
              AVG(p.return_d1) AS avg_d1,
              AVG(p.return_d3) AS avg_d3,
              AVG(p.return_d5) AS avg_d5,
              AVG(p.return_d10) AS avg_d10,
              AVG(p.return_d20) AS avg_d20,
              AVG(CASE WHEN p.return_d5 > 0 THEN 1.0 ELSE 0.0 END)*100 AS win_d5,
              AVG(p.max_favorable_percent) AS avg_mfe,
              AVG(p.max_adverse_percent) AS avg_mae
            FROM signal_performance p
            JOIN radar_runs r ON r.id=p.radar_run_id
            {where}
        """, *args)
    return {"ok": True, "strategy": strategy, "summary": dict(row)}
