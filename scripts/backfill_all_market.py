from __future__ import annotations
import argparse, asyncio, json
from stock_db.pipeline import backfill_all_market
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--years",type=int,default=3)
    p.add_argument("--batch-size",type=int,default=50)
    p.add_argument("--start-after")
    a=p.parse_args()
    print(json.dumps(asyncio.run(backfill_all_market(a.years,a.batch_size,a.start_after)),
                     ensure_ascii=False,indent=2,default=str))
if __name__=="__main__": main()
