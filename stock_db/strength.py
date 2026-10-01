"""Point-in-time strength ranking; uses only the current database snapshot.

Market benchmarks are common-stock median returns, NOT TAIEX/index returns.
Strength eligibility never grants permission to buy or overrides an entry plan.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from math import isfinite
from statistics import median
from typing import Any, Mapping, Sequence

STRENGTH_MODEL_REVISION = "V12.4-STRENGTH-1"
STRENGTH_FACTOR_MODEL = "V12.4-STRENGTH-FACTORS-1"


def _num(row: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(row.get(key))
        return value if isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _clip(value: float) -> float:
    return max(0.0, min(100.0, value))


def _return(row: Mapping[str, Any], horizon: int) -> float | None:
    close, previous = _num(row, "close"), _num(row, f"close{horizon}")
    if close is None or previous is None or close <= 0 or previous <= 0:
        return None
    return (close / previous - 1) * 100


def _percentile(value: float, distribution: list[float]) -> float:
    # Midrank treats tied/flat stocks neutrally instead of all as leaders.
    return 100 * (bisect_left(distribution, value) + bisect_right(distribution, value)) / (2 * len(distribution))


def build_strength_profiles(rows: Sequence[Mapping[str, Any]], config: Any) -> dict[str, dict[str, Any]]:
    by_symbol = {str(row.get("symbol")): row for row in rows if row.get("symbol")}
    returns = {symbol: {h: _return(row, h) for h in (5, 10, 20)} for symbol, row in by_symbol.items()}
    distributions = {h: sorted(r[h] for r in returns.values() if r[h] is not None) for h in (5, 10, 20)}
    benchmarks = {h: median(values) if values else None for h, values in distributions.items()}
    industries: dict[str, list[str]] = {}
    for symbol, row in by_symbol.items():
        industry = str(row.get("industry") or "").strip()
        if industry:
            industries.setdefault(industry, []).append(symbol)

    sectors: dict[str, dict[str, Any]] = {}
    for industry, symbols in industries.items():
        if len(symbols) < config.sector_minimum_members:
            continue
        medians = {h: median(values) if (values := [returns[s][h] for s in symbols if returns[s][h] is not None]) else None for h in (5, 10, 20)}
        usable = [by_symbol[s] for s in symbols if (_num(by_symbol[s], "ma20") or 0) > 0]
        breadth = sum((_num(r, "close") or 0) >= (_num(r, "ma20") or 0) for r in usable) / len(usable) * 100 if usable else None
        volume_acceleration = [v5 / v20 for s in symbols if (v5 := _num(by_symbol[s], "volume_ma5")) is not None and (v20 := _num(by_symbol[s], "volume_ma20")) is not None and v20 > 0]
        breakout_rows = [by_symbol[s] for s in symbols if (_num(by_symbol[s], "prior_high20") or 0) > 0]
        breakout_pct = sum((_num(r, "close") or 0) >= (_num(r, "prior_high20") or 0) for r in breakout_rows) / len(breakout_rows) * 100 if breakout_rows else None
        sectors[industry] = {"memberCount": len(symbols), "medianReturns": medians, "aboveMA20Percent": breadth, "medianVolumeAcceleration": median(volume_acceleration) if volume_acceleration else None, "breakoutPercent": breakout_pct}

    profiles: dict[str, dict[str, Any]] = {}
    for symbol, row in by_symbol.items():
        own = returns[symbol]
        pct = {h: _percentile(own[h], distributions[h]) if own[h] is not None and distributions[h] else None for h in (5, 10, 20)}
        relative = {h: own[h] - benchmarks[h] if own[h] is not None and benchmarks[h] is not None else None for h in (5, 10, 20)}
        sector = sectors.get(str(row.get("industry") or "").strip())
        sector_relative = {h: own[h] - sector["medianReturns"][h] if sector and own[h] is not None and sector["medianReturns"][h] is not None else None for h in (5, 10, 20)}
        rs = sum((pct[h] if pct[h] is not None else 50) * w for h, w in ((5, .4), (10, .35), (20, .25)))
        sector_score = 50.0
        if sector and sector["medianReturns"][5] is not None and benchmarks[5] is not None and sector["aboveMA20Percent"] is not None:
            sector_score = _clip(50 + (sector["medianReturns"][5] - benchmarks[5]) * 4 + (sector["aboveMA20Percent"] - 50) * .3)
            if sector["medianVolumeAcceleration"] is not None:
                sector_score = _clip(sector_score + max(-10, min(10, (sector["medianVolumeAcceleration"] - 1) * 20)))
            if sector["breakoutPercent"] is not None:
                sector_score = _clip(sector_score + min(10, sector["breakoutPercent"] * .5))

        close, high, low = (_num(row, k) or 0 for k in ("close", "high", "low"))
        position = (close - low) / (high - low) if high > low else .5
        upper_shadow = max(0, high - max(close, _num(row, "open") or close)) / close * 100 if close else 0
        vr = _num(row, "volume_ratio")
        v5, v20 = _num(row, "volume_ma5"), _num(row, "volume_ma20")
        acceleration = v5 / v20 if v5 is not None and v20 is not None and v20 > 0 else None
        ma20, ma5, previous = (_num(row, k) or 0 for k in ("ma20", "ma5", "prev_close"))
        prior_high = _num(row, "prior_high20") or 0
        momentum = 50 + (position - .5) * 40
        reasons, warnings = [], []
        if acceleration is not None:
            momentum += max(-10, min(15, (acceleration - 1) * 25))
        if vr is not None and 1.2 <= vr <= 3.2 and position >= .65 and close >= previous > 0:
            momentum += 15
            reasons.append("上漲放量且收盤接近日高")
        if close >= prior_high > 0 and position >= .65:
            momentum += 10
            reasons.append("收盤突破前20日高點")
        if vr is not None and 0 < vr <= .8 and close >= ma5 > 0 and low >= (_num(row, "prev_low") or low):
            momentum += 10
            reasons.append("量縮回測仍守住短線支撐")
        if upper_shadow > 2.5:
            momentum -= min(25, upper_shadow * 4)
            warnings.append("長上影顯示承接不足")
        if vr is not None and vr >= 2 and close < previous:
            momentum -= 20
            warnings.append("放量收跌")
        failed_breakout = high > prior_high > close > 0
        if failed_breakout:
            momentum -= 15
            warnings.append("盤中突破前高但收盤未守住")
        if close < ma20:
            momentum -= 15
        momentum = _clip(momentum)
        score = round(rs * .45 + sector_score * .25 + momentum * .30, 2)
        missing = [f"return{h}" for h in (5, 10, 20) if own[h] is None]
        if sector is None:
            missing.append("sector")
        if vr is None:
            missing.append("volumeRatio")
        if acceleration is None:
            missing.append("volumeAcceleration")
        if not prior_high:
            missing.append("priorHigh20")
        distance = (close / ma20 - 1) * 100 if ma20 > 0 else None
        stage = "STARTING" if own[20] is not None and own[20] <= 15 and distance is not None and distance <= 8 else "CONTINUING"
        relative_ok = (relative[5] or 0) > 0 and (relative[10] or 0) > 0 and (pct[10] or 0) >= 65
        sector_ok = sector is not None and (sector_relative[5] or 0) >= 0 and sector_score >= 50
        eligible = not missing and relative_ok and sector_ok and own[5] > 0 and close >= ma20 > 0 and position >= .45 and not failed_breakout and upper_shadow <= 2.5 and score >= config.strength_min_score
        if relative_ok:
            reasons.append("近5、10日強於全市場普通股中位數")
        if sector_ok:
            reasons.append("族群有支撐且個股未落後族群")
        profiles[symbol] = {"modelRevision": STRENGTH_MODEL_REVISION, "score": score, "eligible": eligible, "stage": stage, "stageLabel": "啟動候選" if stage == "STARTING" else "強勢續攻", "relativeStrengthScore": round(rs, 2), "sectorResonanceScore": round(sector_score, 2), "volumePriceScore": round(momentum, 2), "returns": {str(h): own[h] for h in (5, 10, 20)}, "marketPercentiles": {str(h): pct[h] for h in (5, 10, 20)}, "excessVsMarketMedian": {str(h): relative[h] for h in (5, 10, 20)}, "excessVsSectorMedian": {str(h): sector_relative[h] for h in (5, 10, 20)}, "benchmarkBasis": "TWSE_TPEX_COMMON_STOCK_MEDIAN_NOT_INDEX", "benchmarkReturns": {str(h): benchmarks[h] for h in (5, 10, 20)}, "sectorContext": sector, "reasons": reasons, "warnings": warnings, "missingInputs": missing}
    return profiles


def apply_strength_ranking(candidate: Mapping[str, Any], profiles: Mapping[str, Any], config: Any) -> dict[str, Any]:
    item = dict(candidate)
    if not config.strength_ranking_enabled:
        return item
    profile = profiles.get(str(item.get("symbol")))
    if not profile:
        return item
    # Always use the newly supplied factor/base score, not a prior strength blend.
    base = float(item.get("ranking_score") or item.get("total_score") or 0)
    item.update({"factorModelRevision": STRENGTH_FACTOR_MODEL, "strengthModelRevision": STRENGTH_MODEL_REVISION, "strengthScore": profile["score"], "strengthEligible": profile["eligible"], "strengthStage": profile["stage"], "strengthProfile": profile, "preStrengthRankingScore": round(base, 2), "ranking_score": round(_clip(profile["score"] * max(0, min(1, config.ranking_strength_weight)) + base * (1 - max(0, min(1, config.ranking_strength_weight)))), 2)})
    return item


def strength_boards(candidates: Sequence[Mapping[str, Any]], limit: int = 10) -> dict[str, Any]:
    ranked = sorted((dict(c) for c in candidates if c.get("strengthEligible")), key=lambda c: (c["strengthScore"], c.get("ranking_score", 0)), reverse=True)
    for rank, item in enumerate(ranked, 1):
        item["strengthRank"] = rank
    return {"strengthModelRevision": STRENGTH_MODEL_REVISION, "strengthTop10": ranked[:limit], "startupCandidates": [c for c in ranked if c["strengthStage"] == "STARTING"][:limit], "continuationCandidates": [c for c in ranked if c["strengthStage"] == "CONTINUING"][:limit], "strengthCandidateCount": len(ranked), "strengthRankingNote": "強勢排名不代表可立即買進；買點與操作狀態另列。市場比較基準為普通股中位數，非加權指數。"}
