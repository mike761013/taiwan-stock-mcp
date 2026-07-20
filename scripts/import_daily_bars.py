from __future__ import annotations
import argparse
import asyncio
import json
from stock_db.importers import load_rows, row_to_daily_bar
from stock_db.models import Security
from stock_db.repository import stock_repository
from stock_db.service import stock_database_service

async def run(path: str, market: str) -> dict:
    init = await stock_database_service.initialize()
    if not init.get("ok"):
        return init
    source_rows = load_rows(path)
    bars = [row_to_daily_bar(row) for row in source_rows]
    symbols = sorted({bar.symbol for bar in bars})
    names = {}
    for row in source_rows:
        symbol = str(row.get("symbol") or row.get("stockNo") or row.get("code") or "")
        names[symbol] = str(row.get("name") or symbol)
    await stock_repository.upsert_securities(
        [Security(symbol=s, name=names.get(s, s), market=market) for s in symbols]
    )
    count = await stock_repository.bulk_upsert_daily_bars(bars)
    return {"ok": True, "imported": count, "symbols": len(symbols)}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--market", default="UNKNOWN")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.path, args.market)),
                     ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
