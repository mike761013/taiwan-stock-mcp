import asyncio
from copy import deepcopy
from datetime import date, timedelta

import pytest

from stock_db import activation, radar, maintenance
from stock_db.formal_quality import FORMAL_MODEL_REVISION, apply_formal_quality, prioritize_activation
from stock_db.performance import _action_tier, simulate_signal_execution
from stock_db.v12 import V12Config
from test_formal_quality import setup


def test_support_candidate_graduates_only_after_full_evidence():
    c, e = setup()
    c.update(strategy="trend_support_probe", actionCode="PROBE_ENTRY")
    before = deepcopy(c)
    assert prioritize_activation(c, V12Config())["activationTechnicalReady"]
    r = apply_formal_quality(c, e, V12Config())
    assert r["formalQualification"]["qualified"]
    assert radar._is_formal_actionable(r)
    assert r["actionCode"] == r["tradingPlan"]["statusCode"] == "PRICE_CONFIRMATION_REQUIRED"
    assert r["tradingPlan"]["initialPositionPercent"] == 0
    assert r["tradingPlan"]["aggressiveEntry"]["positionPercent"] == 0
    assert r["tradingPlan"]["confirmationEntry"]["positionPercent"] == 40
    assert c == before


@pytest.mark.parametrize("change", [dict(volume=2_000_000), dict(forwardQualified=False),
                                   dict(actionCode="DO_NOT_CHASE"), dict(actionCode="WAIT_PULLBACK")])
def test_no_strategy_shortcut_and_no_conflicting_watch_orders(change):
    c, e = setup()
    c.update(strategy="trend_support_probe", actionCode="PROBE_ENTRY")
    c.update(change)
    r = apply_formal_quality(c, e, V12Config())
    assert not radar._is_formal_actionable(r)
    assert not radar._is_probe_candidate(r)
    assert _action_tier({"snapshot": r}) == "WATCH"
    assert r["actionCode"] == r["tradingPlan"]["statusCode"] == "WAIT_ACTIVATION"
    assert r["tradingPlan"]["initialPositionPercent"] == 0
    assert r["tradingPlan"]["positionPlan"]["maximumPlannedPercent"] == 0
    assert not r["tradingPlan"]["confirmationEntry"]["executable"]
    assert simulate_signal_execution(r, [])['execution_status'] == "NO_TRADE"


def origin():
    c, _ = setup()
    c.update(actionCode="WAIT_ACTIVATION", forwardQualified=False,
             formalQualification={"qualified": False})
    c["tradingPlan"]["hardStopPrice"] = 47
    return c


def test_expiry_counts_sessions_not_calendar_days_and_never_moves_cap():
    c = origin()
    bars = [dict(trade_date="2026-10-12", close=50)]
    assert activation.activation_watch_state(c, bars, date(2026,10,12), V12Config())[0] == "ACTIVE"
    bars[0]["close"] = 51.1
    assert activation.activation_watch_state(c, bars, date(2026,10,12), V12Config())[0] == "ABOVE_CAP"
    bars = [dict(trade_date=date(2026,10,9)+timedelta(days=i), close=50) for i in range(6)]
    assert activation.activation_watch_state(c, bars, date(2026,10,14), V12Config())[0] == "EXPIRED"


def test_recovery_does_not_erase_original_defense_break_or_missing_session():
    c = origin()
    bars = [dict(trade_date="2026-10-09", close=47), dict(trade_date="2026-10-12", close=50)]
    assert activation.activation_watch_state(c, bars, date(2026,10,12), V12Config())[0] == "INVALIDATED"
    assert activation.activation_watch_state(c, bars[-1:], date(2026,10,12), V12Config(),
                                            sessions=[date(2026,10,9),date(2026,10,12)])[0] == "STALE"


def wire_review(monkeypatch, *, activated=False):
    c = origin(); original = deepcopy(c); captured = {}
    latest, evidence = setup()
    latest.update(trade_date="2026-10-12", close=50, actionCode="PROBE_ENTRY", strategy="trend_support_probe")
    latest["tradingPlan"]["hardStopPrice"] = 47
    for r, d in zip(evidence["features"]["chip"]["institutionalDailyNetShares"],
                    ["2026-10-02", "2026-10-05", "2026-10-06", "2026-10-08", "2026-10-12"]):
        r["date"] = d
    saved = dict(ok=True, run=dict(id=474,run_date="2026-10-08"), candidates=[c])
    async def get_saved(**kwargs):
        return saved if kwargs["strategy"] == "v12_combined" else {"ok": False}
    async def fetch_snapshot(): return [latest], 1, date(2026,10,12)
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
        async def fetch(self, sql, *args):
            if "DISTINCT trade_date" in sql: return [dict(trade_date=date(2026,10,12))]
            if "DISTINCT c.symbol" in sql: return [dict(symbol=c["symbol"])] if activated else []
            return [dict(symbol=c["symbol"],trade_date=date(2026,10,12),close=50)]
    async def enrich(items, as_of, context, config):
        captured["enrichment"] = deepcopy(items)
        return deepcopy(items)
    async def gates(items, as_of, config): return [apply_formal_quality(i,evidence,config) for i in items]
    async def save(**kwargs): captured.update(kwargs); return {"radarRunId": 500}
    monkeypatch.setattr(activation.stock_database_service.repository, "get_saved_radar_run", get_saved)
    monkeypatch.setattr(radar, "_fetch_v12_snapshot", fetch_snapshot)
    monkeypatch.setattr(activation.stock_database, "acquire", lambda: Connection())
    monkeypatch.setattr(activation,"build_strength_profiles",lambda *a: {})
    monkeypatch.setattr(activation,"build_market_context",lambda *a: {})
    monkeypatch.setattr(activation,"apply_strength_ranking",lambda c,*a: c)
    monkeypatch.setattr(activation,"apply_market_context",lambda c,*a: c)
    monkeypatch.setattr(activation,"build_v12_candidate",lambda row,strategy,config: deepcopy(latest) if strategy=="trend_support_probe" else None)
    monkeypatch.setattr(activation,"enrich_candidates_v12_3",enrich)
    monkeypatch.setattr(radar,"_apply_formal_gates",gates)
    monkeypatch.setattr(activation.stock_database_service,"save_radar_result",save)
    return c, original, captured


def test_review_rebuilds_current_candidate_and_saves_new_dated_signal(monkeypatch):
    c, original, captured = wire_review(monkeypatch)
    r = asyncio.run(activation.reevaluate_saved_watch_v12())
    assert r["actionableCandidateCount"] == 1
    assert captured["run_date"] == date(2026,10,12)
    assert captured["strategy"] == "v12_activation"
    fresh = captured["candidates"][0]
    assert fresh["trade_date"] == "2026-10-12"
    assert fresh["activationReview"]["sourceRunId"] == 474
    assert fresh["activationReview"]["state"] == "PROMOTED"
    assert fresh["formalModelRevision"] == FORMAL_MODEL_REVISION
    assert c == original


def test_already_promoted_origin_does_not_create_another_entry(monkeypatch):
    _, _, captured = wire_review(monkeypatch,activated=True)
    r = asyncio.run(activation.reevaluate_saved_watch_v12())
    assert r["actionableCandidateCount"] == 0
    assert "enrichment" not in captured


def test_same_date_review_is_reused_without_enrichment_or_duplicate_save(monkeypatch):
    _, _, captured = wire_review(monkeypatch)
    original_get = activation.stock_database_service.repository.get_saved_radar_run
    async def get_saved(**kw):
        if kw["strategy"] == "v12_activation":
            return dict(ok=True, run=dict(id=500,configuration=dict(
                idempotencyKey=f"watch-activation:474:2026-10-12:{FORMAL_MODEL_REVISION}")), candidates=[])
        return await original_get(**kw)
    monkeypatch.setattr(activation.stock_database_service.repository,"get_saved_radar_run",get_saved)
    r=asyncio.run(activation.reevaluate_saved_watch_v12())
    assert r["skipped"] and r["record"] == 500
    assert not captured


def test_duplicate_activation_save_returns_existing_run_under_transaction_lock():
    from stock_db.repository import StockRepository
    from test_saved_radar_result import Context
    calls=[]
    class Connection:
        def transaction(self): return Context(self)
        async def execute(self,sql,*args): calls.append(sql)
        async def fetchval(self,sql,*args):
            assert "configuration->>'idempotencyKey'" in sql
            return 500
    class Database:
        def acquire(self): return Context(Connection())
    r=asyncio.run(StockRepository(Database()).save_radar_run(
        "v12_activation",date(2026,10,12),[],{"idempotencyKey":"test"}))
    assert r==500 and len(calls)==1 and "pg_advisory_xact_lock" in calls[0]


def test_close_reviews_watch_without_running_full_radar(monkeypatch):
    calls=[]
    async def market(**kw): return dict(ok=True,hasMore=False)
    async def noop(**kw): return dict(ok=True)
    async def review(): calls.append("review"); return dict(ok=True,actionableCandidateCount=1)
    async def full(**kw): raise AssertionError("full radar was not requested")
    monkeypatch.setattr(maintenance,"sync_security_master",noop)
    monkeypatch.setattr(maintenance,"update_official_daily",market)
    monkeypatch.setattr(maintenance,"refresh_monthly_revenue_if_due",noop)
    monkeypatch.setattr(maintenance,"prepare_market_history",noop)
    monkeypatch.setattr(maintenance,"reevaluate_saved_watch_v12",review)
    monkeypatch.setattr(maintenance,"run_full_bullish_radar",full)
    monkeypatch.setattr(maintenance.portfolio_ledger,"get_positions",noop)
    r=asyncio.run(maintenance.run_daily_maintenance(run_radar=False,update_performance=False))
    assert r["completed"] and r["candidateActivation"]["actionableCandidateCount"]==1
    assert calls==["review"]
    async def incomplete(**kw): return dict(ok=True,hasMore=True)
    monkeypatch.setattr(maintenance,"update_official_daily",incomplete)
    asyncio.run(maintenance.run_daily_maintenance(run_radar=False,update_performance=False))
    assert calls==["review"]
