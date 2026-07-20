from __future__ import annotations

import asyncio
import json

from stock_db.maintenance import run_daily_maintenance


if __name__ == "__main__":
    result = asyncio.run(run_daily_maintenance())
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
