from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import asyncio

import pytest

from stock_db.formal_quality import (FORMAL_MODEL_REVISION, apply_formal_quality,
                                    evaluate_formal_quality, merge_formal_evidence)
from stock_db.v12 import V12Config
from stock_db.performance import simulate_signal_execution
from stock_db.market_history import plan_market_tasks


def setup():
    c = dict(symbol="2393", name="測試", industry="26", trade_date="2026-10-08",
             close=50, high=50, low=48, prev_close=48, prev_high=49, ma20=48,
             volume=5_000_000, volume_ma20=2_000_000, volume_ratio=2.5,
             turnover=245_000_000, atr14=2, strengthEligible=True,
             forwardQualified=True, actionCode="BUY_ON_BREAKOUT",
             strengthProfile={"returns":{"5":5}, "excessVsMarketMedian":{"5":4,"10":3},
                              "excessVsSectorMedian":{"5":2},
                              "sectorContext":{"aboveMA20Percent":70}},
             marketContext={"regime":"NEUTRAL"},
             tradingPlan={"aggressiveEntry":{"entryLow":48,"entryHigh":48.1,"positionPercent":40},
                          "confirmationEntry":{"price":50,"executable":True,"availableBelowNoChase":True,
                                               "positionPercent":60,"conditions":[]},
                          "maximumBuyPrice":51,"noChasePrice":52,
                          "failureCondition":{"price":48},"positionPlan":{}})
    e = {"revenues":[{"revenue_month":"2026-09-01","yearly_change_percent":20}],
         "features":{"chip":{"institutionalDailyNetShares":[
             {"date":d,"netShares":n} for d,n in zip(
                 ["2026-10-01","2026-10-02","2026-10-05","2026-10-06","2026-10-08"],
                 [100,-20,100,100,100])],"marginBalanceChangePercent":3}}}
    return c,e


def codes(c,e):
    return evaluate_formal_quality(c,e,V12Config())["metrics"].get("failedRuleCodes", [])


def test_complete_evidence_qualifies_without_mutating_original_plan():
    c,e=setup(); original=deepcopy(c)
    r=apply_formal_quality(c,e,V12Config())
    assert r["formalQualification"]["qualified"]
    assert r["formalModelRevision"]==FORMAL_MODEL_REVISION
    assert r["tradingPlan"]["aggressiveEntry"]["positionPercent"]==0
    assert r["tradingPlan"]["positionPlan"]["maximumPlannedPercent"]==40
    assert c==original


@pytest.mark.parametrize("change,code",[
    ({"volume_ratio":.5},"activation_volume"),
    ({"volume":2_000_000,"turnover":900_000_000},"volume"),
    ({"volume_ma20":1_000_000},"volume_ma20"),
    ({"close":48.5},"activation_price"),
    ({"close":60},"not_extended"),
    ({"symbol":"2881","industry":"金融"},"scope"),
    ({"strengthEligible":False},"strength_profile"),
])
def test_scores_or_turnover_cannot_override_failed_evidence(change,code):
    c,e=setup();c.update(change,total_score=100,dataConfidence=100)
    assert code in codes(c,e)
    r=apply_formal_quality(c,e,V12Config())
    assert not r["forwardQualified"] and r["actionCode"]=="WAIT_ACTIVATION"


def test_current_institutional_data_required_and_future_data_ignored():
    c,e=setup(); daily=e["features"]["chip"]["institutionalDailyNetShares"]
    daily[-1]["date"]="2026-10-07"
    daily.append({"date":"2026-10-09","netShares":999999})
    r=evaluate_formal_quality(c,e,V12Config())
    assert not r["qualified"]
    assert "fiveInstitutionalSessionsThroughSignalDate" in r["missingInputs"]


def test_one_buy_day_does_not_count_as_accumulation():
    c,e=setup(); d=e["features"]["chip"]["institutionalDailyNetShares"]
    for r in d[:-1]:r["netShares"]=-10
    d[-1]["netShares"]=1000
    assert "persistent_chip" in codes(c,e)


def test_official_history_wins_duplicate_dates_and_keeps_hard_risk():
    c,e=setup();c["factorFeatures"]={"chip":{"institutionalDailyNetShares":[
        {"date":"2026-10-08","netShares":-999}]},"event":{"hardRisk":True}}
    merged=merge_formal_evidence(c,e)
    assert merged["features"]["chip"]["institutionalDailyNetShares"][-1]["netShares"]==100
    assert "event_risk" in codes(c,merged)


def test_weak_market_requires_stronger_volume_and_relative_leadership():
    c,e=setup();c["marketContext"]={"regime":"WEAK"};c["volume_ratio"]=1.4
    assert "activation_volume" in codes(c,e)
    c["volume_ratio"]=2;c["strengthProfile"]["excessVsMarketMedian"]["5"]=1
    assert "weak_market_leader" in codes(c,e)


def test_missing_revenue_and_tight_risk_are_not_treated_as_neutral():
    c,e=setup();e["revenues"]=[]
    assert "fundamental" in codes(c,e)
    c,e=setup();c["tradingPlan"]["failureCondition"]["price"]=49.9
    assert "cost_and_noise" in codes(c,e)


def test_watch_and_failed_base_rules_cannot_be_promoted():
    c,e=setup();c.update(actionCode="DO_NOT_CHASE",forwardQualified=False)
    assert not apply_formal_quality(c,e,V12Config())["formalQualification"]["qualified"]


def test_watch_is_no_trade_and_confirmation_requires_new_volume():
    c,e=setup();c["volume_ratio"]=.5
    watch=apply_formal_quality(c,e,V12Config())
    bars=[dict(trade_date="2026-10-12",open=49,high=51,low=48.5,close=50.5,volume_ratio=2)]
    assert simulate_signal_execution(watch,bars)["execution_status"]=="NO_TRADE"
    c,e=setup();formal=apply_formal_quality(c,e,V12Config())
    bars[0]["volume_ratio"]=.5
    assert simulate_signal_execution(formal,bars)["entry_date"] is None
    bars[0].update(volume_ratio=2,high=50.5)
    r=simulate_signal_execution(formal,bars)
    assert r["entry_date"]=="2026-10-12"
    assert r["aggressive_fill_percent"]==0


def test_same_day_official_retry_is_cooled_and_budgeted():
    d=date(2026,10,8);now=datetime(2026,10,8,9,tzinfo=timezone.utc)
    key="institutional:TWSE:2026-10-08"
    progress={key:{"last_attempt":d,"error_message":"not published",
                   "updated_at":now-timedelta(minutes=45)}}
    jobs=[{"trade_date":d,"metadata":{"requestCount":39,"tasks":[{"key":key}]}}]
    tasks,used=plan_market_tasks([], [d], progress,jobs,d,retry_failed_institutional=True,now=now)
    assert used==39 and len(tasks)==1 and tasks[0]["key"]==key
    progress[key]["updated_at"]=now-timedelta(minutes=10)
    assert all(t["key"]!=key for t in plan_market_tasks([], [d],progress,jobs,d,
                        retry_failed_institutional=True,now=now)[0])
    jobs[0]["metadata"]["requestCount"]=40
    assert plan_market_tasks([], [d],progress,jobs,d,retry_failed_institutional=True,now=now)[0]==[]


def test_second_failed_retry_cannot_reserve_again_that_day():
    d=date(2026,10,8);now=datetime(2026,10,8,9,tzinfo=timezone.utc)
    key="institutional:TWSE:2026-10-08"
    p={key:{"last_attempt":d,"error_message":"timeout","updated_at":now-timedelta(hours=1)}}
    jobs=[{"trade_date":d,"metadata":{"requestCount":1,"tasks":[{"key":key}]}}]*2
    tasks,_=plan_market_tasks([], [d],p,jobs,d,retry_failed_institutional=True,now=now)
    assert all(t["key"]!=key for t in tasks)


def test_new_performance_cohort_excludes_old_and_watch_snapshots(monkeypatch):
    from stock_db import performance
    queries=[]
    class Connection:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def fetchrow(self,sql,*args):
            queries.append((sql,args))
            return {"evaluated_signals":0,"pending":0,"filled":0}
    async def schema(*args):pass
    monkeypatch.setattr(performance.stock_database,"acquire",lambda:Connection())
    monkeypatch.setattr(performance,"_ensure_execution_schema",schema)
    r=asyncio.run(performance.execution_performance_summary(formal_model_revision=FORMAL_MODEL_REVISION))
    sql,args=queries[0]
    assert "c.snapshot->>'formalModelRevision'=$1" in sql
    assert "c.snapshot->'formalQualification'->>'qualified'='true'" in sql
    assert args==(FORMAL_MODEL_REVISION,)
    assert r["formalModelRevision"]==FORMAL_MODEL_REVISION
