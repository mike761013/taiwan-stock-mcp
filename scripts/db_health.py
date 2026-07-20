from __future__ import annotations
import asyncio
import json
from stock_db.service import stock_database_service

async def main() -> None:
    print(json.dumps(await stock_database_service.health(),
                     ensure_ascii=False, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(main())
