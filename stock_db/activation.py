"""Bounded post-close candidate reviews; original radar snapshots stay immutable."""
from copy import deepcopy

from .connection import stock_database
from .formal_quality import FORMAL_MODEL_REVISION, block_entry_plan, day, number, apply_formal_quality
from .service import stock_database_service
from .v12 import V12_STRATEGIES, build_v12_candidate, load_v12_config, apply_market_context, build_market_context
from .strength import build_strength_profiles, apply_strength_ranking
from .formal_quality import prioritize_activation
from .factors import enrich_candidates_v12_3


def activation_watch_state(original, bars, as_of, config, sessions=None):
    """A recovered price cannot resurrect an invalidated or expired signal."""
    signal_date = day(original.get("trade_date"))
    plan = original.get("tradingPlan") or {}
    defense = number((plan.get("failureCondition") or {}).get("price"))
    maximum, cap = number(plan.get("maximumBuyPrice")), number(plan.get("noChasePrice"))
    if not signal_date or any(v is None or v <= 0 for v in (defense, maximum, cap)):
        return "INVALID", "原始日期或風險價格不完整"
    subsequent = sorted((b for b in bars if signal_date < day(b["trade_date"]) <= as_of),
                        key=lambda b: str(b["trade_date"]))
    if not subsequent or day(subsequent[-1]["trade_date"]) != as_of:
        return "STALE", "沒有當日完整行情，不沿用舊訊號"
    expected = sessions if sessions is not None else [day(b["trade_date"]) for b in subsequent]
    if len(expected) > config.activation_watch_sessions:
        return "EXPIRED", "超過候選追蹤交易日數"
    if set(expected) != {day(b["trade_date"]) for b in subsequent}:
        return "STALE", "追蹤期間行情有缺漏，不能推定原始防守未失效"
    if any(number(b.get("close")) is None for b in subsequent):
        return "INVALID", "追蹤期間收盤資料不完整"
    if any(float(b["close"]) < defense for b in subsequent):
        return "INVALIDATED", "追蹤期間曾收盤跌破原始失效價"
    close = float(subsequent[-1]["close"])
    if close > maximum or close >= cap:
        return "ABOVE_CAP", "超過原始可買上限，不上移追價價格"
    return "ACTIVE", "以最新資料重新評估全部正式條件"


async def reevaluate_saved_watch_v12():
    # Local import avoids coupling the full-radar module to maintenance.
    from .radar import _fetch_v12_snapshot, _is_formal_actionable, _candidate_rank_score, _apply_formal_gates
    repository = stock_database_service.repository
    saved = await repository.get_saved_radar_run(strategy="v12_combined")
    if not saved.get("ok"):
        return {"ok": True, "skipped": True, "reason": "沒有可追蹤的正式雷達候選"}
    if not saved.get("complete", True):
        raise RuntimeError("原始雷達快照筆數不完整，停止候選升級")
    rows, universe_count, as_of = await _fetch_v12_snapshot()
    as_of, run = day(as_of), saved["run"]
    if not as_of or as_of <= day(run["run_date"]):
        return {"ok": True, "skipped": True, "reason": "尚無訊號日之後的新收盤"}
    key = f"watch-activation:{run['id']}:{as_of}:{FORMAL_MODEL_REVISION}"
    previous = await repository.get_saved_radar_run(strategy="v12_activation", run_date=as_of)
    if previous.get("ok") and previous["run"]["configuration"].get("idempotencyKey") == key:
        candidates = previous["candidates"]
        formal = [c for c in candidates if _is_formal_actionable(c)]
        return {"ok": True, "skipped": True, "reason": "當日候選已重查", "record": previous["run"]["id"],
                "actionableCandidates": formal, "actionableCandidateCount": len(formal)}
    originals = {str(c["symbol"]): c for c in saved["candidates"] if not _is_formal_actionable(c)}
    if not originals:
        return {"ok": True, "skipped": True, "reason": "上次雷達沒有待確認候選"}
    async with stock_database.acquire() as connection:
        calendar = await connection.fetch("""
            SELECT DISTINCT trade_date FROM daily_bars
            WHERE trade_date>$1 AND trade_date<=$2 ORDER BY trade_date
        """, day(run["run_date"]), as_of)
        bars = await connection.fetch("""
            SELECT symbol, trade_date, close FROM daily_bars
            WHERE symbol=ANY($1::text[]) AND trade_date>$2 AND trade_date<=$3
            ORDER BY symbol,trade_date
        """, list(originals), day(run["run_date"]), as_of)
        activated = await connection.fetch("""
            SELECT DISTINCT c.symbol FROM radar_candidates c
            JOIN radar_runs r ON r.id=c.radar_run_id
            WHERE r.strategy='v12_activation'
              AND c.snapshot->'activationReview'->>'sourceRunId'=$1
              AND c.snapshot->'formalQualification'->>'qualified'='true'
        """, str(run["id"]))
    activated = {str(r["symbol"]) for r in activated}
    config = load_v12_config()
    profiles, context = build_strength_profiles(rows, config), build_market_context(rows, config)
    current = {str(r["symbol"]): r for r in rows}
    pending, observations = [], []
    for symbol, original in originals.items():
        if symbol in activated:
            continue
        state, reason = activation_watch_state(original, [b for b in bars if str(b["symbol"]) == symbol], as_of, config,
                                               sessions=[day(b["trade_date"]) for b in calendar])
        review = dict(sourceRunId=run["id"], sourceSignalDate=run["run_date"], evaluationDate=str(as_of),
                      state=state, reason=reason, sourceScore=original.get("total_score"),
                      originalPlan=deepcopy(original.get("tradingPlan") or {}))
        matches = []
        if state == "ACTIVE" and symbol in current:
            for strategy in V12_STRATEGIES:
                candidate = build_v12_candidate(current[symbol], strategy, config)
                if candidate is None:
                    continue
                plan, old_plan = candidate["tradingPlan"], original["tradingPlan"]
                plan["maximumBuyPrice"] = min(plan["maximumBuyPrice"], old_plan["maximumBuyPrice"])
                plan["noChasePrice"] = min(plan["noChasePrice"], old_plan["noChasePrice"])
                plan["failureCondition"]["price"] = max(plan["failureCondition"]["price"], old_plan["failureCondition"]["price"])
                plan["signalDefensePrice"] = plan["failureCondition"]["price"]
                plan["hardStopPrice"] = max(plan["hardStopPrice"], old_plan.get("hardStopPrice") or 0)
                plan["failureCondition"]["legacyEmergencyHardStopPrice"] = plan["hardStopPrice"]
                candidate.update(signal_defense_price=plan["signalDefensePrice"], hard_stop_price=plan["hardStopPrice"])
                trigger = plan["confirmationEntry"]["price"]
                plan["confirmationEntry"].update(
                    availableBelowNoChase=trigger < plan["noChasePrice"],
                    availableBelowMaximumBuy=trigger <= plan["maximumBuyPrice"],
                    conditions=[f"確認價{trigger}；成交不得高於{plan['maximumBuyPrice']}，不得追至{plan['noChasePrice']}",
                                f"失效價{plan['failureCondition']['price']}；須重新通過當日量價與籌碼條件"])
                plan["confirmationEntry"]["executable"] = (
                    plan["failureCondition"]["price"] < trigger <= plan["maximumBuyPrice"]
                    and trigger < plan["noChasePrice"])
                candidate = prioritize_activation(apply_strength_ranking(
                    apply_market_context(candidate, context, config), profiles, config), config)
                matches.append(candidate)
        if matches:
            candidate = max(matches, key=lambda c: (bool(c.get("activationTechnicalReady")), _candidate_rank_score(c)))
            candidate["strategies"] = [c["strategy"] for c in matches]
            candidate["activationReview"] = review
            if candidate.get("activationTechnicalReady"):
                pending.append(candidate)
                continue
            candidate = apply_formal_quality(candidate, {}, config)
        else:
            candidate = deepcopy(original)
            # A failed old setup must not display yesterday's price/factors
            # as a newly calculated score for today's review.
            candidate.update({k: current.get(symbol, {}).get(k) for k in
                              ("close", "open", "high", "low", "volume", "volume_ratio")})
            candidate.update(total_score=None, finalScore=None, bullish_score=None, ranking_score=0,
                             factorScores={}, factorFeatures={}, dataConfidence=0)
            candidate.update(trade_date=str(as_of), actionCode="WAIT_ACTIVATION", action="候選尚未啟動",
                             forwardQualified=False, formalModelRevision=FORMAL_MODEL_REVISION,
                             formalQualification={"qualified": False, "failedRules": [reason if state != "ACTIVE" else "最新型態未達候選條件"]})
            block_entry_plan(candidate, candidate["formalQualification"]["failedRules"][0])
        candidate["activationReview"] = review
        observations.append(candidate)
    pending.sort(key=_candidate_rank_score, reverse=True)
    selected, deferred = pending[:config.activation_enrichment_limit], pending[config.activation_enrichment_limit:]
    for candidate in deferred:
        candidate = apply_formal_quality(candidate, {}, config)
        candidate.update(forwardQualified=False, actionCode="WAIT_ACTIVATION", action="等待完整因子重查")
        candidate["formalQualification"]["qualified"] = False
        candidate["formalQualification"]["failedRules"].append("本輪完整因子重查名額已滿，未推定通過")
        block_entry_plan(candidate, "完整因子尚未重查；不提供進場部位")
        observations.append(candidate)
    enriched = await enrich_candidates_v12_3(selected, as_of, context, config) if selected else []
    qualified = await _apply_formal_gates(enriched, as_of, config) if enriched else []
    results = qualified + observations
    promotions = [c for c in qualified if _is_formal_actionable(c)]
    for c in qualified:
        c["activationReview"]["state"] = "PROMOTED" if _is_formal_actionable(c) else "WAITING"
        c["activationReview"]["reason"] = "最新全部正式條件通過，建立新日期訊號" if _is_formal_actionable(c) else "最新正式條件未全部通過，詳見formalQualification"
    record = await stock_database_service.save_radar_result(
        strategy="v12_activation", candidates=results, run_date=as_of, universe_count=universe_count,
        configuration={"formalModelRevision": FORMAL_MODEL_REVISION, "sourceRunId": run["id"],
                       "idempotencyKey": key, "fullUniverseRadar": False})
    return {"ok": True, "latestTradeDate": str(as_of), "sourceRunId": run["id"],
            "formalModelRevision": FORMAL_MODEL_REVISION, "record": record,
            "reviewedCount": len(results), "enrichedCount": len(selected), "deferredCount": len(deferred),
            "actionableCandidateCount": len(promotions), "actionableCandidates": promotions,
            "watchCandidates": observations + [c for c in qualified if c not in promotions]}
