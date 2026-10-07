"""Resumable market-wide evidence; compact rows, shared request budget."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, timedelta

import httpx

from .connection import stock_database
from .factors import _finmind_rows, ensure_factor_schema
from .advanced_factors import fetch_tdcc_context, ensure_advanced_schema
from .prelaunch import cached_evidence, day, mapping
from .prelaunch_history import DAILY_REQUEST_CAP, revenue_history

MODEL = 'FULL-MARKET-HISTORY-1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_evidence_progress (
    task_key varchar(80) PRIMARY KEY,
    symbol varchar(16), dataset varchar(50) NOT NULL,
    evidence_date date, last_attempt date NOT NULL,
    last_success date, record_count integer NOT NULL DEFAULT 0,
    error_message text, updated_at timestamptz NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS institutional_daily_history (
    symbol varchar(16) NOT NULL, trade_date date NOT NULL,
    net_shares bigint NOT NULL, source varchar(80) NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol,trade_date)
);
'''


def official_institutional_rows(body, market, requested_date):
    """Reject stale responses and changed schemas; missing rows are not zero."""
    if market == 'TWSE':
        if day(body.get('date')) != requested_date:
            # TWSE uses YYYYMMDD rather than ISO.
            raw = str(body.get('date', ''))
            if raw != requested_date.strftime('%Y%m%d'):
                raise ValueError('TWSE date mismatch')
        tables = [body]
    else:
        tables = body.get('tables') or []
    result = {}
    for table in tables:
        if market == 'TPEX':
            raw = str(table.get('date', ''))
            expected = f'{requested_date.year-1911}/{requested_date.month:02}/{requested_date.day:02}'
            if raw != expected and raw != requested_date.strftime('%Y/%m/%d'):
                continue
        fields = table.get('fields') or []
        target = '三大法人買賣超股數' if market == 'TWSE' else '三大法人買賣超股數合計'
        if target not in fields:
            continue
        index = fields.index(target)
        for row in table.get('data') or []:
            symbol = str(row[0]).strip()
            if re.fullmatch(r'[1-9][0-9]{3}', symbol) and len(row) > index:
                value = str(row[index]).replace(',', '').strip()
                if re.fullmatch(r'-?\d+', value):
                    result[symbol] = int(value)
    if not result:
        raise ValueError('No dated common-stock institutional rows')
    return result


def plan_market_tasks(symbols, dates, progress, jobs, as_of, limit=30):
    """Persisted attempts allow resume without starving unvisited symbols."""
    used = sum(int(mapping(j.get('metadata')).get('requestCount') or 0)
               for j in jobs if day(j.get('trade_date')) == as_of)
    reserved = {t.get('key') or f"revenue:{t.get('symbol')}"
                for j in jobs if day(j.get('trade_date')) == as_of
                for t in mapping(j.get('metadata')).get('tasks', [])}
    old_revenue_attempts = {}
    for job in jobs:
        when = day(job.get('trade_date'))
        for task in mapping(job.get('metadata')).get('tasks', []):
            if task.get('dataset') == 'TaiwanStockMonthRevenue' and when:
                symbol = task.get('symbol')
                old_revenue_attempts[symbol] = max(old_revenue_attempts.get(symbol, date.min), when)
    tasks = []
    for d in dates:
        for market in ('TWSE', 'TPEX'):
            key = f'institutional:{market}:{d}'
            p = progress.get(key, {})
            if not p.get('last_success') and day(p.get('last_attempt')) != as_of and key not in reserved:
                tasks.append({'key':key,'dataset':'official_institutional','market':market,'date':str(d)})
    revenues = []
    for symbol in symbols:
        key = f'revenue:{symbol}'
        p = progress.get(key, {})
        attempt = day(p.get('last_attempt'))
        if key not in progress and old_revenue_attempts.get(symbol, date.min) > as_of-timedelta(days=7):
            continue
        success = day(p.get('last_success'))
        # Monthly history is refreshed weekly, failed requests cool down a day.
        if (success and success > as_of-timedelta(days=7)) or (attempt and attempt >= as_of) or key in reserved:
            continue
        revenues.append((success is not None, attempt or date.min, symbol,
                         {'key':key,'dataset':'TaiwanStockMonthRevenue','symbol':symbol}))
    revenues.sort(key=lambda r:r[:3])
    tasks.extend(r[3] for r in revenues[:max(1,min(int(limit),40))])
    return tasks[:max(0, DAILY_REQUEST_CAP-used)], used


async def history_status(as_of=None):
    await ensure_factor_schema()
    await ensure_advanced_schema()
    async with stock_database.acquire() as c:
        await c.execute(SCHEMA)
        as_of = as_of or await c.fetchval('SELECT MAX(trade_date) FROM daily_bars')
        symbols = [r['symbol'] for r in await c.fetch("""
            SELECT symbol FROM securities WHERE is_active AND symbol ~ '^[1-9][0-9]{3}$'
            AND UPPER(market) IN ('TWSE','TPEX','OTC') ORDER BY symbol
        """)]
        completed = await c.fetchval("""SELECT COUNT(*) FROM market_evidence_progress
            WHERE dataset='TaiwanStockMonthRevenue' AND last_success IS NOT NULL
            AND symbol=ANY($1::varchar[])""", symbols)
        failures = [dict(r) for r in await c.fetch("""SELECT task_key,error_message,last_attempt
            FROM market_evidence_progress WHERE error_message IS NOT NULL
            ORDER BY updated_at DESC LIMIT 20""")]
        dates = [str(r['snapshot_date']) for r in await c.fetch("""
            SELECT DISTINCT snapshot_date FROM tdcc_distribution_snapshots
            WHERE snapshot_date <= $1 ORDER BY snapshot_date DESC LIMIT 3
        """, as_of)] if as_of else []
        inst = await c.fetchval('SELECT COUNT(*) FROM institutional_daily_history')
    evidence = await cached_evidence(symbols, as_of) if as_of else {}
    ready_revenue = 0
    ready_ownership = 0
    ready_institutional = 0
    for e in evidence.values():
        months = [day(r['revenue_month']) for r in e['revenues'][:3]]
        if len(months)==3 and all(months) and all(r.get('yearly_change_percent') is not None for r in e['revenues'][:3]) and all((months[i].year*12+months[i].month)-(months[i+1].year*12+months[i+1].month)==1 for i in (0,1)) and (as_of-months[0]).days<=80:
            ready_revenue += 1
        weeks = [day(r['snapshot_date']) for r in e['ownership'][:3]]
        if len(weeks)==3 and all(weeks) and 0 <= (as_of-weeks[0]).days <= 10 and all(5 <= (weeks[i]-weeks[i+1]).days <= 9 for i in (0,1)):
            ready_ownership += 1
        daily = sorted(e.get('features', {}).get('chip', {}).get('institutionalDailyNetShares', []), key=lambda r:str(r['date']))[-5:]
        if len(daily)==5 and 0 <= (as_of-day(daily[-1]['date'])).days <= 4 and (day(daily[-1]['date'])-day(daily[0]['date'])).days <= 10:
            ready_institutional += 1
    return {'ok':True,'modelRevision':MODEL,'asOfDate':str(as_of),
            'universeCount':len(symbols),'revenueHistoryCompletedSymbols':completed,
            'threeRevenueMonthsReady':ready_revenue,'threeOwnershipWeeksReady':ready_ownership,
            'fiveInstitutionalSessionsReady':ready_institutional,
            'ownershipSnapshotDates':dates,'institutionalRowsStored':inst,
            'dailyRequestCap':DAILY_REQUEST_CAP,'recentErrors':failures,
            'note':'完成回補不等於符合營收加速或籌碼累積；缺資料不補零。'}


async def prepare_market_history(limit=30):
    """One durable batch. A DB advisory lock shares budget across processes."""
    await ensure_factor_schema()
    async with stock_database.acquire() as c:
        await c.execute(SCHEMA)
        as_of = await c.fetchval('SELECT MAX(trade_date) FROM daily_bars')
        if not as_of:
            return {'ok':False,'error':'No daily close date'}
        symbols = [r['symbol'] for r in await c.fetch("""SELECT symbol FROM securities
            WHERE is_active AND symbol ~ '^[1-9][0-9]{3}$' AND UPPER(market) IN ('TWSE','TPEX','OTC') ORDER BY symbol""")]
        dates = [r['trade_date'] for r in await c.fetch('SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date <= $1 ORDER BY trade_date DESC LIMIT 5',as_of)]
        async with c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(12440040)')
            progress = {r['task_key']:dict(r) for r in await c.fetch('SELECT * FROM market_evidence_progress')}
            jobs = [dict(r) for r in await c.fetch("SELECT trade_date,metadata FROM database_jobs WHERE job_type='prelaunch_history' AND trade_date >= $1",as_of-timedelta(days=7))]
            tasks,used = plan_market_tasks(symbols,dates,progress,jobs,as_of,limit)
            job_id = None
            if tasks:
                job_id = await c.fetchval("""INSERT INTO database_jobs(job_type,trade_date,status,started_at,metadata)
                    VALUES('prelaunch_history',$1,'running',NOW(),$2::jsonb) RETURNING id""",as_of,json.dumps({'modelRevision':MODEL,'requestCount':len(tasks),'tasks':tasks}))
                await c.executemany("""INSERT INTO market_evidence_progress(task_key,symbol,dataset,evidence_date,last_attempt)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT(task_key) DO UPDATE SET last_attempt=EXCLUDED.last_attempt,updated_at=NOW()""",
                    [(t['key'],t.get('symbol'),t['dataset'],day(t.get('date')),as_of) for t in tasks])
    # One downloaded weekly CSV already covers the market; the existing parser
    # persists all ordinary shares, not only the selected list.
    _, weekly = await fetch_tdcc_context(symbols, as_of)
    semaphore = asyncio.Semaphore(3)
    async def fetch(t):
        async with semaphore:
            try:
                if t['dataset']=='TaiwanStockMonthRevenue':
                    raw = await asyncio.wait_for(_finmind_rows(t['dataset'],t['symbol'],as_of-timedelta(days=550),as_of),20)
                    parsed = revenue_history(raw,as_of)
                    if not parsed: raise ValueError('No valid published revenue history')
                else:
                    d = day(t['date'])
                    if t['market']=='TWSE':
                        url='https://www.twse.com.tw/rwd/zh/fund/T86'
                        params={'date':d.strftime('%Y%m%d'),'selectType':'ALLBUT0999','response':'json'}
                    else:
                        url='https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade'
                        params={'date':d.strftime('%Y/%m/%d'),'type':'Daily','response':'json'}
                    async with httpx.AsyncClient(timeout=20,follow_redirects=True) as client:
                        response=await client.get(url,params=params)
                        response.raise_for_status()
                        parsed=official_institutional_rows(response.json(),t['market'],d)
                return t,parsed,None
            except Exception as exc:
                return t,None,f'{type(exc).__name__}: {exc}'[:400]
    results=await asyncio.gather(*(fetch(t) for t in tasks))
    errors=[]; revenue_rows=0; inst_rows=0
    async with stock_database.acquire() as c:
        for t,parsed,error in results:
            count=0
            async with c.transaction():
                if error:
                    errors.append({'task':t['key'],'error':error})
                elif t['dataset']=='TaiwanStockMonthRevenue':
                    await c.executemany("""INSERT INTO monthly_revenue(symbol,revenue_month,revenue,monthly_change_percent,yearly_change_percent,yearly_acceleration_percent,source)
                        VALUES($1,$2,$3,$4,$5,$6,'FinMind prelaunch history') ON CONFLICT(symbol,revenue_month) DO UPDATE SET
                        revenue=EXCLUDED.revenue,monthly_change_percent=EXCLUDED.monthly_change_percent,
                        yearly_change_percent=EXCLUDED.yearly_change_percent,yearly_acceleration_percent=EXCLUDED.yearly_acceleration_percent,updated_at=NOW()
                        WHERE monthly_revenue.source='FinMind prelaunch history'""",
                        [(t['symbol'],r['month'],r['revenue'],r['mom'],r['yoy'],r['acceleration']) for r in parsed])
                    count=len(parsed);revenue_rows+=count
                else:
                    records=[(s,day(t['date']),n,f"{t['market']} official daily") for s,n in parsed.items() if s in symbols]
                    await c.executemany("""INSERT INTO institutional_daily_history(symbol,trade_date,net_shares,source)
                        VALUES($1,$2,$3,$4) ON CONFLICT(symbol,trade_date) DO UPDATE SET net_shares=EXCLUDED.net_shares,source=EXCLUDED.source,captured_at=NOW()""",records)
                    count=len(records);inst_rows+=count
                await c.execute("""UPDATE market_evidence_progress SET last_success=CASE WHEN $2::text IS NULL THEN $3 ELSE last_success END,
                    record_count=CASE WHEN $2::text IS NULL THEN $4 ELSE record_count END,error_message=$2,updated_at=NOW() WHERE task_key=$1""",t['key'],error,as_of,count)
        if job_id:
            await c.execute("""UPDATE database_jobs SET status=$2,finished_at=NOW(),processed_count=$3,failed_count=$4,error_message=$5 WHERE id=$1""",
                            job_id,'failed' if errors else 'completed',len(tasks)-len(errors),len(errors),'Source gaps' if errors else None)
        # Only recent daily evidence is required for this lane; keep storage
        # bounded independently of the existing daily-K retention policy.
        await c.execute("DELETE FROM institutional_daily_history WHERE trade_date < $1",as_of-timedelta(days=90))
        await c.execute("DELETE FROM market_evidence_progress WHERE dataset='official_institutional' AND evidence_date < $1",as_of-timedelta(days=90))
    return {'ok':not errors,'modelRevision':MODEL,'latestTradeDate':str(as_of),'jobId':job_id,
            'requestedDatasets':len(tasks),'dailyRequestsAlreadyReserved':used,
            'dailyRequestCap':DAILY_REQUEST_CAP,'remainingRequestsToday':max(0,DAILY_REQUEST_CAP-used-len(tasks)),
            'revenueRowsPrepared':revenue_rows,'institutionalRowsPrepared':inst_rows,
            'weeklyOwnership':weekly,'errors':errors,'coverage':await history_status(as_of)}
