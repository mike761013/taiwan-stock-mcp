from __future__ import annotations
import argparse
import asyncio
import json
from stock_db.service import stock_database_service

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    args = parser.parse_args()
    result = asyncio.run(
        stock_database_service.calculate_symbol_indicators(args.symbol)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    main()
