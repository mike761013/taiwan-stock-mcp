"""Report stock-history coverage in PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from stock_db.connection import stock_database
from stock_db.service import stock_database_service


async def main() -> None:
    initialized = await stock_database_service.initialize()
    if not initialized.get("ok"):
        print(json.dumps(initialized, ensure_ascii=False, default=str))
        raise SystemExit(1)

    recent_cutoff = date.today() - timedelta(days=14)

    async with stock_database.acquire() as connection:
        row = await connection.fetchrow(
            """
            WITH per_symbol AS (
                SELECT
                    s.symbol,
                    COUNT(b.trade_date) AS bar_count,
                    MIN(b.trade_date) AS first_date,
                    MAX(b.trade_date) AS latest_date
                FROM securities s
                LEFT JOIN daily_bars b ON b.symbol = s.symbol
                WHERE s.is_active = TRUE
                GROUP BY s.symbol
            )
            SELECT
                COUNT(*) AS active_securities,
                COUNT(*) FILTER (WHERE bar_count > 0) AS symbols_with_bars,
                COUNT(*) FILTER (WHERE bar_count >= 200) AS symbols_with_200_bars,
                COUNT(*) FILTER (WHERE latest_date >= $1) AS recently_updated_symbols,
                COUNT(*) FILTER (WHERE bar_count = 0) AS symbols_without_bars,
                MIN(first_date) AS earliest_date,
                MAX(latest_date) AS latest_date
            FROM per_symbol
            """,
            recent_cutoff,
        )

        missing_rows = await connection.fetch(
            """
            SELECT s.symbol, s.name
            FROM securities s
            LEFT JOIN daily_bars b ON b.symbol = s.symbol
            WHERE s.is_active = TRUE
            GROUP BY s.symbol, s.name
            HAVING COUNT(b.trade_date) = 0
            ORDER BY s.symbol
            LIMIT 100
            """
        )

    result = dict(row)
    result["recentCutoff"] = recent_cutoff.isoformat()
    result["first100SymbolsWithoutBars"] = [dict(item) for item in missing_rows]
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
