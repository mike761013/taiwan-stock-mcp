"""Explicit bootstrap of whole-market evidence, separate from daily maintenance."""
from __future__ import annotations
import asyncio
import json
import re
from datetime import date
from html.parser import HTMLParser
import httpx
from .connection import stock_database
from .factors import ensure_factor_schema
from .advanced_factors import ensure_advanced_schema, parse_tdcc_csv, TDCC_URL
from .market_history import history_status

ARCHIVE = "https://raw.githubusercontent.com/wirelessr/tdcc-opendata-archive/main/snapshots"

class RevenueTable(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row=[]
        if tag in ("td", "th"): self.cell=[]
    def handle_data(self, value):
        if self.cell is not None: self.cell.append(value)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None: self.row.append("".join(self.cell).strip())
            self.cell=None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row); self.row=None

def parse_revenue_html(body, month, market):
    text=body.decode("big5", errors="replace") if isinstance(body,bytes) else body
    expected=f"{month.year-1911}年{month.month}月份"
    if expected not in text or "單位：千元" not in text:
        raise ValueError("Revenue month or unit mismatch")
    p=RevenueTable(); p.feed(text)
    if not any(r[:3]==["公司代號","公司名稱","當月營收"] for r in p.rows):
        raise ValueError("Revenue column schema mismatch")
    output=[]
    for r in p.rows:
        if len(r)<10 or not re.fullmatch(r"[1-9][0-9]{3}",r[0]): continue
        def num(s):
            try:return float(s.replace(",", ""))
            except ValueError:return None
        amount,previous,prior_year=map(num,r[2:5])
        if amount is None or amount<0: continue
        mom=(amount/previous-1)*100 if previous and previous>0 else None
        yoy=(amount/prior_year-1)*100 if prior_year and prior_year>0 else None
        output.append((r[0],month,amount*1000,mom,yoy,f"MOPS {market} monthly archive"))
    if not output: raise ValueError("No ordinary-stock revenue records")
    return output

def validate_ownership(body, requested_date):
    parsed=parse_tdcc_csv(body)
    if len(parsed)<1000 or any(v["snapshotDate"]!=requested_date.isoformat() for v in parsed.values()):
        raise ValueError("Ownership archive date/coverage mismatch")
    for value in parsed.values():
        small,large=value["under100LotsPercent"],value["over400LotsPercent"]
        if not (0<=small<=100 and 0<=large<=100 and small+large<=100.15):
            raise ValueError("Invalid ownership percentage")
    return parsed

def compare_official_archive(official, archived):
    common=set(official)&set(archived)
    if len(common)<1000: raise ValueError("Insufficient archive verification overlap")
    fields=("snapshotDate","under100LotsPercent","over400LotsPercent","holderCount")
    matched=sum(all(official[s].get(k)==archived[s].get(k) for k in fields) for s in common)
    if matched/len(common)<0.99: raise ValueError("Archive fails current official cross-check")
    return {"comparedSymbols":len(common),"matchedSymbols":matched}

async def bootstrap_market_evidence(revenue_months=3, ownership_weeks=3):
    """Bounded explicit bootstrap; no per-symbol requests or formal radar writes."""
    revenue_months=max(1,min(int(revenue_months),6))
    ownership_weeks=max(1,min(int(ownership_weeks),6))
    await ensure_factor_schema(); await ensure_advanced_schema()
    async with stock_database.acquire() as c:
        as_of=await c.fetchval("SELECT MAX(trade_date) FROM daily_bars")
        if not as_of:return {"ok":False,"error":"No daily close date"}
        symbols=set(r["symbol"] for r in await c.fetch("SELECT symbol FROM securities WHERE is_active AND symbol ~ '^[1-9][0-9]{3}$' AND UPPER(market) IN ('TWSE','TPEX','OTC')"))
        job_id=await c.fetchval("INSERT INTO database_jobs(job_type,trade_date,status,started_at,metadata) VALUES('market_evidence_bootstrap',$1,'running',NOW(),$2::jsonb) RETURNING id",as_of,json.dumps({"revenueMonths":revenue_months,"ownershipWeeks":ownership_weeks}))
    # Before the monthly deadline use the last fully published month.
    serial=as_of.year*12+as_of.month-1-(2 if as_of.day<10 else 1)
    months=[date((serial-i)//12,(serial-i)%12+1,1) for i in range(revenue_months)]
    errors=[]; revenue_rows=[]; ownership_rows=[]; requests=0; verification=None
    semaphore=asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=30,follow_redirects=True) as client:
        async def fetch_revenue(month,market,part):
            nonlocal requests
            url=f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{month.year-1911}_{month.month}_{part}.html"
            async with semaphore:
                requests+=1
                try:
                    response=await client.get(url); response.raise_for_status()
                    return [r for r in parse_revenue_html(response.content,month,market) if r[0] in symbols]
                except Exception as exc:
                    errors.append({"dataset":"revenue","month":str(month),"market":market,"part":part,"error":str(exc)[:180]}); return []
        revenue_batches=await asyncio.gather(*(fetch_revenue(m,market,part) for m in months for market in ("sii","otc") for part in (0,1)))
        revenue_rows=[r for batch in revenue_batches for r in batch]
        try:
            requests+=1; response=await client.get(TDCC_URL);response.raise_for_status()
            official=parse_tdcc_csv(response.content)
            latest=max(date.fromisoformat(v["snapshotDate"]) for v in official.values())
            if latest>as_of: raise ValueError("Official ownership date is after signal date")
            requests+=1; response=await client.get("https://api.github.com/repos/wirelessr/tdcc-opendata-archive/contents/snapshots/"+str(latest.year));response.raise_for_status()
            dates=sorted((date.fromisoformat(x["name"][:-4]) for x in response.json() if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.csv",x.get("name",""))),reverse=True)
            dates=[d for d in dates if d<=latest][:ownership_weeks]
            if not dates or dates[0]!=latest: raise ValueError("Latest official week absent from archive")
            staged=[]
            for d in dates:
                requests+=1; response=await client.get(f"{ARCHIVE}/{d.year}/{d}.csv");response.raise_for_status()
                parsed=validate_ownership(response.content,d)
                if d==latest: verification=compare_official_archive(official,parsed)
                for s,v in parsed.items():
                    if s in symbols: staged.append((s,d,v["under100LotsPercent"],v["over400LotsPercent"],v["holderCount"]))
            ownership_rows=staged
        except Exception as exc:
            errors.append({"dataset":"ownershipArchive","error":str(exc)[:240]})
    # Historical archive never overwrites an already captured official weekly row.
    async with stock_database.acquire() as c:
        async with c.transaction():
            if revenue_rows:
                await c.executemany("""INSERT INTO monthly_revenue(symbol,revenue_month,revenue,monthly_change_percent,yearly_change_percent,source)
                VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(symbol,revenue_month) DO UPDATE SET
                revenue=EXCLUDED.revenue,monthly_change_percent=EXCLUDED.monthly_change_percent,
                yearly_change_percent=EXCLUDED.yearly_change_percent,source=EXCLUDED.source,updated_at=NOW()
                WHERE monthly_revenue.source IS NULL OR monthly_revenue.source NOT LIKE 'MOPS%'""",revenue_rows)
                await c.execute("""WITH ranked AS (SELECT symbol,revenue_month,yearly_change_percent,
                LAG(yearly_change_percent) OVER(PARTITION BY symbol ORDER BY revenue_month) prior_yoy,
                LAG(revenue_month) OVER(PARTITION BY symbol ORDER BY revenue_month) prior_month FROM monthly_revenue)
                UPDATE monthly_revenue m SET yearly_acceleration_percent=CASE WHEN r.prior_month=(r.revenue_month-INTERVAL '1 month')::date
                THEN r.yearly_change_percent-r.prior_yoy ELSE NULL END FROM ranked r WHERE m.symbol=r.symbol AND m.revenue_month=r.revenue_month""")
            if ownership_rows:
                await c.executemany("""INSERT INTO tdcc_distribution_snapshots(symbol,snapshot_date,under_100_lots_percent,over_400_lots_percent,holder_count,source)
                VALUES($1,$2,$3,$4,$5,'TDCC official CSV / wirelessr archive') ON CONFLICT(symbol,snapshot_date) DO NOTHING""",ownership_rows)
            await c.execute("UPDATE database_jobs SET status=$2,finished_at=NOW(),processed_count=$3,failed_count=$4,error_message=$5 WHERE id=$1",job_id,"failed" if errors else "completed",len(revenue_rows)+len(ownership_rows),len(errors),"Source gaps" if errors else None)
    return {"ok":not errors,"jobId":job_id,"asOfDate":str(as_of),"requestCount":requests,
            "budgetPolicy":"EXPLICIT_WHOLE_MARKET_BOOTSTRAP_SEPARATE_FROM_DAILY_40",
            "revenueMonths":[str(m) for m in months],"revenueRowsPrepared":len(revenue_rows),
            "ownershipRowsPrepared":len(ownership_rows),"ownershipArchiveVerification":verification,
            "ownershipProvenance":"TDCC official CSV archived by wirelessr; current week cross-checked before historical import",
            "errors":errors,"coverage":await history_status()}
