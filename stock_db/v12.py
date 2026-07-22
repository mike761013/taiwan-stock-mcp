"""Pure V12 bullish-radar rules and trading-plan helpers.

V12 keeps the existing V11 strategies, adds ``reversal_reclaim`` for earlier
entries, applies V7-style liquidity gates, and separates signal quality from
whether the current price is still tradable.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Sequence


V12_STRATEGIES = ("early_stage", "breakout", "pullback", "reversal_reclaim")
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "v12_config.json"


@dataclass(frozen=True)
class V12Config:
    # V7 liquidity gates. Volumes are expressed in Taiwan lots (1 lot=1,000 shares).
    min_daily_volume_lots: float = 1000.0
    min_average_volume20_lots: float = 500.0
    min_trade_value: float = 50_000_000.0
    strict_min_trade_value: float = 100_000_000.0
    strict_liquidity: bool = False

    # Early reversal/reclaim pattern.
    reversal_min_change_pct: float = 5.0
    reversal_min_close_position: float = 0.80
    reversal_max_distance_ma20_pct: float = 2.0
    reversal_min_volume_ratio20: float = 0.40
    reversal_max_volume_ratio20: float = 2.50
    reversal_max_upper_shadow_pct: float = 2.50
    reversal_min_score: float = 65.0

    # ATR and stop management.
    atr_period: int = 14
    atr_stop_multiple: float = 0.25
    minimum_stop_buffer_pct: float = 2.0
    soft_break_reduce_ratio: float = 0.50
    max_entry_to_hard_stop_risk_pct: float = 8.0

    # Entry/no-chase bands.
    entry_low_atr_multiple: float = 0.30
    max_buy_atr_multiple: float = 0.20
    no_chase_atr_multiple: float = 0.60

    # Trading-quality penalties.
    ma20_distance_warning_pct: float = 8.0
    ma20_distance_danger_pct: float = 12.0
    bollinger_excess_warning_pct: float = 2.0
    extreme_volume_ratio: float = 5.0
    strong_day_change_pct: float = 8.5

    @property
    def effective_min_trade_value(self) -> float:
        return (
            self.strict_min_trade_value
            if self.strict_liquidity
            else self.min_trade_value
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "V12Config":
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["effective_min_trade_value"] = self.effective_min_trade_value
        return result


def load_v12_config(path: Path | None = None) -> V12Config:
    config_path = path or _CONFIG_PATH
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return V12Config()
    return V12Config.from_mapping(raw) if isinstance(raw, Mapping) else V12Config()


def _number(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(value, maximum))


def round_tw_price(value: float) -> float:
    """Approximate TWSE/TPEx tick rounding for displayed trading plans."""
    if value < 10:
        tick = 0.01
    elif value < 50:
        tick = 0.05
    elif value < 100:
        tick = 0.10
    elif value < 500:
        tick = 0.50
    elif value < 1000:
        tick = 1.00
    else:
        tick = 5.00
    return round(round(value / tick) * tick, 2)


def _close_position(row: Mapping[str, Any]) -> float:
    high = _number(row, "high")
    low = _number(row, "low")
    close = _number(row, "close")
    spread = high - low
    return (close - low) / spread if spread > 0 else 0.5


def _daily_change_pct(row: Mapping[str, Any]) -> float:
    close = _number(row, "close")
    previous_close = _number(row, "prev_close")
    return (close / previous_close - 1) * 100 if previous_close > 0 else 0.0


def _upper_shadow_pct(row: Mapping[str, Any]) -> float:
    high = _number(row, "high")
    close = _number(row, "close")
    open_price = _number(row, "open")
    return max(0.0, high - max(open_price, close)) / close * 100 if close > 0 else 0.0


def _distance_pct(value: float, reference: float, absolute: bool = False) -> float:
    if reference <= 0:
        return 0.0
    result = (value - reference) / reference * 100
    return abs(result) if absolute else result


def _bullish_engulfing(row: Mapping[str, Any]) -> bool:
    current_open = _number(row, "open")
    current_close = _number(row, "close")
    previous_open = _number(row, "prev_open")
    previous_close = _number(row, "prev_close")
    return (
        current_close > current_open
        and previous_close < previous_open
        and min(current_open, current_close) <= min(previous_open, previous_close)
        and max(current_open, current_close) >= max(previous_open, previous_close)
    )


def liquidity_result(row: Mapping[str, Any], config: V12Config) -> dict[str, Any]:
    volume = _number(row, "volume")
    volume_ma20 = _number(row, "volume_ma20")
    close = _number(row, "close")
    turnover = _number(row, "turnover") or close * volume
    daily_lots = volume / 1000.0
    average_lots = volume_ma20 / 1000.0
    failures: list[str] = []

    if daily_lots < config.min_daily_volume_lots:
        failures.append(
            f"單日成交量{daily_lots:.0f}張低於{config.min_daily_volume_lots:.0f}張"
        )
    if average_lots < config.min_average_volume20_lots:
        failures.append(
            f"20日均量{average_lots:.0f}張低於{config.min_average_volume20_lots:.0f}張"
        )
    if turnover < config.effective_min_trade_value:
        failures.append(
            f"成交金額{turnover:,.0f}元低於{config.effective_min_trade_value:,.0f}元"
        )

    return {
        "eligible": not failures,
        "dailyVolumeLots": round(daily_lots, 2),
        "averageVolume20Lots": round(average_lots, 2),
        "tradeValue": round(turnover, 2),
        "failedRules": failures,
    }


def _v11_base_score(row: Mapping[str, Any]) -> float:
    score = _number(row, "technical_score")
    if _number(row, "volume_ratio") >= 1.2:
        score += 8
    if _number(row, "ma5") > _number(row, "ma20") > 0:
        score += 5
    return min(score, 100.0)


def reversal_reclaim_score(
    row: Mapping[str, Any], config: V12Config
) -> tuple[bool, float, list[str], list[str]]:
    close = _number(row, "close")
    open_price = _number(row, "open")
    prev_high = _number(row, "prev_high")
    ma5 = _number(row, "ma5")
    ma10 = _number(row, "ma10")
    ma20 = _number(row, "ma20")
    volume_ratio = _number(row, "volume_ratio")
    close_position = _close_position(row)
    change_pct = _daily_change_pct(row)
    upper_shadow = _upper_shadow_pct(row)
    ma20_distance = _distance_pct(close, ma20, absolute=True)

    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    if close > prev_high > 0:
        score += 15
        reasons.append("收盤突破前一日高點")
    if close > ma5 > 0:
        score += 10
        reasons.append("站回MA5")
    if close > ma10 > 0:
        score += 10
        reasons.append("站回MA10")
    if ma20 > 0 and ma20_distance <= config.reversal_max_distance_ma20_pct:
        score += 15
        reasons.append("貼近MA20，尚未明顯乖離")
    if close_position >= config.reversal_min_close_position:
        score += 15
        reasons.append("收盤接近當日最高")
    if change_pct >= config.reversal_min_change_pct:
        score += 10
        reasons.append("單日強勢反轉")
    if config.reversal_min_volume_ratio20 <= volume_ratio < 1.0:
        score += 10
        reasons.append("低量強漲，疑似供給收縮")
    elif 1.0 <= volume_ratio <= config.reversal_max_volume_ratio20:
        score += 7
        reasons.append("量能溫和放大")
    if _bullish_engulfing(row):
        score += 10
        reasons.append("多方吞噬前一日黑K")
    if close > open_price:
        score += 5

    if upper_shadow > config.reversal_max_upper_shadow_pct:
        score -= 15
        warnings.append("上影線偏長")
    if ma20_distance > 5.0:
        score -= 15
        warnings.append("距MA20過遠")
    if volume_ratio > 4.0:
        score -= 10
        warnings.append("爆量過熱")

    core_pass = all(
        (
            close > prev_high > 0,
            close > ma5 > 0,
            close > ma10 > 0,
            ma20 > 0 and ma20_distance <= config.reversal_max_distance_ma20_pct,
            close_position >= config.reversal_min_close_position,
            change_pct >= config.reversal_min_change_pct,
            config.reversal_min_volume_ratio20
            <= volume_ratio
            <= config.reversal_max_volume_ratio20,
            upper_shadow <= config.reversal_max_upper_shadow_pct,
        )
    )
    score = _clamp(score)
    return core_pass and score >= config.reversal_min_score, score, reasons, warnings


def strategy_passes(row: Mapping[str, Any], strategy: str, config: V12Config) -> bool:
    close = _number(row, "close")
    ma5 = _number(row, "ma5")
    ma20 = _number(row, "ma20")
    ma60 = _number(row, "ma60")
    bollinger_upper = _number(row, "bollinger_upper")
    volume_ratio = _number(row, "volume_ratio")

    if strategy == "early_stage":
        return (
            ma5 >= ma20 > 0
            and close >= ma20
            and 0.8 <= volume_ratio <= 2.5
        )
    if strategy == "breakout":
        return (
            bollinger_upper > 0
            and close >= bollinger_upper
            and volume_ratio >= 1.2
        )
    if strategy == "pullback":
        return (
            ma20 >= ma60 > 0
            and close >= ma20
            and ma5 > 0
            and close <= ma5 * 1.03
        )
    if strategy == "reversal_reclaim":
        passed, _, _, _ = reversal_reclaim_score(row, config)
        return passed
    raise ValueError(f"strategy must be one of {V12_STRATEGIES}")


def _strategy_reasons(row: Mapping[str, Any], strategy: str) -> list[str]:
    reasons: list[str] = []
    if _number(row, "ma5") > _number(row, "ma20") > 0:
        reasons.append("MA5高於MA20")
    if _number(row, "volume_ratio") >= 1.2:
        reasons.append("量比放大")
    if (
        _number(row, "large_volume_low") > 0
        and _number(row, "close") >= _number(row, "large_volume_low")
    ):
        reasons.append("守住滾動大量低點")
    if strategy == "breakout":
        reasons.append("收盤突破布林上軌")
    elif strategy == "pullback":
        reasons.append("多頭趨勢內回測短均線")
    elif strategy == "early_stage":
        reasons.append("短均線剛轉強")
    return reasons


def trading_adjustment(
    row: Mapping[str, Any], config: V12Config
) -> tuple[float, list[str]]:
    close = _number(row, "close")
    open_price = _number(row, "open")
    ma20 = _number(row, "ma20")
    bollinger_upper = _number(row, "bollinger_upper")
    volume_ratio = _number(row, "volume_ratio")
    close_position = _close_position(row)
    change_pct = _daily_change_pct(row)
    distance_ma20 = _distance_pct(close, ma20)
    bollinger_excess = _distance_pct(close, bollinger_upper) if bollinger_upper > 0 else 0

    adjustment = 0.0
    warnings: list[str] = []

    if distance_ma20 >= config.ma20_distance_danger_pct:
        adjustment -= 20
        warnings.append(f"距MA20達{distance_ma20:.1f}%，嚴重乖離")
    elif distance_ma20 >= config.ma20_distance_warning_pct:
        adjustment -= 10
        warnings.append(f"距MA20達{distance_ma20:.1f}%，乖離偏高")

    if bollinger_excess >= config.bollinger_excess_warning_pct:
        adjustment -= 10
        warnings.append("明顯站上布林上軌，隔日追價風險高")

    if volume_ratio >= config.extreme_volume_ratio:
        if close_position < 0.60:
            adjustment -= 20
            warnings.append("極端爆量但收盤位置不佳，疑似出貨")
        else:
            adjustment -= 8
            warnings.append("極端爆量，短線過熱")

    # A strong reversal near MA20 with restrained volume should not be punished.
    if (
        change_pct >= config.strong_day_change_pct
        and distance_ma20 >= 5.0
    ):
        adjustment -= 8
        warnings.append("單日漲幅與乖離同時偏大")

    if close < open_price and close_position <= 0.25:
        adjustment -= 15
        warnings.append("開高走低並收近低點")

    return adjustment, warnings


def build_trading_plan(
    row: Mapping[str, Any], strategy: str, config: V12Config
) -> dict[str, Any]:
    close = _number(row, "close")
    low = _number(row, "low") or close
    ma20 = _number(row, "ma20")
    atr14 = _number(row, "atr14")
    prev_high = _number(row, "prev_high")
    bollinger_upper = _number(row, "bollinger_upper")

    signal_defense = low
    stop_buffer = max(
        signal_defense * config.minimum_stop_buffer_pct / 100.0,
        atr14 * config.atr_stop_multiple,
    )
    hard_stop = max(0.01, signal_defense - stop_buffer)

    if strategy == "breakout":
        trigger_candidates = [value for value in (prev_high, bollinger_upper) if value > 0]
        trigger = max(trigger_candidates) if trigger_candidates else close
        entry_low = max(signal_defense, trigger - atr14 * 0.20)
        entry_high = trigger + atr14 * 0.10
    elif strategy == "reversal_reclaim":
        entry_low = max(signal_defense, close - atr14 * config.entry_low_atr_multiple)
        entry_high = max(close, ma20 if ma20 > 0 else close)
    else:
        reference = ma20 if ma20 > 0 else close
        entry_low = max(signal_defense, reference - atr14 * 0.20)
        entry_high = min(close, reference + atr14 * 0.20) if close >= reference else close

    maximum_buy = entry_high + atr14 * config.max_buy_atr_multiple
    no_chase = entry_high + atr14 * config.no_chase_atr_multiple
    risk_pct = (entry_high - hard_stop) / entry_high * 100 if entry_high > 0 else 0.0

    if risk_pct > config.max_entry_to_hard_stop_risk_pct:
        status = "EARLY_ENTRY_SMALL_POSITION" if strategy == "reversal_reclaim" else "SMALL_POSITION_OR_SKIP"
        initial_position = 30
    elif strategy == "reversal_reclaim":
        status = "EARLY_ENTRY"
        initial_position = 40
    elif close > no_chase:
        status = "DO_NOT_CHASE"
        initial_position = 0
    elif strategy == "breakout":
        status = "BUY_ON_BREAKOUT"
        initial_position = 40
    elif strategy == "pullback":
        status = "BUY_ZONE"
        initial_position = 40
    else:
        status = "PRICE_CONFIRMATION_REQUIRED"
        initial_position = 30

    return {
        "status": status,
        "signalDate": str(row.get("trade_date") or ""),
        "signalPrice": round_tw_price(close),
        "idealEntryLow": round_tw_price(entry_low),
        "idealEntryHigh": round_tw_price(entry_high),
        "maximumBuyPrice": round_tw_price(maximum_buy),
        "noChasePrice": round_tw_price(no_chase),
        "signalDefensePrice": round_tw_price(signal_defense),
        "hardStopPrice": round_tw_price(hard_stop),
        "atr14": round_tw_price(atr14),
        "initialPositionPercent": initial_position,
        "maximumRiskPercent": round(risk_pct, 2),
        "softBreakAction": f"減碼{int(config.soft_break_reduce_ratio * 100)}%，進入SHAKEOUT_WATCH",
        "hardStopAction": "收盤確認跌破後退出剩餘部位",
        "reclaimAction": "快速收復固定防守與前一日黑K中值時分批買回",
    }


def build_v12_candidate(
    row: Mapping[str, Any], strategy: str, config: V12Config
) -> dict[str, Any] | None:
    liquidity = liquidity_result(row, config)
    if not liquidity["eligible"] or not strategy_passes(row, strategy, config):
        return None

    if strategy == "reversal_reclaim":
        _, raw_score, reasons, pattern_warnings = reversal_reclaim_score(row, config)
    else:
        raw_score = _v11_base_score(row)
        reasons = _strategy_reasons(row, strategy)
        pattern_warnings = []

    adjustment, trading_warnings = trading_adjustment(row, config)
    final_score = _clamp(raw_score + adjustment)
    plan = build_trading_plan(row, strategy, config)

    # A heavily penalised setup is observable but should not be presented as a buy.
    action = plan["status"]
    if adjustment <= -25:
        action = "DO_NOT_CHASE"
    elif adjustment <= -10 and action not in {"SMALL_POSITION_OR_SKIP", "EARLY_ENTRY_SMALL_POSITION"}:
        action = "WAIT_PULLBACK"

    item = dict(row)
    item.update(
        {
            "strategy": strategy,
            "strategies": [strategy],
            "raw_score": round(raw_score, 2),
            "trading_adjustment": round(adjustment, 2),
            "total_score": round(final_score, 2),
            "finalScore": round(final_score, 2),
            "action": action,
            "reasons": reasons,
            "warnings": pattern_warnings + trading_warnings,
            "liquidity": liquidity,
            "tradingPlan": plan,
            # These are deliberately frozen in the signal snapshot.
            "signal_defense_price": plan["signalDefensePrice"],
            "hard_stop_price": plan["hardStopPrice"],
            # This remains rolling for reference only, never for retroactive stop changes.
            "rolling_massive_volume_low": _number(row, "large_volume_low") or None,
            "dailyChangePercent": round(_daily_change_pct(row), 2),
            "closePosition": round(_close_position(row), 4),
            "upperShadowPercent": round(_upper_shadow_pct(row), 2),
        }
    )
    return item


def screen_v12_rows(
    rows: Sequence[Mapping[str, Any]],
    strategy: str,
    minimum_score: float,
    limit: int,
    config: V12Config,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    liquidity_rejected = pattern_rejected = score_rejected = 0

    for row in rows:
        liquidity = liquidity_result(row, config)
        if not liquidity["eligible"]:
            liquidity_rejected += 1
            continue
        if not strategy_passes(row, strategy, config):
            pattern_rejected += 1
            continue
        candidate = build_v12_candidate(row, strategy, config)
        if candidate is None:
            pattern_rejected += 1
            continue
        if float(candidate["total_score"]) < minimum_score:
            score_rejected += 1
            continue
        accepted.append(candidate)

    accepted.sort(
        key=lambda item: (
            float(item.get("total_score") or 0),
            -float(item.get("tradingPlan", {}).get("maximumRiskPercent") or 999),
            float(item.get("volume_ratio") or 0),
        ),
        reverse=True,
    )
    accepted = accepted[:limit]
    for rank, item in enumerate(accepted, start=1):
        item["rank"] = rank

    return accepted, {
        "liquidityRejected": liquidity_rejected,
        "patternRejected": pattern_rejected,
        "scoreRejected": score_rejected,
    }
