import asyncio
from copy import deepcopy
from datetime import date, timedelta

from stock_db import radar, prelaunch
from stock_db.prelaunch import evaluate_prelaunch, screen_prelaunch, observation_metrics
from stock_db.v12 import V12Config, build_trading_plan
from stock_db.performance import simulate_signal_execution


def base():
    row = dict(symbol='1101',name='base',market='TWSE',trade_date='2026-10-05',
               close=50,open=49.9,high=50.1,low=49.7,prev_close=50,
               ma5=50,ma10=49.8,ma20=49,prev_ma20=48.9,ma60=48,
               volume=1500000,volume_ma20=2000000,turnover=75000000,
               volume_ratio=.75,prior_high20=51,atr14=1)
    evidence = dict(revenues=[dict(revenue_month=f'2026-{m:02}-01',yearly_change_percent=y)
                             for m,y in [(8,25),(7,18),(6,12)]],
                    ownership=[dict(snapshot_date=d,over_400_lots_percent=l,under_100_lots_percent=s)
                               for d,l,s in [('2026-10-02',50,30),('2026-09-25',49,31),('2026-09-18',48,32)]],
                    features={})
    profile = {'returns':{'10':3,'20':6},'sectorContext':{'aboveMA20Percent':60},
               'sectorResonanceScore':60}
    return row,evidence,profile


def test_independent_lane_accepts_unextended_base_without_strength_eligibility():
    row,e,p=base()
    p['eligible']=False
    board=screen_prelaunch([row],{row['symbol']:e},{row['symbol']:p},V12Config())
    c=board['candidates'][0]
    assert c['actionCode']=='EARLY_WATCH_ONLY' and c['forwardQualified'] is False
    assert 'tradingPlan' not in c
    assert c['confirmationTrigger']>row['prior_high20']


def test_single_month_or_single_ownership_week_never_substitutes_for_continuity():
    row,e,p=base()
    e['revenues']=e['revenues'][:1]
    e['ownership']=e['ownership'][:1]
    result=evaluate_prelaunch(row,e,p,V12Config())
    assert not result['eligible']
    assert set(result['failedRules']) >= {'fundamentalAcceleration','persistentAccumulation'}
    assert 'recentThreeConsecutiveRevenueMonths' in result['missingInputs']


def test_skipped_month_and_old_ownership_rejected():
    row,e,p=base()
    e['revenues'][1]['revenue_month']='2026-06-01'
    e['ownership'][0]['snapshot_date']='2026-09-12'
    assert not evaluate_prelaunch(row,e,p,V12Config())['eligible']


def test_future_institutional_data_does_not_create_accumulation():
    row,e,p=base()
    e['ownership']=[]
    e['features']={'chip':{'institutionalDailyNetShares':[
        {'date':f'2026-10-{d:02}','netShares':1000} for d in range(6,11)]}}
    assert not evaluate_prelaunch(row,e,p,V12Config())['eligible']


def test_five_session_accumulation_is_alternative_to_three_week_ownership():
    row,e,p=base()
    e['ownership']=[]
    e['features']={'chip':{'institutionalDailyNetShares':[
        {'date':d,'netShares':n} for d,n in [('2026-09-28',100),('2026-09-29',-50),
            ('2026-09-30',100),('2026-10-01',100),('2026-10-02',100)]]}}
    result=evaluate_prelaunch(row,e,p,V12Config())
    assert result['eligible'] and result['institutionalAccumulation']
    assert 'threeWeeklyOwnershipSnapshots' in result['missingInputs']


def test_extended_stock_weak_sector_and_known_hard_risk_excluded():
    row,e,p=base()
    for changes in ({'close':58},{'close':205}):
        assert not evaluate_prelaunch({**row,**changes},e,p,V12Config())['eligible']
    p['sectorContext']['aboveMA20Percent']=40
    assert not evaluate_prelaunch(row,e,p,V12Config())['eligible']
    p['sectorContext']['aboveMA20Percent']=60
    e['features']={'event':{'hardRisk':True}}
    assert not evaluate_prelaunch(row,e,p,V12Config())['eligible']


def test_observation_does_not_use_signal_day_or_mark_pending_as_failure():
    s={'trade_date':'2026-10-05','observationReferencePrice':50,'confirmationTrigger':51}
    b=[dict(trade_date='2026-10-05',close=60,high=61,low=49,volume_ratio=2),
       dict(trade_date='2026-10-06',close=51.1,high=52,low=49.5,volume_ratio=1)]
    m=observation_metrics(s,b)
    assert m['observedSessions']==1 and m['sessionsToConfirmedBreakout'] is None
    assert m['noBreakoutAfter20Sessions'] is None
    b.append(dict(trade_date='2026-10-07',close=51.2,high=52,low=49.8,volume_ratio=1.3))
    assert observation_metrics(s,b)['sessionsToConfirmedBreakout']==2


def test_twenty_session_false_signal_and_drawdown_recorded_separately():
    s={'trade_date':'2026-10-05','observationReferencePrice':50,'confirmationTrigger':51}
    b=[dict(trade_date=date(2026,10,6)+timedelta(days=i),close=49,high=50,low=45,volume_ratio=1)
       for i in range(20)]
    m=observation_metrics(s,b)
    assert m['status']=='MATURED' and m['noBreakoutAfter20Sessions'] is True
    assert m['maximumAdversePercent']==-10
    assert m['returns']['20']==-2


def test_confirmation_above_buy_cap_is_explicitly_disabled_without_moving_stop():
    row=dict(trade_date='2026-08-12',open=90,high=90,low=82.9,close=83.8,
             prev_high=86.5,ma5=85.98,ma20=77.42,large_volume_low=78,atr14=3.5)
    plan=build_trading_plan(row,'pullback',V12Config())
    assert plan['confirmationEntry']['price']>plan['maximumBuyPrice']
    assert plan['confirmationEntry']['executable'] is False
    assert plan['failureCondition']['price']==82.9
    assert plan['confirmationEntry']['actionWhenUnavailable']


def test_legacy_plan_cannot_fill_confirmation_above_buy_cap():
    s={'actionCode':'BUY_ZONE','tradingPlan':{'maximumBuyPrice':101,'noChasePrice':106,
        'statusCode':'BUY_ZONE','aggressiveEntry':{'entryLow':98,'entryHigh':99,'positionPercent':0},
        'confirmationEntry':{'price':103,'positionPercent':60,'availableBelowNoChase':True},
        'failureCondition':{'price':96}}}
    bars=[dict(trade_date=date(2026,10,6),open=102,high=104,low=101,close=103.5)]
    r=simulate_signal_execution(s,bars)
    assert r['confirmation_fill_date'] is None and r['filled_position_percent']==0


def test_full_radar_persists_separate_watch_lane_and_combined_snapshot(monkeypatch):
    row,e,p=base()
    async def snapshot(): return [row],1,date(2026,10,5)
    async def evidence(*args): return {row['symbol']:e}
    async def performance(*args): return {'samples':1}
    async def priors(**kwargs): return {}
    async def enrich(items,*args): return items
    async def prepare(*args): return {'ok':True,'requestedDatasets':0}
    saved=[]
    async def save(**kwargs):
        saved.append(kwargs)
        return {'radarRunId':len(saved)}
    monkeypatch.setattr(radar,'_fetch_v12_snapshot',snapshot)
    monkeypatch.setattr(radar,'cached_evidence',evidence)
    monkeypatch.setattr(radar,'prelaunch_performance',performance)
    monkeypatch.setattr(radar,'execution_strategy_priors',priors)
    monkeypatch.setattr(radar,'enrich_candidates_v12_3',enrich)
    monkeypatch.setattr(radar,'prepare_prelaunch_history',prepare)
    monkeypatch.setattr(radar,'build_strength_profiles',lambda *args:{row['symbol']:p})
    monkeypatch.setattr(radar,'screen_v12_rows',lambda **kwargs:([],{}))
    monkeypatch.setattr(radar.stock_database_service,'save_radar_result',save)
    r=asyncio.run(radar.run_full_bullish_radar_v12(save_result=True))
    early=next(s for s in saved if s['strategy']=='prelaunch_watch')
    combined=next(s for s in saved if s['strategy']=='v12_combined')
    assert len(early['candidates'])==1 and combined['candidates']==[]
    assert combined['configuration']['prelaunchWatch']['candidates']==early['candidates']
    assert r['actionableCandidateCount']==0 and r['prelaunchWatch']['candidateCount']==1


def test_actual_confirmation_fill_cannot_exceed_cap_even_when_trigger_is_valid():
    s={'actionCode':'BUY_ZONE','tradingPlan':{'maximumBuyPrice':104,'noChasePrice':110,
        'statusCode':'BUY_ZONE','aggressiveEntry':{'entryLow':98,'entryHigh':99,'positionPercent':0},
        'confirmationEntry':{'price':103,'positionPercent':60,'availableBelowNoChase':True},
        'failureCondition':{'price':96}}}
    bars=[dict(trade_date=date(2026,10,6),open=102,high=106,low=101,close=105)]
    r=simulate_signal_execution(s,bars)
    assert r['confirmation_fill_date'] is None and r['filled_position_percent']==0


def test_chip_daily_evidence_aggregates_all_categories_and_rejects_future(monkeypatch):
    from stock_db import factors
    async def provider(dataset,*args):
        if dataset!='TaiwanStockInstitutionalInvestorsBuySell': return []
        return [dict(date='2026-10-02',buy=100,sell=20),
                dict(date='2026-10-02',buy=50,sell=30),
                dict(date='2026-10-06',buy=10000,sell=0)]
    monkeypatch.setattr(factors,'_finmind_rows',provider)
    _,features=asyncio.run(factors._chip_factor('1101',date(2026,10,5)))
    assert features['institutionalDailyNetShares']==[{'date':'2026-10-02','netShares':100}]
    assert features['institutionalAsOfDate']=='2026-10-02'


def test_tdcc_preserves_unselected_common_shares_without_extra_download(monkeypatch):
    from stock_db import advanced_factors as af
    from datetime import datetime
    class Context:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def executemany(self,sql,rows): saved.extend(rows)
        async def fetch(self,*args): return []
    saved=[]
    data={s:dict(snapshotDate='2026-10-02',under100LotsPercent=30,over400LotsPercent=50)
          for s in ('1101','1102','0050')}
    monkeypatch.setitem(af._TDCC_CACHE,'data',data)
    monkeypatch.setitem(af._TDCC_CACHE,'expires',datetime.now(af.TAIPEI_TZ)+timedelta(days=1))
    monkeypatch.setattr(af.stock_database,'acquire',lambda:Context())
    async def ensure(): return None
    monkeypatch.setattr(af,'ensure_advanced_schema',ensure)
    selected,_=asyncio.run(af.fetch_tdcc_context(['1101'],date(2026,10,5)))
    assert set(selected)=={'1101'}
    assert {r[0] for r in saved}=={'1101','1102'}


def test_revenue_backfill_calculates_yoy_from_prior_year_and_excludes_future_publication():
    from stock_db.prelaunch_history import revenue_history
    records=[dict(date='2025-09-01',revenue_year=2025,revenue_month=8,revenue=100),
             dict(date='2026-09-01',create_time='2026-09-08',revenue_year=2026,revenue_month=8,revenue=125),
             dict(date='2026-10-01',create_time='2026-10-10',revenue_year=2026,revenue_month=9,revenue=200)]
    result=revenue_history(records,date(2026,10,5))
    assert result[-1]['month']==date(2026,8,1) and result[-1]['yoy']==25
    assert result[0]['yoy'] is None


def test_bounded_backfill_reuses_attempts_and_has_global_daily_cap():
    from stock_db.prelaunch_history import request_plan
    ready=[({'symbol':str(1100+i)},{'missingInputs':['recentThreeConsecutiveRevenueMonths','fiveRecentInstitutionalSessions']}) for i in range(25)]
    tasks,used=request_plan(ready,[],date(2026,10,5),100)
    assert len(tasks)==40 and used==0
    jobs=[{'trade_date':'2026-10-05','metadata':{'requestCount':40,'tasks':tasks}}]
    assert request_plan(ready,jobs,date(2026,10,5),20)[0]==[]
    next_day,used=request_plan(ready,jobs,date(2026,10,6),20)
    assert len(next_day)==20 and all(t['dataset']=='TaiwanStockInstitutionalInvestorsBuySell' for t in next_day)


def test_backfill_batches_do_not_get_stuck_on_already_prepared_leaders():
    from stock_db.prelaunch_history import request_plan
    ready=[({'symbol':str(1100+i)},{'missingInputs':[] if i<20 else ['recentThreeConsecutiveRevenueMonths']}) for i in range(25)]
    tasks,_=request_plan(ready,[],date(2026,10,5),20)
    assert {t['symbol'] for t in tasks}=={str(1120+i) for i in range(5)}


def test_history_preparation_writes_compact_evidence_and_does_not_repeat_requests(monkeypatch):
    from stock_db import prelaunch_history as history
    row,_,p=base()
    async def evidence(*args): return {}
    jobs=[];queries=[];calls=[]
    class Context:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def fetch(self,*args): return jobs
        async def executemany(self,sql,rows):
            assert "WHERE monthly_revenue.source='FinMind prelaunch history'" in sql
            queries.append(('revenue',rows))
        async def execute(self,sql,*args):
            assert "jsonb_set" in sql and 'COALESCE(daily_factor_snapshots.features' in sql
            queries.append(('chip',args))
    async def start(kind,as_of,metadata):
        jobs.append({'trade_date':as_of,'metadata':metadata});return 1
    async def finish(*args): pass
    async def provider(dataset,symbol,start,end):
        calls.append(dataset)
        if dataset=='TaiwanStockMonthRevenue':
            return [dict(date=f'{y}-09-01',create_time=f'{y}-09-08',revenue_year=y,revenue_month=8,revenue=r)
                    for y,r in [(2025,100),(2026,125)]]
        return [dict(date=d,buy=100,sell=10) for d in ('2026-09-28','2026-09-29','2026-09-30','2026-10-01','2026-10-02')]
    monkeypatch.setattr(history,'cached_evidence',evidence)
    monkeypatch.setattr(history.stock_database,'acquire',lambda:Context())
    monkeypatch.setattr(history.stock_repository,'start_job',start)
    monkeypatch.setattr(history.stock_repository,'finish_job',finish)
    monkeypatch.setattr(history,'_finmind_rows',provider)
    result=asyncio.run(history.prepare_prelaunch_history([row],{row['symbol']:p},V12Config(),date(2026,10,5)))
    assert result['requestedDatasets']==2 and result['institutionalSymbolsPrepared']==1
    assert result['revenueRowsPrepared']==2 and len(queries)==2
    again=asyncio.run(history.prepare_prelaunch_history([row],{row['symbol']:p},V12Config(),date(2026,10,5)))
    assert again['requestedDatasets']==0 and len(calls)==2
