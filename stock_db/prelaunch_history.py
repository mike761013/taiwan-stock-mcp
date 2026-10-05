"""Bounded evidence bootstrap for technically ready early bases.

At most 40 dataset requests per trade date across repeated invocations. Revenue
history retries at most weekly; institutional history once per trade date.
Existing official revenue values and enriched factor features are preserved.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from .connection import stock_database
from .factors import _finmind_rows
from .prelaunch import cached_evidence, evaluate_prelaunch, number, day, mapping
from .repository import stock_repository

_LOCK = asyncio.Lock()
DAILY_REQUEST_CAP = 40


def revenue_history(records, as_of):
    values = {}
    for r in records:
        published = day(r.get('create_time')) or day(r.get('date'))
        try:
            month = date(int(r['revenue_year']), int(r['revenue_month']), 1)
        except (KeyError, ValueError, TypeError):
            continue
        value = number(r.get('revenue'))
        if month <= as_of and published and published <= as_of and value is not None and value >= 0:
            values[month] = value
    output = []
    for month, value in sorted(values.items()):
        previous_year = values.get(date(month.year-1, month.month, 1))
        previous_month = date(month.year-1, 12, 1) if month.month == 1 else date(month.year, month.month-1, 1)
        previous_value = values.get(previous_month)
        output.append(dict(month=month, revenue=value,
                           yoy=(value/previous_year-1)*100 if previous_year and previous_year>0 else None,
                           mom=(value/previous_value-1)*100 if previous_value and previous_value>0 else None))
    by_month = {r['month']:r for r in output}
    for r in output:
        month=r['month']
        previous = date(month.year-1,12,1) if month.month==1 else date(month.year,month.month-1,1)
        py = (by_month.get(previous) or {}).get('yoy')
        r['acceleration'] = r['yoy']-py if r['yoy'] is not None and py is not None else None
    return output


def institutional_history(records, as_of):
    totals = {}
    for r in records:
        d = day(r.get('date'))
        buy, sell = number(r.get('buy')), number(r.get('sell'))
        if d and as_of-timedelta(days=14) <= d <= as_of and buy is not None and sell is not None:
            totals[str(d)] = totals.get(str(d),0)+buy-sell
    return [{'date':d,'netShares':round(n)} for d,n in sorted(totals.items())]


def request_plan(ready, jobs, as_of, limit):
    requests = []
    used = 0
    attempted = {}
    for job in jobs:
        meta = mapping(job.get('metadata'))
        when = day(job.get('trade_date'))
        if when == as_of:
            used += int(meta.get('requestCount') or 0)
        for task in meta.get('tasks') or []:
            key = (task['symbol'], task['dataset'])
            attempted[key] = max(attempted.get(key,date.min),when or date.min)
    remaining=max(0,DAILY_REQUEST_CAP-used)
    selected = 0
    for row,p in ready:
        symbol=str(row['symbol'])
        needed=[]
        if 'recentThreeConsecutiveRevenueMonths' in p['missingInputs']:
            dataset='TaiwanStockMonthRevenue'
            if attempted.get((symbol,dataset),date.min)<as_of-timedelta(days=6):
                needed.append({'symbol':symbol,'dataset':dataset})
        if 'fiveRecentInstitutionalSessions' in p['missingInputs']:
            dataset='TaiwanStockInstitutionalInvestorsBuySell'
            if attempted.get((symbol,dataset),date.min)<as_of:
                needed.append({'symbol':symbol,'dataset':dataset})
        if needed and len(requests)+len(needed)<=remaining:
            requests.extend(needed)
            selected += 1
            if selected >= max(1,min(int(limit),20)):
                break
    return requests,used


async def prepare_prelaunch_history(rows,profiles,config,as_of,limit=20):
    if not rows or as_of is None:
        return {'ok':True,'requestedDatasets':0,'dailyRequestCap':DAILY_REQUEST_CAP}
    async with _LOCK:
        evidence=await cached_evidence([str(r['symbol']) for r in rows],as_of)
        ready=[]
        for row in rows:
            symbol=str(row['symbol'])
            p=evaluate_prelaunch(row,evidence.get(symbol,{}),profiles.get(symbol,{}),config)
            # Only use price/liquidity/sector gates to nominate missing evidence;
            # do not require a prior bullish shortlist or fabricate fundamentals.
            if set(p['failedRules']) <= {'fundamentalAcceleration','persistentAccumulation'}:
                ready.append((row,p))
        ready.sort(key=lambda pair:(pair[1]['sectorAboveMA20Percent'] or 0,
                                    -(number(pair[0].get('volume_ratio')) or 0)),reverse=True)
        async with stock_database.acquire() as connection:
            jobs=[dict(r) for r in await connection.fetch('''
                SELECT trade_date,metadata FROM database_jobs
                WHERE job_type='prelaunch_history' AND trade_date >= $1
            ''',as_of-timedelta(days=7))]
        tasks,used=request_plan(ready,jobs,as_of,limit)
        if not tasks:
            return {'ok':True,'requestedDatasets':0,'dailyRequestCap':DAILY_REQUEST_CAP,
                    'dailyRequestsAlreadyReserved':used,'baseReadyCount':len(ready),
                    'reason':'快取足夠、已嘗試或達每日上限'}
        job_id=await stock_repository.start_job('prelaunch_history',as_of,
                                                {'requestCount':len(tasks),'tasks':tasks})
        semaphore=asyncio.Semaphore(3)
        async def fetch(task):
            async with semaphore:
                try:
                    start=as_of-timedelta(days=550 if task['dataset']=='TaiwanStockMonthRevenue' else 14)
                    records=await asyncio.wait_for(
                        _finmind_rows(task['dataset'],task['symbol'],start,as_of),timeout=15)
                    if not records:
                        return task,[], '資料未回傳或Token缺少'
                    return task,records,None
                except Exception as exc:
                    return task,[],f'{type(exc).__name__}: {exc}'
        results=await asyncio.gather(*(fetch(task) for task in tasks))
        errors=[];revenue_rows=0;institutional_symbols=0
        async with stock_database.acquire() as connection:
            for task,records,error in results:
                symbol=task['symbol']
                if error:
                    errors.append({'symbol':symbol,'dataset':task['dataset'],'error':error})
                    continue
                if task['dataset']=='TaiwanStockMonthRevenue':
                    history=revenue_history(records,as_of)
                    if history:
                        await connection.executemany('''
                            INSERT INTO monthly_revenue(symbol,revenue_month,revenue,
                              monthly_change_percent,yearly_change_percent,yearly_acceleration_percent,source)
                            VALUES($1,$2,$3,$4,$5,$6,'FinMind prelaunch history')
                            ON CONFLICT(symbol,revenue_month) DO UPDATE SET
                              revenue=EXCLUDED.revenue,monthly_change_percent=EXCLUDED.monthly_change_percent,
                              yearly_change_percent=EXCLUDED.yearly_change_percent,
                              yearly_acceleration_percent=EXCLUDED.yearly_acceleration_percent,updated_at=NOW()
                            WHERE monthly_revenue.source='FinMind prelaunch history'
                        ''',[(symbol,r['month'],r['revenue'],r['mom'],r['yoy'],r['acceleration']) for r in history])
                        revenue_rows+=len(history)
                else:
                    daily=institutional_history(records,as_of)
                    if daily:
                        chip={'institutionalDailyNetShares':daily,'institutionalAsOfDate':daily[-1]['date'],
                              'institutionalHistorySource':'FinMind bounded prelaunch history'}
                        await connection.execute('''
                            INSERT INTO daily_factor_snapshots(symbol,trade_date,features)
                            VALUES($1,$2,$3::jsonb)
                            ON CONFLICT(symbol,trade_date) DO UPDATE SET
                              features=jsonb_set(daily_factor_snapshots.features,'{chip}',
                                COALESCE(daily_factor_snapshots.features->'chip','{}'::jsonb) ||
                                (EXCLUDED.features->'chip')),updated_at=NOW()
                        ''',symbol,as_of,json.dumps({'chip':chip}))
                        institutional_symbols+=1
        await stock_repository.finish_job(job_id,len(results)-len(errors),len(errors),
                                          '部分歷史來源缺資料' if errors else None)
        return {'ok':not errors,'requestedDatasets':len(tasks),'dailyRequestCap':DAILY_REQUEST_CAP,
                'dailyRequestsAlreadyReserved':used,'baseReadyCount':len(ready),
                'revenueRowsPrepared':revenue_rows,'institutionalSymbolsPrepared':institutional_symbols,
                'errors':errors,'jobId':job_id}
