from datetime import date, timedelta
import json
from pathlib import Path
import asyncio

import pytest

from stock_db.market_history import official_institutional_rows, plan_market_tasks


def test_official_parser_checks_requested_date_and_uses_named_total():
    d=date(2026,10,7)
    twse={'date':'20261007','fields':['代號','外資','三大法人買賣超股數'],
          'data':[['2429','100','-21,000'],['0050','100','200'],['2303','100','0']]}
    assert official_institutional_rows(twse,'TWSE',d)=={'2429':-21000,'2303':0}
    with pytest.raises(ValueError):
        official_institutional_rows(twse,'TWSE',d-timedelta(days=1))
    tpex={'tables':[{'date':'115/10/07','fields':['代號','三大法人買賣超股數合計'],
                     'data':[['3707','4,739,278']]}]}
    assert official_institutional_rows(tpex,'TPEX',d)=={'3707':4739278}
    with pytest.raises(ValueError):
        official_institutional_rows(tpex,'TPEX',d-timedelta(days=1))


def test_real_official_response_layouts():
    # Live captured response fixtures exercise the actual provider layouts.
    root=Path(__file__).parent/'fixtures'/'market_history'
    for market in ('TWSE','TPEX'):
        body=json.loads((root/f'{market.lower()}.json').read_text())
        rows=official_institutional_rows(body,market,date(2026,10,7))
        assert rows
    assert official_institutional_rows(json.loads((root/'twse.json').read_text()),'TWSE',date(2026,10,7))['2429']==-21000


def test_global_budget_includes_old_candidate_jobs_and_resumes_next_day():
    d=date(2026,10,7)
    jobs=[{'trade_date':d,'metadata':{'requestCount':32,'tasks':[]}}]
    tasks,used=plan_market_tasks(['1101','1102'],[d-timedelta(days=i) for i in range(5)],{},jobs,d)
    assert used==32 and len(tasks)==8
    assert all(t['dataset']=='official_institutional' for t in tasks)
    # A second caller sees the reservation even before provider writes finish.
    jobs.append({'trade_date':d,'metadata':{'requestCount':8,'tasks':tasks}})
    assert plan_market_tasks(['1101'],[d],{},jobs,d)[0]==[]
    progress={t['key']:{'last_attempt':d,'last_success':d} for t in tasks}
    resumed,_=plan_market_tasks(['1101','1102'],[d],progress,jobs,d+timedelta(days=1))
    assert [t['symbol'] for t in resumed]==['1101','1102']


def test_persistent_queue_reaches_non_candidates_and_does_not_starve_unvisited():
    d=date(2026,10,7)
    progress={'revenue:1101':{'last_attempt':d-timedelta(days=10),'last_success':d-timedelta(days=10)},
              'revenue:1102':{'last_attempt':d,'error_message':'timeout'}}
    tasks,_=plan_market_tasks(['1101','1102','1103'],[],progress,[],d,1)
    assert tasks==[{'key':'revenue:1103','dataset':'TaiwanStockMonthRevenue','symbol':'1103'}]
    progress['revenue:1103']={'last_attempt':d,'last_success':d}
    assert plan_market_tasks(['1103'],[],progress,[],d+timedelta(days=1))[0]==[]


def test_same_day_failure_is_not_retried_and_old_candidate_revenue_is_reused():
    d=date(2026,10,7)
    progress={'institutional:TWSE:2026-10-07':{'last_attempt':d,'error_message':'timeout'}}
    jobs=[{'trade_date':d-timedelta(days=1),'metadata':{'requestCount':1,'tasks':[
        {'symbol':'1101','dataset':'TaiwanStockMonthRevenue'}]}}]
    tasks,_=plan_market_tasks(['1101','1102'],[d],progress,jobs,d)
    assert [t['key'] for t in tasks]==['institutional:TPEX:2026-10-07','revenue:1102']


def test_cached_bulk_institutional_evidence_preserves_other_factor_features(monkeypatch):
    from stock_db import prelaunch
    class Context:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def fetchval(self,*args):return 'institutional_daily_history'
        async def fetch(self,sql,*args):
            if 'FROM monthly_revenue' in sql or 'FROM tdcc_distribution_snapshots' in sql:return []
            if 'FROM daily_factor_snapshots' in sql:
                return [{'symbol':'1101','trade_date':date(2026,10,7),'features':{
                    'event':{'hardRisk':True},'chip':{'tdccScore':60,'institutionalDailyNetShares':[
                        {'date':'2026-10-06','netShares':99}]}}}]
            return [{'symbol':'1101','trade_date':date(2026,10,6),'net_shares':-20},
                    {'symbol':'1101','trade_date':date(2026,10,7),'net_shares':100}]
    monkeypatch.setattr(prelaunch.stock_database,'acquire',lambda:Context())
    e=asyncio.run(prelaunch.cached_evidence(['1101'],date(2026,10,7)))['1101']
    assert e['features']['event']['hardRisk'] is True
    assert e['features']['chip']['tdccScore']==60
    assert e['features']['chip']['institutionalDailyNetShares']==[
        {'date':'2026-10-06','netShares':-20},{'date':'2026-10-07','netShares':100}]


def test_close_finalization_adds_one_batch_without_running_radar(monkeypatch):
    from stock_db import maintenance
    calls=[]
    async def market(**kwargs):return {'ok':True,'hasMore':False}
    async def master():return {'ok':True}
    async def fundamentals(**kwargs):return {'ok':True,'skipped':True}
    async def history(**kwargs):calls.append(kwargs);return {'ok':True,'requestedDatasets':8}
    async def positions():return {}
    async def radar(**kwargs):raise AssertionError('Must remain separate')
    monkeypatch.setattr(maintenance,'update_official_daily',market)
    monkeypatch.setattr(maintenance,'sync_security_master',master)
    monkeypatch.setattr(maintenance,'refresh_monthly_revenue_if_due',fundamentals)
    monkeypatch.setattr(maintenance,'prepare_market_history',history)
    monkeypatch.setattr(maintenance,'run_full_bullish_radar',radar)
    monkeypatch.setattr(maintenance.portfolio_ledger,'get_positions',positions)
    result=asyncio.run(maintenance.run_daily_maintenance(run_radar=False,update_performance=False))
    assert result['completed'] and result['historyPreparation']['requestedDatasets']==8
    assert calls==[{'limit':30}]


def test_tpex_official_industry_code_is_not_discarded(monkeypatch):
    from stock_db import data_sources
    async def fetch(url):
        if url==data_sources.TPEX_SECURITIES_URL:
            return [{'SecuritiesCompanyCode':'1240','CompanyAbbreviation':'茂生農經','SecuritiesIndustryCode':'33'}]
        return [{'公司代號':'1101','公司簡稱':'台泥','產業別':'01'}]
    monkeypatch.setattr(data_sources,'_get_json',fetch)
    rows=asyncio.run(data_sources.fetch_security_master())
    assert {r['symbol']:r['industry'] for r in rows}=={'1101':'01','1240':'33'}
