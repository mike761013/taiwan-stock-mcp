from __future__ import annotations
import asyncio, json
from stock_db.performance import update_signal_performance
if __name__=="__main__":
    print(json.dumps(asyncio.run(update_signal_performance()),
                     ensure_ascii=False,indent=2,default=str))
