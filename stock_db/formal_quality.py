"""Prospective formal-entry gates; scores never substitute for evidence.

This revision is recorded in each new snapshot. Old signals are not relabelled
or backtested with today's financial/ownership cache.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from math import isfinite
from typing import Any, Mapping

FORMAL_MODEL_REVISION = "V12.4-FORMAL-ACTIVATION-1"


def merge_formal_evidence(candidate: Mapping, cached: Mapping) -> dict:
    result = deepcopy(dict(cached))
    old = result.get("features") or {}
    live = candidate.get("factorFeatures") or {}
    features = {**old, **deepcopy(live)}
    chip = {**(old.get("chip") or {}), **(live.get("chip") or {})}
    daily = {str(r.get("date")):dict(r) for r in (live.get("chip") or {}).get("institutionalDailyNetShares", [])}
    # Bulk official history has priority for duplicate dates.
    daily.update({str(r.get("date")):dict(r) for r in (old.get("chip") or {}).get("institutionalDailyNetShares", [])})
    chip["institutionalDailyNetShares"] = [daily[d] for d in sorted(daily)]
    features["chip"] = chip
    result["features"] = features
    return result


def number(value):
    try:
        n = float(value)
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def evaluate_formal_quality(candidate: Mapping, evidence: Mapping, config: Any) -> dict:
    failed, missing, passed, warnings = [], [], [], []
    metrics = {}

    def require(code, condition, label):
        (passed if condition else failed).append(label)
        if not condition:
            metrics.setdefault("failedRuleCodes", []).append(code)

    as_of = day(candidate.get("trade_date"))
    features = evidence.get("features") or candidate.get("factorFeatures") or {}
    close = number(candidate.get("close"))
    financial = (features.get("fundamental") or {}).get("officialQuarterlyFinancials") or {}
    symbol = str(candidate.get("symbol") or "")
    industry = str(candidate.get("industry") or "")
    is_financial = (financial.get("isFinancialIndustry") is True
                    or industry == "17" or "金融" in industry
                    or symbol.startswith(("28", "58", "60")))
    require("scope", bool(close and close <= config.primary_max_price and not is_financial),
            "200元以下非金融普通股")
    for key, minimum, label in (
        ("volume", config.formal_min_daily_volume_lots * 1000, "當日成交張數達正式門檻"),
        ("volume_ma20", config.formal_min_average_volume20_lots * 1000, "20日均量達正式門檻"),
        ("turnover", config.effective_min_trade_value, "成交金額達流動性門檻"),
    ):
        n = number(candidate.get(key))
        if n is None:
            missing.append(key)
        require(key, n is not None and n >= minimum, label)
    profile = candidate.get("strengthProfile") or {}
    require("strength_profile", candidate.get("strengthEligible") is True, "強勢品質分數與型態同時達標")
    rs_market = profile.get("excessVsMarketMedian") or {}
    rs_sector = profile.get("excessVsSectorMedian") or {}
    sector = profile.get("sectorContext") or {}
    rs5, rs10, sector5 = [number(v) for v in (rs_market.get("5"), rs_market.get("10"), rs_sector.get("5"))]
    breadth = number(sector.get("aboveMA20Percent"))
    if any(v is None for v in (rs5, rs10, sector5, breadth)):
        missing.append("relativeStrengthAndSector")
    require("relative_strength", bool(rs5 is not None and rs10 is not None and sector5 is not None
            and rs5 > 0 and rs10 >= 0 and sector5 >= 0), "近5、10日強於市場且未落後族群")
    require("sector", breadth is not None and breadth >= 50, "族群至少半數站上20日線")
    market = candidate.get("marketContext") or {}
    weak = str(market.get("regime") or "").upper() == "WEAK"
    vr_min = config.formal_weak_min_volume_ratio if weak else config.formal_min_volume_ratio
    vr = number(candidate.get("volume_ratio"))
    high, low, previous, ma20, previous_high = [number(candidate.get(k))
        for k in ("high", "low", "prev_close", "ma20", "prev_high")]
    position = ((close-low)/(high-low) if close is not None and high is not None and low is not None and high > low else 0)
    require("activation_volume", vr is not None and vr >= vr_min,
            f"啟動量比至少{vr_min:.2f}倍（縮量拉回留待觀察）")
    require("activation_price", bool(close and previous and previous_high and close > previous
            and close >= previous_high and position >= config.formal_min_close_position),
            "收盤上漲、收復前日高點且接近當日高點")
    distance = (close/ma20-1)*100 if close and ma20 else None
    gain5 = number((profile.get("returns") or {}).get("5"))
    require("not_extended", bool(distance is not None and 0 <= distance <= config.formal_max_distance_ma20_pct
            and gain5 is not None and gain5 <= config.predictive_max_5day_change_pct),
            "未過度偏離20日線且五日漲幅未透支")
    if weak:
        require("weak_market_leader", rs5 is not None and rs5 >= 3 and (breadth or 0) >= 60,
                "弱勢市場須為相對強勢族群領先股")

    chip = features.get("chip") or {}
    daily = {day(r.get("date")): number(r.get("netShares"))
             for r in chip.get("institutionalDailyNetShares") or []
             if day(r.get("date")) and as_of and day(r["date"]) <= as_of}
    dates = sorted(daily)[-5:]
    nets = [daily[d] for d in dates]
    fresh = bool(len(dates) == 5 and dates[-1] == as_of
                 and (dates[-1]-dates[0]).days <= 10 and all(n is not None for n in nets))
    if not fresh:
        missing.append("fiveInstitutionalSessionsThroughSignalDate")
    accumulation = bool(fresh and sum(n > 0 for n in nets) >= 3
                        and sum(nets) > 0 and sum(nets[-2:]) > 0 and nets[-1] > 0)
    require("persistent_chip", accumulation, "近五日法人持續累積且訊號日買超")
    revenues = sorted((r for r in evidence.get("revenues") or []
                       if day(r.get("revenue_month")) and as_of and day(r["revenue_month"]) <= as_of),
                      key=lambda r:str(r["revenue_month"]), reverse=True)
    fundamental = features.get("fundamental") or {}
    month = day(revenues[0].get("revenue_month")) if revenues else day(fundamental.get("revenueMonth"))
    yoy = number(revenues[0].get("yearly_change_percent")) if revenues else number(fundamental.get("revenueYoYPercent"))
    revenue_fresh = bool(month and as_of and 0 <= (as_of-month).days <= 80 and yoy is not None)
    if not revenue_fresh:
        missing.append("recentPublishedRevenue")
    require("fundamental", bool(revenue_fresh and yoy >= 0), "最新已公布營收年增非負")
    margin_change = number(chip.get("marginBalanceChangePercent"))
    require("margin_overheat", margin_change is None or margin_change <= 25 or (gain5 is not None and gain5 <= 5),
            "避免融資快速增加且股價已急漲")
    if margin_change is None:
        warnings.append("融資變化資料不足，未推定融資下降")
    ownership = evidence.get("ownership") or []
    if len(ownership) < 2:
        warnings.append("持股分布週資料不足；籌碼通過僅代表法人日資料累積")
    require("event_risk", not (features.get("event") or {}).get("hardRisk")
            and not (features.get("event") or {}).get("disposition"), "未出現已知重大事件硬風險")
    plan = candidate.get("tradingPlan") or {}
    entry = plan.get("confirmationEntry") or {}
    trigger, maximum, failure, atr = [number(v) for v in (
        entry.get("price"), plan.get("maximumBuyPrice"),
        (plan.get("failureCondition") or {}).get("price"), candidate.get("atr14"))]
    risk_pct = (trigger-failure)/trigger*100 if trigger and failure else None
    require("executable_plan", bool(entry.get("executable") and trigger and maximum and failure
            and failure < trigger <= maximum), "確認買點可執行且未超過追價上限")
    require("cost_and_noise", bool(risk_pct is not None and .8 <= risk_pct <= 6
            and atr and trigger-failure >= .5*atr), "買點至失效價須留足成本與半個ATR波動空間")
    metrics.update(volumeRatio=vr, minimumVolumeRatio=vr_min, closePosition=round(position, 4),
                   institutionalDates=[str(d) for d in dates], institutionalNetShares=nets,
                   revenueMonth=str(month) if month else None, revenueYoYPercent=yoy,
                   distanceMA20Percent=distance, confirmationRiskPercent=risk_pct,
                   marketRegime=market.get("regime"), ownershipDates=[str(r.get("snapshot_date")) for r in ownership[:3]])
    return dict(modelRevision=FORMAL_MODEL_REVISION, qualified=not failed,
                passedRules=passed, failedRules=failed, missingInputs=missing,
                warnings=warnings, metrics=metrics, historicalPointInTimeBacktest=False)


def apply_formal_quality(candidate: Mapping, evidence: Mapping, config: Any) -> dict:
    item = deepcopy(dict(candidate))
    report = evaluate_formal_quality(item, evidence, config)
    previous_qualified = bool(item.get("forwardQualified", False))
    from .v12 import V12_ACTIONABLE_STATUS_CODES
    was_formal = item.get("actionCode") in V12_ACTIONABLE_STATUS_CODES and previous_qualified
    report["baseQualified"] = was_formal
    report["qualified"] = was_formal and report["qualified"]
    item.update(formalModelRevision=FORMAL_MODEL_REVISION, formalQualification=report)
    if not was_formal:
        return item
    if not report["qualified"]:
        item.update(forwardQualified=False, actionCode="WAIT_ACTIVATION", action="等待量價與籌碼確認")
        qualification = dict(item.get("forwardQualification") or {})
        qualification.update(qualified=False, failedRules=list(qualification.get("failedRules") or [])+report["failedRules"])
        item["forwardQualification"] = qualification
        item["warnings"] = list(item.get("warnings") or []) + report["failedRules"] + report["warnings"]
        item.setdefault("tradingPlan", {})["statusCode"] = "WAIT_ACTIVATION"
        item["tradingPlan"]["status"] = item["action"]
        return item
    # A formal signal is entered only after price and volume confirmation;
    # a touch of the former aggressive support zone cannot manufacture a fill.
    plan = item["tradingPlan"]
    plan.setdefault("aggressiveEntry", {})["positionPercent"] = 0
    plan["confirmationEntry"]["positionPercent"] = 40
    plan["confirmationEntry"]["minimumVolumeRatio"] = report["metrics"]["minimumVolumeRatio"]
    plan["confirmationEntry"]["minimumClosePosition"] = config.formal_min_close_position
    plan["confirmationEntry"].setdefault("conditions", []).append("成交日量比與收盤位置再次達正式門檻；只觸價不視為確認")
    plan.setdefault("positionPlan", {}).update(aggressiveEntryPercent=0, confirmationEntryPercent=40,
                                maximumPlannedPercent=40, description="價格與量能確認後進場40%；不預先低接")
    item["warnings"] = list(item.get("warnings") or []) + report["warnings"]
    return item


def prioritize_activation(candidate: Mapping, config: Any) -> dict:
    """Reserve enrichment capacity for technically executable activations."""
    from .v12 import V12_ACTIONABLE_STATUS_CODES
    item = dict(candidate)
    technical_codes = {"scope", "volume", "volume_ma20", "turnover", "strength_profile",
                       "relative_strength", "sector", "activation_volume", "activation_price",
                       "not_extended", "weak_market_leader", "executable_plan", "cost_and_noise"}
    failed = set(evaluate_formal_quality(item, {}, config)["metrics"].get("failedRuleCodes", []))
    item["activationTechnicalReady"] = bool(item.get("actionCode") in V12_ACTIONABLE_STATUS_CODES
        and item.get("forwardQualified") and not failed.intersection(technical_codes))
    return item
