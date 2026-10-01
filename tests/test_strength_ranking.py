import asyncio
from copy import deepcopy
from datetime import date

from stock_db.strength import (
    apply_strength_ranking, build_strength_profiles, strength_boards,
)
from stock_db.v12 import V12Config
from stock_db import radar


def row(symbol, gain=0, industry="AI", **changes):
    close = 100 * (1 + gain / 100)
    result = dict(symbol=symbol, name=symbol, market="TWSE", industry=industry,
                  close=close, close5=100, close10=98, close20=95,
                  open=close-1, high=close+.2, low=close-2, prev_close=close-1,
                  prev_low=close-3, ma5=close-1, ma10=close-2, ma20=close-3,
                  ma60=close-5, prev_ma5=close-2, prev_ma20=close-4,
                  volume=4000000, turnover=400000000, volume_ratio=1.5,
                  volume_ma5=3000000, volume_ma20=2000000,
                  prior_high20=close-1, technical_score=85)
    result.update(changes)
    return result


def universe():
    return [row(str(1100+i), gain=i/10) for i in range(15)] + [row("6209", 6)]


def test_leader_outranks_flat_stock_despite_lower_old_score():
    cfg = V12Config()
    profiles = build_strength_profiles(universe(), cfg)
    leader = apply_strength_ranking(dict(symbol="6209", ranking_score=60), profiles, cfg)
    flat = apply_strength_ranking(dict(symbol="1100", ranking_score=90), profiles, cfg)
    assert leader["strengthEligible"] is True
    assert leader["ranking_score"] > flat["ranking_score"]
    assert leader["strengthProfile"]["benchmarkBasis"].endswith("NOT_INDEX")


def test_missing_history_and_sector_do_not_become_strong_candidates():
    rows = universe() + [row("9999", 20, industry="", close20=None)]
    p = build_strength_profiles(rows, V12Config())["9999"]
    assert p["eligible"] is False
    assert "return20" in p["missingInputs"]
    assert "sector" in p["missingInputs"]


def test_failed_breakout_and_distribution_are_penalised():
    healthy = universe()
    failed = deepcopy(healthy)
    failed[-1].update(high=110, close=106, prior_high20=108,
                      prev_close=107, volume_ratio=3)
    good = build_strength_profiles(healthy, V12Config())["6209"]
    bad = build_strength_profiles(failed, V12Config())["6209"]
    assert good["score"] > bad["score"]
    assert bad["eligible"] is False
    assert "放量收跌" in bad["warnings"]


def test_strength_never_promotes_watch_to_buy_or_changes_stop():
    c = dict(symbol="6209", ranking_score=60, actionCode="DO_NOT_CHASE",
             forwardQualified=False, tradingPlan={"hardStopPrice": 90})
    result = apply_strength_ranking(c, build_strength_profiles(universe(), V12Config()), V12Config())
    assert result["actionCode"] == "DO_NOT_CHASE"
    assert result["forwardQualified"] is False
    assert result["tradingPlan"] == c["tradingPlan"]
    assert strength_boards([result])["strengthTop10"][0]["symbol"] == "6209"


def test_boards_separate_startup_and_continuation():
    c = [dict(symbol="A", strengthEligible=True, strengthScore=90, strengthStage="CONTINUING"),
         dict(symbol="B", strengthEligible=True, strengthScore=80, strengthStage="STARTING")]
    boards = strength_boards(c, 1)
    assert boards["startupCandidates"][0]["symbol"] == "B"
    assert boards["continuationCandidates"][0]["symbol"] == "A"


def test_flat_ties_are_neutral_not_top_percentile():
    p = build_strength_profiles([row(str(1100+i)) for i in range(10)], V12Config())
    assert p["1100"]["marketPercentiles"]["10"] == 50
    assert not p["1100"]["eligible"]


def test_single_strategy_ranks_before_shortlisting_and_after_factor_enrichment(monkeypatch):
    cfg = V12Config(factor_prefilter_limit=1)
    rows = universe()
    received = []
    async def snapshot():
        return rows, len(rows), date(2026, 10, 1)
    async def priors(**kwargs):
        return {}
    def screen(**kwargs):
        # All candidates must reach the strength prefilter, even beyond 200.
        assert kwargs["limit"] >= len(rows)
        return [dict(symbol="1100", close=100, industry="AI", ranking_score=90, bullish_score=90),
                dict(symbol="6209", close=106, industry="AI", ranking_score=60, bullish_score=60)], {}
    async def enrich(candidates, *args):
        received.extend(c["symbol"] for c in candidates)
        # Provider overwrites the base rank: strength must be re-applied.
        return [{**c, "ranking_score": 70} for c in candidates]
    monkeypatch.setattr(radar, "load_v12_config", lambda: cfg)
    monkeypatch.setattr(radar, "_fetch_v12_snapshot", snapshot)
    monkeypatch.setattr(radar, "execution_strategy_priors", priors)
    monkeypatch.setattr(radar, "screen_v12_rows", screen)
    monkeypatch.setattr(radar, "find_v12_near_misses", lambda *a, **k: [])
    monkeypatch.setattr(radar, "enrich_candidates_v12_3", enrich)
    result = asyncio.run(radar.screen_database_market_v12("early_stage", limit=1, save_result=False))
    assert received[0] == "6209"
    assert result["results"][0]["symbol"] == "6209"
    assert result["results"][0]["preStrengthRankingScore"] == 70
    assert result["record"] is None


def test_breakout_reference_excludes_signal_day():
    assert "reverse_rank BETWEEN 2 AND 21" in radar._V12_SNAPSHOT_QUERY


def test_disable_restores_original_ranking():
    cfg = V12Config(strength_ranking_enabled=False)
    c = dict(symbol="6209", ranking_score=60)
    assert apply_strength_ranking(c, build_strength_profiles(universe(), cfg), cfg) == c
