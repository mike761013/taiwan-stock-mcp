from __future__ import annotations
import asyncio, json
from stock_db.radar import run_full_bullish_radar
if __name__=="__main__":
    print(json.dumps(asyncio.run(run_full_bullish_radar()),
                     ensure_ascii=False,indent=2,default=str))
