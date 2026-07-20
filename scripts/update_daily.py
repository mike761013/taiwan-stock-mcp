from __future__ import annotations
import asyncio, json
from stock_db.pipeline import update_official_daily
if __name__=="__main__":
    print(json.dumps(asyncio.run(update_official_daily()),
                     ensure_ascii=False,indent=2,default=str))
