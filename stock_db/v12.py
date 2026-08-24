"""Pure V12 bullish-radar rules and trading-plan helpers.

V12 keeps the existing V11 strategies, uses ``reversal_reclaim`` for bottoming
signals near recent lows, applies V7-style liquidity gates, and separates
signal quality from whether the current price is still tradable.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Sequence


V12_STRATEGIES = ("early_stage", "breakout", "pullback", "reversal_reclaim")
V12_ACCURACY_ENGINE = "V12.3.1_SEVEN_FACTOR_FIX"
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "v12_config.json"

V12_STATUS_LABELS = {
    "BUY_ZONE": "買進區",
    "BUY_ON_BREAKOUT": "突破時買進",
    "EARLY_ENTRY": "早期進場",
    "EARLY_ENTRY_SMALL_POSITION": "早期進場（小部位）",
    "BOTTOM_REVERSAL_WATCH": "底部止跌觀察",
    "PRICE_CONFIRMATION_REQUIRED": "等待價格確認",
    "SMALL_POSITION_OR_SKIP": "小部位或略過",
    "WAIT_PULLBACK": "等待拉回",
    "DO_NOT_CHASE": "不追價",
}

V12_HIGH_PRICE_ELIGIBLE_STATUS_CODES = frozenset(
    {
        "BUY_ZONE",
        "BUY_ON_BREAKOUT",
        "EARLY_ENTRY",
        "EARLY_ENTRY_SMALL_POSITION",
        "PRICE_CONFIRMATION_REQUIRED",
    }
)

V12_ACTIONABLE_STATUS_CODES = frozenset(
    {
        "BUY_ZONE",
        "BUY_ON_BREAKOUT",
        "EARLY_ENTRY",
        "EARLY_ENTRY_SMALL_POSITION",
        "PRICE_CONFIRMATION_REQUIRED",
    }
)


def v12_status_label(status_code: str) -> str:
    """Return a Chinese display label while keeping a stable machine code."""
    return V12_STATUS_LABELS.get(status_code, status_code)


@dataclass(frozen=True)
class V12Config:
    # V7 liquidity gates. Volumes are expressed in Taiwan lots (1 lot=1,000 shares).
    min_daily_volume_lots: float = 2000.0
    min_average_volume20_lots: float = 1000.0
    min_trade_value: float = 100_000_000.0
    strict_min_trade_value: float = 200_000_000.0
    strict_liquidity: bool = False

    # Price-tier display rules. The main radar is affordable-stock first.
    primary_max_price: float = 200.0
    high_price_min_score: float = 85.0
    high_price_max_risk_pct: float = 6.0
    high_price_limit: int = 5

    # Bottom reversal/reclaim pattern. A candidate must still be close to its
    # recent low; a one-day surge near MA20 is not sufficient by itself.
    reversal_min_change_pct: float = 5.0
    reversal_min_close_position: float = 0.55
    reversal_max_distance_ma20_pct: float = 2.0
    reversal_min_volume_ratio20: float = 0.40
    reversal_max_volume_ratio20: float = 2.50
    reversal_max_upper_shadow_pct: float = 2.50
    reversal_min_score: float = 65.0
    reversal_max_range_position20: float = 0.45
    reversal_max_distance_low20_pct: float = 15.0
    reversal_max_distance_low20_atr: float = 2.50
    reversal_min_drawdown20_pct: float = 10.0
    reversal_min_stabilization_signals: int = 2

    # Pullback V3: strict bullish alignment while MA5 and the rolling
    # high-volume low remain intact. A down close is allowed and rewarded when
    # volume contracts.
    pullback_max_distance_ma5_pct: float = 3.0
    pullback_max_distance_ma10_pct: float = 3.0
    pullback_max_distance_ma20_pct: float = 4.0
    pullback_max_below_ma20_pct: float = 2.0
    pullback_max_volume_ratio20: float = 1.50
    pullback_preferred_volume_ratio20: float = 0.80
    pullback_min_stabilization_signals: int = 2

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
    aggressive_entry_atr_multiple: float = 0.10
    confirmation_ma5_atr_discount: float = 0.20

    # Trading-quality penalties.
    ma20_distance_warning_pct: float = 8.0
    ma20_distance_danger_pct: float = 12.0
    bollinger_excess_warning_pct: float = 2.0
    extreme_volume_ratio: float = 5.0
    strong_day_change_pct: float = 8.5

    # V12.1 forward-bullish ranking.  The legacy technical score is useful,
    # but it over-rewards a stock merely for already being extended.  Blend it
    # with structure, candle quality, trend slope and support quality instead.
    bullish_raw_score_weight: float = 0.55
    bullish_quality_score_weight: float = 0.45
    ranking_bullish_weight: float = 0.75
    ranking_execution_weight: float = 0.25
    consensus_bonus_per_extra_strategy: float = 2.0
    consensus_bonus_cap: float = 4.0

    # Immediate false-breakout / late-entry protection.  V12.2 deliberately
    # keeps overheated names in the watch list while requiring a healthier
    # close and less extension before they can enter the formal top ten.
    breakout_min_close_position: float = 0.65
    breakout_max_distance_ma20_pct: float = 9.0
    breakout_max_volume_ratio20: float = 3.2
    predictive_max_5day_change_pct: float = 10.0
    predictive_max_upper_shadow_pct: float = 2.5

    # Early-stage candidates must still be close enough to the base.  Missing
    # previous-MA fields remain backward compatible with older snapshots.
    early_max_distance_ma20_pct: float = 5.5
    early_max_volume_ratio20: float = 2.2
    early_min_close_position: float = 0.52

    # V12.2 forward-persistence qualification.  Candidates that fail these
    # rules are still returned for observation, but are excluded from the
    # formal actionable ranking until price structure confirms.
    forward_min_bullish_score: float = 65.0
    forward_min_quality_early: float = 64.0
    forward_min_quality_breakout: float = 68.0
    forward_min_quality_pullback: float = 66.0
    forward_min_quality_reversal: float = 68.0
    forward_ma20_slope_tolerance_pct: float = 0.15
    forward_ma5_slope_tolerance_pct: float = 0.35
    pullback_down_day_min_close_position: float = 0.45
    pullback_down_day_max_volume_ratio20: float = 1.0
    reversal_actionable_requires_ma20_reclaim: bool = True

    # Breadth and industry context are calculated from the same daily
    # snapshot, so no additional API quota is consumed.
    market_weak_breadth_pct: float = 45.0
    market_strong_breadth_pct: float = 60.0
    market_weak_score_penalty: float = 4.0
    market_weak_breakout_min_quality: float = 75.0
    sector_relative_breadth_threshold_pct: float = 15.0
    sector_strong_score_bonus: float = 2.0
    sector_weak_score_penalty: float = 3.0
    sector_minimum_members: int = 5

    # Historical execution performance is only allowed to make a small,
    # confidence-weighted adjustment.  This prevents a short hot streak from
    # taking over the model.
    execution_prior_min_samples: int = 30
    execution_prior_full_confidence_samples: int = 120
    execution_prior_max_adjustment: float = 8.0
    execution_entry_window_sessions: int = 3

    # V12.3.1 seven-factor formal ranking. Missing provider data is omitted and
    # the remaining weights are normalised; it is never silently scored zero.
    factor_weight_technical: float = 30.0
    factor_weight_chip: float = 20.0
    factor_weight_fundamental: float = 15.0
    factor_weight_theme: float = 15.0
    factor_weight_intraday: float = 10.0
    factor_weight_market: float = 5.0
    factor_weight_history: float = 5.0
    factor_minimum_confidence: float = 60.0
    factor_api_concurrency: int = 3
    factor_prefilter_limit: int = 40
    historical_retention_years: int = 5

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


def _lower_shadow_pct(row: Mapping[str, Any]) -> float:
    low = _number(row, "low")
    close = _number(row, "close")
    open_price = _number(row, "open")
    return max(0.0, min(open_price, close) - low) / close * 100 if close > 0 else 0.0


def _distance_pct(value: float, reference: float, absolute: bool = False) -> float:
    if reference <= 0:
        return 0.0
    result = (value - reference) / reference * 100
    return abs(result) if absolute else result


def _slope_not_declining(
    current: float,
    previous: float,
    tolerance_pct: float,
) -> bool:
    """Treat missing history as unknown, otherwise reject a real downslope."""
    if current <= 0 or previous <= 0:
        return True
    return current >= previous * (1.0 - max(0.0, tolerance_pct) / 100.0)


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


def _bottom_context(row: Mapping[str, Any]) -> dict[str, float]:
    """Return normalized recent-bottom measurements supplied by radar.py."""
    close = _number(row, "close")
    high20 = _number(row, "high20")
    low20 = _number(row, "low20")
    atr14 = _number(row, "atr14")
    span20 = high20 - low20
    distance_from_low = max(0.0, close - low20)
    return {
        "high20": high20,
        "low20": low20,
        "rangePosition20": distance_from_low / span20 if span20 > 0 else 999.0,
        "distanceFromLow20Percent": (
            distance_from_low / low20 * 100 if low20 > 0 else 999.0
        ),
        "distanceFromLow20ATR": (
            distance_from_low / atr14 if atr14 > 0 else 999.0
        ),
        "drawdownFromHigh20Percent": (
            max(0.0, (high20 - close) / high20 * 100) if high20 > 0 else 0.0
        ),
    }


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
    """Score a bottoming setup instead of rewarding a one-day price spike."""
    close = _number(row, "close")
    open_price = _number(row, "open")
    low = _number(row, "low")
    prev_high = _number(row, "prev_high")
    prev_low = _number(row, "prev_low")
    prev_close = _number(row, "prev_close")
    prev2_low = _number(row, "prev2_low")
    ma5 = _number(row, "ma5")
    ma10 = _number(row, "ma10")
    ma20 = _number(row, "ma20")
    volume_ratio = _number(row, "volume_ratio")
    large_volume_low = _number(row, "large_volume_low")
    close_position = _close_position(row)
    change_pct = _daily_change_pct(row)
    upper_shadow = _upper_shadow_pct(row)
    lower_shadow = _lower_shadow_pct(row)
    ma20_distance = _distance_pct(close, ma20, absolute=True)
    bottom = _bottom_context(row)

    score = 0.0
    reasons: list[str] = []
    warnings: list[str] = []
    stabilization_signals: list[str] = []

    near_bottom = (
        bottom["high20"] > bottom["low20"] > 0
        and 0.0 <= bottom["rangePosition20"] <= config.reversal_max_range_position20
        and bottom["distanceFromLow20Percent"]
        <= config.reversal_max_distance_low20_pct
        and bottom["distanceFromLow20ATR"]
        <= config.reversal_max_distance_low20_atr
        and bottom["drawdownFromHigh20Percent"]
        >= config.reversal_min_drawdown20_pct
    )
    if near_bottom:
        score += 30
        reasons.append(
            f"接近20日底部：距低點{bottom['distanceFromLow20Percent']:.1f}%"
        )
        reasons.append(
            f"仍較20日高點回落{bottom['drawdownFromHigh20Percent']:.1f}%"
        )
    else:
        warnings.append("已不在設定的20日底部區，排除單日急彈假訊號")

    if prev_low > 0 and low >= prev_low:
        stabilization_signals.append("低點未再下移")
        score += 12
    elif prev2_low > 0 and low >= prev2_low:
        stabilization_signals.append("未再跌破前兩日低點")
        score += 8
    if prev_close > 0 and close > prev_close:
        stabilization_signals.append("收盤高於前一日")
        score += 10
    if close_position >= config.reversal_min_close_position:
        stabilization_signals.append("收盤位於當日振幅上半部")
        score += 10
    if close >= ma5 > 0:
        stabilization_signals.append("站回MA5")
        score += 10
    if _bullish_engulfing(row):
        stabilization_signals.append("多方吞噬前一日黑K")
        score += 10
    if lower_shadow >= 1.0:
        stabilization_signals.append("出現承接下影線")
        score += 6

    if close > prev_high > 0:
        score += 6
        reasons.append("收盤突破前一日高點")
    if close > ma10 > 0:
        score += 5
        reasons.append("站回MA10")
    if ma20 > 0 and ma20_distance <= config.reversal_max_distance_ma20_pct:
        score += 5
        reasons.append("接近MA20壓力區")
    if change_pct >= config.reversal_min_change_pct:
        score += 4
        reasons.append("單日反彈達反轉觀察門檻")
    if config.reversal_min_volume_ratio20 <= volume_ratio < 1.0:
        score += 8
        reasons.append("量能收斂，賣壓減輕")
    elif 1.0 <= volume_ratio <= config.reversal_max_volume_ratio20:
        score += 5
        reasons.append("量能溫和放大")
    if close > open_price:
        score += 3

    reasons.extend(f"止跌：{signal}" for signal in stabilization_signals)

    if upper_shadow > config.reversal_max_upper_shadow_pct:
        score -= 10
        warnings.append("上影線偏長")
    if volume_ratio > 4.0:
        score -= 10
        warnings.append("爆量過熱")
    if large_volume_low > 0 and close < large_volume_low:
        score -= 8
        warnings.append("尚未收復滾動大量低點")
    if change_pct >= config.strong_day_change_pct:
        warnings.append("單日漲幅過大，只列觀察、不追價")

    stabilization_ok = (
        len(stabilization_signals) >= config.reversal_min_stabilization_signals
    )
    recovery_ok = (
        close > prev_close > 0
        or close >= ma5 > 0
        or _bullish_engulfing(row)
    )
    core_pass = (
        near_bottom
        and stabilization_ok
        and recovery_ok
        and close_position >= config.reversal_min_close_position
        and config.reversal_min_volume_ratio20
        <= volume_ratio
        <= config.reversal_max_volume_ratio20
    )
    score = _clamp(score)
    return core_pass and score >= config.reversal_min_score, score, reasons, warnings



def pullback_v2_score(
    row: Mapping[str, Any], config: V12Config
) -> tuple[bool, float, list[str], list[str], list[str]]:
    """Score a healthy bullish pullback; return pass, score, reasons, warnings, signals."""
    close = _number(row, "close")
    low = _number(row, "low")
    prev_low = _number(row, "prev_low")
    prev_high = _number(row, "prev_high")
    ma5 = _number(row, "ma5")
    ma10 = _number(row, "ma10")
    ma20 = _number(row, "ma20")
    ma60 = _number(row, "ma60")
    prev_ma5 = _number(row, "prev_ma5")
    prev_ma20 = _number(row, "prev_ma20")
    volume_ratio = _number(row, "volume_ratio")
    large_volume_low = _number(row, "large_volume_low")
    close_position = _close_position(row)

    reasons: list[str] = []
    warnings: list[str] = []
    signals: list[str] = []
    score = 0.0

    # The user-defined pullback is deliberately strict: the entire moving-
    # average stack must be bullish and both MA5 and the rolling high-volume
    # low must remain intact on a closing basis. The candle may close down.
    trend_ok = ma5 > ma10 > ma20 > ma60 > 0
    if trend_ok:
        score += 25
        reasons.append("MA5>MA10>MA20>MA60，完整多頭排列")
    else:
        warnings.append("均線未形成MA5>MA10>MA20>MA60完整多頭排列")

    ma20_persistent = _slope_not_declining(
        ma20,
        prev_ma20,
        config.forward_ma20_slope_tolerance_pct,
    )
    ma5_persistent = _slope_not_declining(
        ma5,
        prev_ma5,
        config.forward_ma5_slope_tolerance_pct,
    )
    if ma20_persistent:
        score += 5
        if prev_ma20 > 0:
            reasons.append("MA20未轉為明顯下彎")
    else:
        score -= 12
        warnings.append("MA20已下彎，不視為健康多頭回檔")
    if not ma5_persistent:
        score -= 6
        warnings.append("MA5斜率快速轉弱")

    dist5 = _distance_pct(close, ma5) if ma5 > 0 else 999.0
    ma5_hold = ma5 > 0 and close >= ma5
    near_ma5 = ma5_hold and dist5 <= config.pullback_max_distance_ma5_pct
    if ma5_hold:
        score += 20
        reasons.append("收盤守住MA5")
    else:
        warnings.append("收盤跌破MA5")
    if near_ma5:
        score += 10 if dist5 > 1.0 else 15
        reasons.append(f"收盤距MA5僅{dist5:.1f}%")
    elif ma5_hold:
        warnings.append("雖守住MA5，但距離已超出健康回檔區")

    # Healthy pullbacks should contract in volume. Unlike the old shared
    # scoring model, low volume is a positive feature here.
    if 0 < volume_ratio <= config.pullback_preferred_volume_ratio20:
        score += 10
        reasons.append("量縮，賣壓收斂")
    elif volume_ratio <= 1.0:
        score += 7
        reasons.append("量能低於20日均量")
    elif volume_ratio <= config.pullback_max_volume_ratio20:
        score += 2
        warnings.append("量能略高，仍需觀察賣壓")
    else:
        score -= 12
        warnings.append("量能過大，不屬健康回檔")

    massive_low_hold = large_volume_low > 0 and close >= large_volume_low
    if massive_low_hold:
        score += 15
        reasons.append("收盤守住滾動大量低點")
    elif large_volume_low <= 0:
        warnings.append("缺少滾動大量低點，無法確認支撐")
    else:
        warnings.append("收盤跌破滾動大量低點")

    change_pct = _daily_change_pct(row)
    down_day_quality = (
        change_pct < 0
        and 0 < volume_ratio <= config.pullback_down_day_max_volume_ratio20
        and close_position >= config.pullback_down_day_min_close_position
        and prev_low > 0
        and low >= prev_low
        and ma20_persistent
        and ma5_persistent
    )
    if change_pct < 0:
        if down_day_quality:
            score += 10
            reasons.append("收跌但量縮、低點未下移且收盤位置健康")
        else:
            score -= 8
            warnings.append("收跌日缺少量縮、低點墊高或收盤承接，不先假設是健康整理")
    else:
        score += 4

    # Stabilisation: require at least two independent signs before promotion.
    if prev_low > 0 and low >= prev_low:
        signals.append("低點未再下移")
        score += 6
    if close_position >= 0.55:
        signals.append("收盤位於當日振幅上半部")
        score += 5
    if low >= ma5 > 0:
        signals.append("盤中與收盤都守住MA5")
        score += 5
    elif ma5_hold:
        signals.append("盤中跌破MA5後收回")
        score += 3
    if prev_high > 0 and close > prev_high:
        signals.append("突破前一日高點")
        score += 8
    if close > _number(row, "open"):
        signals.append("收紅K")
        score += 4

    stabilization_ok = (
        len(signals) >= config.pullback_min_stabilization_signals
    )
    if not stabilization_ok:
        warnings.append(
            f"止穩訊號僅{len(signals)}項，低於"
            f"{config.pullback_min_stabilization_signals}項"
        )

    core_pass = (
        trend_ok
        and ma20_persistent
        and near_ma5
        and massive_low_hold
        and 0 < volume_ratio <= config.pullback_max_volume_ratio20
        and stabilization_ok
        and (change_pct >= 0 or down_day_quality)
    )
    return core_pass, _clamp(score), reasons, warnings, signals


def _five_day_change_pct(row: Mapping[str, Any]) -> float:
    close = _number(row, "close")
    close5 = _number(row, "close5")
    return (close / close5 - 1) * 100 if close5 > 0 else 0.0


def predictive_quality_score(
    row: Mapping[str, Any], strategy: str, config: V12Config
) -> tuple[float, list[str], list[str]]:
    """Score features that are more likely to persist after the signal day.

    The old technical score mostly answered "is this stock already strong?".
    This score instead rewards rising structure, support, a healthy close and
    non-exhaustive volume, while penalising late five-day acceleration and
    upper-shadow rejection.  Every input is available on the signal date, so
    the score has no future-data leakage.
    """
    close = _number(row, "close")
    open_price = _number(row, "open")
    ma5 = _number(row, "ma5")
    ma10 = _number(row, "ma10")
    ma20 = _number(row, "ma20")
    ma60 = _number(row, "ma60")
    prev_ma5 = _number(row, "prev_ma5")
    prev_ma20 = _number(row, "prev_ma20")
    prev_high = _number(row, "prev_high")
    prev_low = _number(row, "prev_low")
    prev_close = _number(row, "prev_close")
    volume_ratio = _number(row, "volume_ratio")
    large_volume_low = _number(row, "large_volume_low")
    close_position = _close_position(row)
    upper_shadow = _upper_shadow_pct(row)
    change_pct = _daily_change_pct(row)
    change5 = _five_day_change_pct(row)
    dist20 = _distance_pct(close, ma20) if ma20 > 0 else 999.0
    dist5 = _distance_pct(close, ma5) if ma5 > 0 else 999.0
    bottom = _bottom_context(row)

    score = 50.0
    positives: list[str] = []
    risks: list[str] = []

    if prev_ma20 > 0:
        if ma20 > prev_ma20 * 1.001:
            score += 8
            positives.append("MA20持續上彎")
        elif ma20 >= prev_ma20 * 0.999:
            score += 2
        else:
            score -= 10
            risks.append("MA20仍下彎")
    if prev_ma5 > 0:
        if ma5 > prev_ma5:
            score += 6
            positives.append("MA5斜率向上")
        else:
            score -= 4
            risks.append("MA5斜率轉弱")

    if close_position >= 0.70:
        score += 8
        positives.append("收盤接近日高")
    elif close_position >= 0.55:
        score += 4
    elif close_position <= 0.30:
        score -= 10
        risks.append("收盤接近日低")

    if upper_shadow <= 1.5:
        score += 4
    elif upper_shadow > config.predictive_max_upper_shadow_pct:
        score -= 10
        risks.append("長上影顯示追價承接不足")
    if close > open_price > 0:
        score += 4

    if large_volume_low > 0:
        if close >= large_volume_low:
            score += 6
            positives.append("守住滾動大量低點")
        else:
            score -= 12
            risks.append("收盤仍在滾動大量低點下方")

    if dist20 > config.ma20_distance_danger_pct:
        score -= 18
        risks.append("距MA20過遠，後續回歸風險高")
    elif dist20 > config.ma20_distance_warning_pct:
        score -= 10
        risks.append("距MA20偏遠")

    if change5 > config.predictive_max_5day_change_pct:
        score -= 15
        risks.append("五日漲幅過大，容易進入短線兌現")
    elif change5 > config.predictive_max_5day_change_pct * 0.67:
        score -= 8
        risks.append("五日漲幅偏快")
    if change_pct >= config.strong_day_change_pct:
        score -= 10
        risks.append("訊號日漲幅過大")

    if strategy == "early_stage":
        if ma5 > ma10 > ma20 > 0:
            score += 10
            positives.append("短中期均線剛形成多頭結構")
        elif ma5 >= ma20 > 0:
            score += 5
        if 0 <= dist20 <= 4.0:
            score += 10
            positives.append("仍靠近MA20基座")
        elif dist20 <= config.early_max_distance_ma20_pct:
            score += 5
        if 0.8 <= volume_ratio <= 1.8:
            score += 8
            positives.append("量能溫和而非爆量")
        elif volume_ratio > config.early_max_volume_ratio20:
            score -= 8
            risks.append("早期型態卻已明顯爆量")
        if 0.25 <= bottom["rangePosition20"] <= 0.80:
            score += 6
        elif bottom["rangePosition20"] > 0.90:
            score -= 6
            risks.append("已接近20日區間頂端")

    elif strategy == "breakout":
        if prev_high > 0 and close >= prev_high:
            score += 8
            positives.append("收盤有效越過前一日高點")
        elif prev_high > 0:
            score -= 10
            risks.append("未能收過前一日高點")
        if close_position >= 0.70:
            score += 8
        if 1.2 <= volume_ratio <= 2.5:
            score += 8
            positives.append("突破量能健康")
        elif volume_ratio <= 3.5:
            score += 3
        else:
            score -= 10
            risks.append("突破量能疑似高潮量")
        if 0 <= dist20 <= 6.0:
            score += 6
        elif dist20 > 8.0:
            score -= 10
            risks.append("突破位置離MA20過遠")

    elif strategy == "pullback":
        if ma5 > ma10 > ma20 > ma60 > 0:
            score += 10
            positives.append("完整多頭排列")
        if 0 <= dist5 <= 1.5:
            score += 10
            positives.append("貼近MA5且尚未失守")
        elif 0 <= dist5 <= config.pullback_max_distance_ma5_pct:
            score += 5
        if 0 < volume_ratio <= config.pullback_preferred_volume_ratio20:
            score += 8
            positives.append("拉回量縮")
        elif volume_ratio <= 1.0:
            score += 5
        elif volume_ratio > config.pullback_max_volume_ratio20:
            score -= 10
        if prev_low > 0 and _number(row, "low") >= prev_low:
            score += 5
            positives.append("低點未再下移")

    elif strategy == "reversal_reclaim":
        if bottom["rangePosition20"] <= 0.30:
            score += 10
            positives.append("仍在20日底部三成區")
        elif bottom["rangePosition20"] <= config.reversal_max_range_position20:
            score += 5
        if bottom["drawdownFromHigh20Percent"] >= 15.0:
            score += 5
        if prev_low > 0 and _number(row, "low") >= prev_low:
            score += 5
            positives.append("低點止跌")
        if prev_close > 0 and close > prev_close:
            score += 5
        if config.reversal_min_volume_ratio20 <= volume_ratio <= 1.5:
            score += 6
            positives.append("底部量能未失控")

    return _clamp(score), positives, risks


def evaluate_forward_qualification(
    row: Mapping[str, Any],
    strategy: str,
    bullish_score: float,
    quality_score: float,
    config: V12Config,
) -> dict[str, Any]:
    """Decide whether a signal is strong enough for the formal top ten.

    Failing this gate does not delete the signal.  It stays visible as a watch
    candidate so explosive but extended stocks can still be monitored without
    being presented as an already-confirmed entry.
    """
    quality_thresholds = {
        "early_stage": config.forward_min_quality_early,
        "breakout": config.forward_min_quality_breakout,
        "pullback": config.forward_min_quality_pullback,
        "reversal_reclaim": config.forward_min_quality_reversal,
    }
    quality_threshold = quality_thresholds[strategy]
    passed_rules: list[str] = []
    failed_rules: list[str] = []

    if bullish_score >= config.forward_min_bullish_score:
        passed_rules.append("綜合看漲分數達標")
    else:
        failed_rules.append(
            f"綜合看漲分數{bullish_score:.1f}低於"
            f"{config.forward_min_bullish_score:.1f}"
        )
    if quality_score >= quality_threshold:
        passed_rules.append("後勢品質分數達標")
    else:
        failed_rules.append(
            f"後勢品質分數{quality_score:.1f}低於{quality_threshold:.1f}"
        )

    close = _number(row, "close")
    ma5 = _number(row, "ma5")
    ma20 = _number(row, "ma20")
    prev_ma5 = _number(row, "prev_ma5")
    prev_ma20 = _number(row, "prev_ma20")
    change5 = _five_day_change_pct(row)
    close_position = _close_position(row)
    upper_shadow = _upper_shadow_pct(row)

    if strategy in {"early_stage", "breakout", "pullback"}:
        if _slope_not_declining(
            ma20,
            prev_ma20,
            config.forward_ma20_slope_tolerance_pct,
        ):
            passed_rules.append("MA20趨勢未明顯轉弱")
        else:
            failed_rules.append("MA20已明顯下彎")

    if strategy in {"early_stage", "pullback"}:
        if _slope_not_declining(
            ma5,
            prev_ma5,
            config.forward_ma5_slope_tolerance_pct,
        ):
            passed_rules.append("MA5斜率未快速轉弱")
        else:
            failed_rules.append("MA5斜率快速轉弱")

    if strategy in {"early_stage", "breakout"}:
        if change5 <= config.predictive_max_5day_change_pct:
            passed_rules.append("五日漲幅未過度透支")
        else:
            failed_rules.append(
                f"五日漲幅{change5:.1f}%已過度透支，保留觀察但不列正式買點"
            )
        if upper_shadow <= config.predictive_max_upper_shadow_pct:
            passed_rules.append("上影線風險可控")
        else:
            failed_rules.append("上影線過長，突破承接不足")

    if strategy == "breakout":
        if close_position >= config.breakout_min_close_position:
            passed_rules.append("突破日收盤位置健康")
        else:
            failed_rules.append("突破日收盤離日高過遠")

    if strategy == "pullback" and _daily_change_pct(row) < 0:
        down_day_ok = (
            0 < _number(row, "volume_ratio")
            <= config.pullback_down_day_max_volume_ratio20
            and close_position >= config.pullback_down_day_min_close_position
            and _number(row, "prev_low") > 0
            and _number(row, "low") >= _number(row, "prev_low")
        )
        if down_day_ok:
            passed_rules.append("收跌日具備量縮、低點墊高與收盤承接")
        else:
            failed_rules.append("收跌日尚未形成可驗證的止穩結構")

    if (
        strategy == "reversal_reclaim"
        and config.reversal_actionable_requires_ma20_reclaim
    ):
        if close >= ma20 > 0:
            passed_rules.append("反轉訊號已站回MA20")
        else:
            failed_rules.append("反轉訊號尚未站回MA20，只列底部觀察")

    return {
        "qualified": not failed_rules,
        "engine": V12_ACCURACY_ENGINE,
        "minimumBullishScore": config.forward_min_bullish_score,
        "minimumQualityScore": quality_threshold,
        "passedRules": passed_rules,
        "failedRules": failed_rules,
    }


def build_market_context(
    rows: Sequence[Mapping[str, Any]],
    config: V12Config,
) -> dict[str, Any]:
    """Build broad-market and industry breadth from the existing snapshot."""
    usable: list[Mapping[str, Any]] = []
    industries: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        close = _number(row, "close")
        ma20 = _number(row, "ma20")
        if close <= 0 or ma20 <= 0:
            continue
        usable.append(row)
        industry = str(row.get("industry") or "").strip()
        if industry:
            industries.setdefault(industry, []).append(row)

    count = len(usable)
    above_ma20 = sum(
        1 for row in usable if _number(row, "close") >= _number(row, "ma20")
    )
    aligned = sum(
        1 for row in usable if _number(row, "ma5") >= _number(row, "ma20") > 0
    )
    breadth = above_ma20 / count * 100 if count else 0.0
    alignment = aligned / count * 100 if count else 0.0
    if breadth >= config.market_strong_breadth_pct:
        regime = "STRONG"
        regime_label = "市場寬度偏強"
    elif breadth < config.market_weak_breadth_pct:
        regime = "WEAK"
        regime_label = "市場寬度偏弱"
    else:
        regime = "NEUTRAL"
        regime_label = "市場寬度中性"

    industry_context: dict[str, dict[str, Any]] = {}
    for industry, members in industries.items():
        member_count = len(members)
        if member_count < config.sector_minimum_members:
            continue
        sector_above = sum(
            1
            for row in members
            if _number(row, "close") >= _number(row, "ma20")
        )
        changes = sorted(_five_day_change_pct(row) for row in members)
        middle = member_count // 2
        median_change = (
            changes[middle]
            if member_count % 2
            else (changes[middle - 1] + changes[middle]) / 2
        )
        sector_breadth = sector_above / member_count * 100
        industry_context[industry] = {
            "memberCount": member_count,
            "aboveMA20Percent": round(sector_breadth, 2),
            "relativeBreadthPercent": round(sector_breadth - breadth, 2),
            "medianFiveDayChangePercent": round(median_change, 2),
        }

    return {
        "sampleCount": count,
        "aboveMA20Percent": round(breadth, 2),
        "ma5AboveMA20Percent": round(alignment, 2),
        "regime": regime,
        "regimeLabel": regime_label,
        "industries": industry_context,
    }


def apply_market_context(
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    config: V12Config,
) -> dict[str, Any]:
    """Apply a small, transparent breadth adjustment without hiding signals."""
    item = dict(candidate)
    adjustment = 0.0
    reasons: list[str] = []
    warnings = list(item.get("warnings") or [])
    regime = str(context.get("regime") or "NEUTRAL")
    industry = str(item.get("industry") or "").strip()
    industries = context.get("industries")
    sector = (
        industries.get(industry)
        if isinstance(industries, Mapping) and industry
        else None
    )

    if regime == "WEAK":
        adjustment -= config.market_weak_score_penalty
        warnings.append("整體市場寬度偏弱，後勢分數保守扣分")

    if isinstance(sector, Mapping):
        relative = _number(sector, "relativeBreadthPercent")
        median_change = _number(sector, "medianFiveDayChangePercent")
        threshold = config.sector_relative_breadth_threshold_pct
        if relative >= threshold and median_change > 0:
            adjustment += config.sector_strong_score_bonus
            reasons.append("所屬產業相對市場強勢")
        elif relative <= -threshold and median_change < 0:
            adjustment -= config.sector_weak_score_penalty
            warnings.append("所屬產業廣度明顯落後市場")

    qualification = dict(item.get("forwardQualification") or {})
    if (
        regime == "WEAK"
        and str(item.get("strategy") or "") == "breakout"
        and _number(item, "predictive_quality_score")
        < config.market_weak_breakout_min_quality
    ):
        qualification["qualified"] = False
        failed = list(qualification.get("failedRules") or [])
        failed.append("弱勢市場中的突破品質不足，只列觀察")
        qualification["failedRules"] = failed

    bullish_score = _clamp(_number(item, "bullish_score") + adjustment)
    execution_score = _clamp(_number(item, "execution_score") + adjustment)
    ranking_score = _clamp(
        bullish_score * config.ranking_bullish_weight
        + execution_score * config.ranking_execution_weight
    )
    compact_context = {
        "regime": regime,
        "regimeLabel": context.get("regimeLabel"),
        "aboveMA20Percent": context.get("aboveMA20Percent"),
        "ma5AboveMA20Percent": context.get("ma5AboveMA20Percent"),
        "industry": industry or None,
        "industryContext": dict(sector) if isinstance(sector, Mapping) else None,
        "scoreAdjustment": round(adjustment, 2),
    }
    item.update(
        {
            "bullish_score": round(bullish_score, 2),
            "total_score": round(bullish_score, 2),
            "finalScore": round(bullish_score, 2),
            "execution_score": round(execution_score, 2),
            "ranking_score": round(ranking_score, 2),
            "marketContext": compact_context,
            "marketContextReasons": reasons,
            "forwardQualification": qualification,
            "forwardQualified": bool(
                qualification.get(
                    "qualified",
                    item.get("forwardQualified", True),
                )
            ),
            "warnings": warnings,
        }
    )
    return item


def actionability_adjustment(
    action_code: str, maximum_risk_percent: float
) -> tuple[float, list[str]]:
    """Keep directional potential separate from whether it is buyable now."""
    status_adjustment = {
        "BUY_ZONE": 8.0,
        "BUY_ON_BREAKOUT": 5.0,
        "EARLY_ENTRY": 5.0,
        "EARLY_ENTRY_SMALL_POSITION": 3.0,
        "PRICE_CONFIRMATION_REQUIRED": 2.0,
        "SMALL_POSITION_OR_SKIP": -6.0,
        "BOTTOM_REVERSAL_WATCH": -8.0,
        "WAIT_PULLBACK": -10.0,
        "DO_NOT_CHASE": -25.0,
    }.get(action_code, -5.0)
    risk_penalty = max(0.0, maximum_risk_percent - 5.0) * 1.5
    details = [f"操作狀態調整{status_adjustment:+.1f}"]
    if risk_penalty:
        details.append(f"停損距離扣分-{risk_penalty:.1f}")
    return status_adjustment - risk_penalty, details


def apply_execution_prior(
    candidate: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    config: V12Config,
) -> dict[str, Any]:
    """Apply a capped out-of-sample execution prior to one candidate."""
    item = dict(candidate)
    adjustment = _number(prior or {}, "adjustment")
    adjustment = max(
        -config.execution_prior_max_adjustment,
        min(adjustment, config.execution_prior_max_adjustment),
    )
    bullish_score = _clamp(_number(item, "bullish_score") + adjustment)
    execution_score = _clamp(_number(item, "execution_score") + adjustment)
    ranking_score = _clamp(
        bullish_score * config.ranking_bullish_weight
        + execution_score * config.ranking_execution_weight
    )
    qualification = dict(item.get("forwardQualification") or {})
    if (
        bool(qualification.get("qualified", item.get("forwardQualified", True)))
        and bullish_score < config.forward_min_bullish_score
    ):
        qualification["qualified"] = False
        failed = list(qualification.get("failedRules") or [])
        failed.append("同版實際成交績效調整後，看漲分數低於正式門檻")
        qualification["failedRules"] = failed
    item.update(
        {
            "historical_execution_adjustment": round(adjustment, 2),
            "executionPrior": dict(prior or {}),
            "bullish_score": round(bullish_score, 2),
            "total_score": round(bullish_score, 2),
            "finalScore": round(bullish_score, 2),
            "execution_score": round(execution_score, 2),
            "ranking_score": round(ranking_score, 2),
            "forwardQualification": qualification,
            "forwardQualified": bool(
                qualification.get(
                    "qualified",
                    item.get("forwardQualified", True),
                )
            ),
        }
    )
    return item

def strategy_passes(row: Mapping[str, Any], strategy: str, config: V12Config) -> bool:
    close = _number(row, "close")
    ma5 = _number(row, "ma5")
    ma20 = _number(row, "ma20")
    ma60 = _number(row, "ma60")
    bollinger_upper = _number(row, "bollinger_upper")
    volume_ratio = _number(row, "volume_ratio")

    if strategy == "early_stage":
        distance_ma20 = _distance_pct(close, ma20) if ma20 > 0 else 999.0
        return (
            ma5 >= ma20 > 0
            and close >= ma20
            and distance_ma20 <= config.early_max_distance_ma20_pct
            and 0.8 <= volume_ratio <= config.early_max_volume_ratio20
            and _close_position(row) >= config.early_min_close_position
        )
    if strategy == "breakout":
        distance_ma20 = _distance_pct(close, ma20) if ma20 > 0 else 999.0
        prev_high = _number(row, "prev_high")
        return (
            bollinger_upper > 0
            and close >= bollinger_upper
            and (prev_high <= 0 or close >= prev_high)
            and 1.2 <= volume_ratio <= config.breakout_max_volume_ratio20
            and distance_ma20 <= config.breakout_max_distance_ma20_pct
            and _close_position(row) >= config.breakout_min_close_position
        )
    if strategy == "pullback":
        passed, _, _, _, _ = pullback_v2_score(row, config)
        return passed
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

    # A near-limit-up day is still useful as a signal, but it must never be
    # promoted as an immediate early entry.
    if change_pct >= config.strong_day_change_pct:
        adjustment -= 8
        warnings.append("單日漲幅過大，等待回測而非追價")

    if close < open_price and close_position <= 0.25:
        adjustment -= 15
        warnings.append("開高走低並收近低點")

    return adjustment, warnings


def _dual_entry_position_split(
    strategy: str,
    status: str,
) -> tuple[int, int]:
    """Return incremental position sizes for aggressive and confirmed entries."""
    if status == "BOTTOM_REVERSAL_WATCH":
        return 0, 30
    if status == "SMALL_POSITION_OR_SKIP":
        return 20, 30
    if strategy in {"early_stage", "reversal_reclaim"}:
        return 30, 70
    return 40, 60


def _failure_reference_reasons(
    row: Mapping[str, Any],
    signal_defense: float,
    atr14: float,
) -> list[str]:
    """Explain which nearby structures make the fixed failure price meaningful."""
    reasons = ["收盤跌破訊號K低點，代表本次低接／收復結構失效"]
    tolerance = max(atr14 * 0.25, signal_defense * 0.005)
    nearby_references = (
        ("前波壓力轉支撐失敗", _number(row, "prev_high")),
        ("滾動大量低點失守", _number(row, "large_volume_low")),
        ("MA20支撐失守", _number(row, "ma20")),
    )
    for label, value in nearby_references:
        if value > 0 and abs(value - signal_defense) <= tolerance:
            reasons.append(label)
    return reasons


def _build_dual_entry_plan(
    row: Mapping[str, Any],
    strategy: str,
    config: V12Config,
    *,
    status: str,
    signal_defense: float,
    hard_stop: float,
    maximum_buy: float,
    no_chase: float,
    atr14: float,
    breakout_trigger: float,
) -> dict[str, Any]:
    """Build an explicit low-catch + confirmation plan without changing legacy fields."""
    close = _number(row, "close")
    open_price = _number(row, "open") or close
    ma5 = _number(row, "ma5")
    ma20 = _number(row, "ma20")

    aggressive_low = signal_defense
    aggressive_high = min(
        maximum_buy,
        signal_defense + atr14 * config.aggressive_entry_atr_multiple,
    )
    aggressive_low, aggressive_high = sorted((aggressive_low, aggressive_high))

    body_midpoint = (open_price + close) / 2.0
    discounted_ma5 = (
        ma5 - atr14 * config.confirmation_ma5_atr_discount
        if ma5 > 0
        else 0.0
    )
    if strategy == "breakout":
        confirmation_candidates = (breakout_trigger, body_midpoint)
        confirmation_detail = "突破價回測不破或跌破後快速收復，且量能不低於20日均量"
    elif strategy == "reversal_reclaim":
        confirmation_candidates = (body_midpoint, ma5, ma20)
        confirmation_detail = "止跌低點不破，並收盤站回MA20"
    elif strategy == "pullback":
        confirmation_candidates = (body_midpoint, discounted_ma5)
        confirmation_detail = "低點不再下移，並收復MA5附近或訊號K實體中值"
    else:
        confirmation_candidates = (body_midpoint, discounted_ma5, ma20)
        confirmation_detail = "短均線維持上彎，並收復訊號K實體中值"

    confirmation_price = max(
        aggressive_high,
        *(value for value in confirmation_candidates if value > 0),
    )
    aggressive_percent, confirmation_percent = _dual_entry_position_split(
        strategy,
        status,
    )

    rounded_aggressive_low = round_tw_price(aggressive_low)
    rounded_aggressive_high = round_tw_price(aggressive_high)
    rounded_confirmation = round_tw_price(confirmation_price)
    rounded_defense = round_tw_price(signal_defense)
    rounded_hard_stop = round_tw_price(hard_stop)
    rounded_no_chase = round_tw_price(no_chase)
    confirmation_available = rounded_confirmation <= rounded_no_chase

    return {
        "aggressiveEntry": {
            "label": "激進低接點",
            "entryLow": rounded_aggressive_low,
            "entryHigh": rounded_aggressive_high,
            "positionPercent": aggressive_percent,
            "conditions": [
                f"價格進入{rounded_aggressive_low}～{rounded_aggressive_high}",
                f"{rounded_defense}未被收盤有效跌破",
                "至少出現低點不再下移、下影線或外盤回升其中一項承接訊號",
            ],
            "cancelWhen": f"收盤跌破{rounded_defense}",
        },
        "confirmationEntry": {
            "label": "確認買點",
            "price": rounded_confirmation,
            "positionPercent": confirmation_percent,
            "availableBelowNoChase": confirmation_available,
            "conditions": [
                f"站回{rounded_confirmation}且維持在盤中均價之上",
                confirmation_detail,
                f"成交價不得高於不追價線{rounded_no_chase}",
            ],
            "actionWhenUnavailable": (
                None
                if confirmation_available
                else "確認價已高於不追價線，不追價；等待重新回測"
            ),
        },
        "positionPlan": {
            "aggressiveEntryPercent": aggressive_percent,
            "confirmationEntryPercent": confirmation_percent,
            "maximumPlannedPercent": aggressive_percent + confirmation_percent,
            "description": (
                f"激進低接先買{aggressive_percent}%，確認後再買"
                f"{confirmation_percent}%"
            ),
        },
        "failureCondition": {
            "price": rounded_defense,
            "confirmation": "收盤確認",
            "action": "取消尚未成交的分批；已持有部位退出",
            "reasons": _failure_reference_reasons(
                row,
                signal_defense,
                atr14,
            ),
            "legacyEmergencyHardStopPrice": rounded_hard_stop,
        },
    }


def build_trading_plan(
    row: Mapping[str, Any], strategy: str, config: V12Config
) -> dict[str, Any]:
    close = _number(row, "close")
    low = _number(row, "low") or close
    ma5 = _number(row, "ma5")
    ma20 = _number(row, "ma20")
    atr14 = _number(row, "atr14")
    prev_high = _number(row, "prev_high")
    bollinger_upper = _number(row, "bollinger_upper")
    breakout_trigger = 0.0

    signal_defense = low
    stop_buffer = max(
        signal_defense * config.minimum_stop_buffer_pct / 100.0,
        atr14 * config.atr_stop_multiple,
    )
    hard_stop = max(0.01, signal_defense - stop_buffer)

    if strategy == "breakout":
        trigger_candidates = [value for value in (prev_high, bollinger_upper) if value > 0]
        trigger = max(trigger_candidates) if trigger_candidates else close
        breakout_trigger = trigger
        entry_low = max(signal_defense, trigger - atr14 * 0.20)
        entry_high = trigger + atr14 * 0.10
    elif strategy == "reversal_reclaim":
        entry_low = max(signal_defense, close - atr14 * config.entry_low_atr_multiple)
        entry_high = close + atr14 * 0.10
    elif strategy == "pullback":
        reference = ma5 if ma5 > 0 else close
        entry_low = max(signal_defense, reference - atr14 * 0.20)
        entry_high = min(close, reference + atr14 * 0.20)
    else:
        reference = ma20 if ma20 > 0 else close
        entry_low = max(signal_defense, reference - atr14 * 0.20)
        entry_high = min(close, reference + atr14 * 0.20) if close >= reference else close

    # A high signal-day low can put the two calculated entry bounds in reverse
    # order. Normalise them before deriving every downstream price so that the
    # public plan always satisfies low <= high <= maximum buy <= no chase.
    entry_low, entry_high = sorted((entry_low, entry_high))
    maximum_buy = entry_high + atr14 * config.max_buy_atr_multiple
    no_chase = entry_high + atr14 * config.no_chase_atr_multiple
    risk_pct = (entry_high - hard_stop) / entry_high * 100 if entry_high > 0 else 0.0

    # Status codes are public trading instructions, so compare the same
    # tick-rounded prices that the user sees.  Comparing raw floats could
    # publish signalPrice == noChasePrice while still labelling the candidate
    # WAIT_PULLBACK.  A pullback above the ideal range but still below the
    # maximum buy price is not the low-catch BUY_ZONE; it requires price
    # confirmation instead.
    rounded_signal_price = round_tw_price(close)
    rounded_entry_low = round_tw_price(entry_low)
    rounded_entry_high = round_tw_price(entry_high)
    rounded_maximum_buy = round_tw_price(maximum_buy)
    rounded_no_chase = round_tw_price(no_chase)

    # Price tradability takes precedence over strategy/risk labels. Previously
    # pullback candidates could be reported as BUY_ZONE even when the signal
    # price was already above maximumBuyPrice.
    change_pct = _daily_change_pct(row)
    if strategy == "reversal_reclaim" and change_pct >= config.strong_day_change_pct:
        status = "DO_NOT_CHASE"
        initial_position = 0
    elif rounded_signal_price >= rounded_no_chase:
        status = "DO_NOT_CHASE"
        initial_position = 0
    elif rounded_signal_price > rounded_maximum_buy:
        status = "WAIT_PULLBACK"
        initial_position = 0
    elif risk_pct > config.max_entry_to_hard_stop_risk_pct:
        status = "BOTTOM_REVERSAL_WATCH" if strategy == "reversal_reclaim" else "SMALL_POSITION_OR_SKIP"
        initial_position = 0 if strategy == "reversal_reclaim" else 30
    elif strategy == "reversal_reclaim" and close < ma20:
        status = "BOTTOM_REVERSAL_WATCH"
        initial_position = 0
    elif strategy == "reversal_reclaim":
        status = "EARLY_ENTRY_SMALL_POSITION"
        initial_position = 30
    elif strategy == "breakout":
        status = "BUY_ON_BREAKOUT"
        initial_position = 40
    elif strategy == "pullback":
        if rounded_entry_low <= rounded_signal_price <= rounded_entry_high:
            status = "BUY_ZONE"
            initial_position = 40
        else:
            status = "PRICE_CONFIRMATION_REQUIRED"
            initial_position = 0
    else:
        status = "PRICE_CONFIRMATION_REQUIRED"
        initial_position = 30

    dual_entry_plan = _build_dual_entry_plan(
        row,
        strategy,
        config,
        status=status,
        signal_defense=signal_defense,
        hard_stop=hard_stop,
        maximum_buy=maximum_buy,
        no_chase=no_chase,
        atr14=atr14,
        breakout_trigger=breakout_trigger,
    )

    return {
        "status": v12_status_label(status),
        "statusCode": status,
        "signalDate": str(row.get("trade_date") or ""),
        "signalPrice": rounded_signal_price,
        "idealEntryLow": rounded_entry_low,
        "idealEntryHigh": rounded_entry_high,
        "maximumBuyPrice": rounded_maximum_buy,
        "noChasePrice": rounded_no_chase,
        "signalDefensePrice": round_tw_price(signal_defense),
        "hardStopPrice": round_tw_price(hard_stop),
        "atr14": round_tw_price(atr14),
        "initialPositionPercent": initial_position,
        "maximumRiskPercent": round(risk_pct, 2),
        "softBreakAction": f"減碼{int(config.soft_break_reduce_ratio * 100)}%，進入洗盤觀察",
        "hardStopAction": "收盤確認跌破後退出剩餘部位",
        "reclaimAction": "快速收復固定防守與前一日黑K中值時分批買回",
        **dual_entry_plan,
    }


def build_v12_candidate(
    row: Mapping[str, Any], strategy: str, config: V12Config
) -> dict[str, Any] | None:
    liquidity = liquidity_result(row, config)
    if not liquidity["eligible"] or not strategy_passes(row, strategy, config):
        return None

    if strategy == "reversal_reclaim":
        _, raw_score, reasons, pattern_warnings = reversal_reclaim_score(row, config)
    elif strategy == "pullback":
        _, raw_score, reasons, pattern_warnings, stabilization_signals = pullback_v2_score(
            row, config
        )
        reasons = reasons + [f"止穩：{signal}" for signal in stabilization_signals]
    else:
        raw_score = _v11_base_score(row)
        reasons = _strategy_reasons(row, strategy)
        pattern_warnings = []

    quality_score, quality_reasons, quality_warnings = predictive_quality_score(
        row, strategy, config
    )
    adjustment, trading_warnings = trading_adjustment(row, config)
    bullish_score = _clamp(
        raw_score * config.bullish_raw_score_weight
        + quality_score * config.bullish_quality_score_weight
        + adjustment
    )
    forward_qualification = evaluate_forward_qualification(
        row,
        strategy,
        bullish_score,
        quality_score,
        config,
    )
    plan = build_trading_plan(row, strategy, config)

    # A heavily penalised setup is observable but should not be presented as a buy.
    action_code = str(plan["statusCode"])
    if adjustment <= -25:
        action_code = "DO_NOT_CHASE"
    elif adjustment <= -10 and action_code not in {
        "DO_NOT_CHASE",
        "WAIT_PULLBACK",
        "SMALL_POSITION_OR_SKIP",
        "EARLY_ENTRY_SMALL_POSITION",
        "BOTTOM_REVERSAL_WATCH",
    }:
        action_code = "WAIT_PULLBACK"

    if action_code != plan["statusCode"]:
        plan = dict(plan)
        plan["statusCode"] = action_code
        plan["status"] = v12_status_label(action_code)
        if action_code in {"DO_NOT_CHASE", "WAIT_PULLBACK"}:
            plan["initialPositionPercent"] = 0

    execution_adjustment, execution_reasons = actionability_adjustment(
        action_code,
        _number(plan, "maximumRiskPercent"),
    )
    execution_score = _clamp(bullish_score + execution_adjustment)
    ranking_score = _clamp(
        bullish_score * config.ranking_bullish_weight
        + execution_score * config.ranking_execution_weight
    )

    item = dict(row)
    # The database column historically stores the official daily price change
    # amount even though it is named change_percent. Keep that value under an
    # accurate compatibility field and expose a real percentage in V12.
    change_amount = item.get("change_percent")
    daily_change_percent = round(_daily_change_pct(row), 4)
    item.update(
        {
            "change_amount": change_amount,
            "change_percent": daily_change_percent,
            "strategy": strategy,
            "strategies": [strategy],
            "accuracyEngine": V12_ACCURACY_ENGINE,
            "raw_score": round(raw_score, 2),
            "predictive_quality_score": round(quality_score, 2),
            "trading_adjustment": round(adjustment, 2),
            "bullish_score": round(bullish_score, 2),
            "execution_score": round(execution_score, 2),
            "ranking_score": round(ranking_score, 2),
            "historical_execution_adjustment": 0.0,
            "total_score": round(bullish_score, 2),
            "finalScore": round(bullish_score, 2),
            "action": v12_status_label(action_code),
            "actionCode": action_code,
            "forwardQualified": bool(forward_qualification["qualified"]),
            "forwardQualification": forward_qualification,
            "reasons": reasons + [
                f"後勢品質：{reason}" for reason in quality_reasons
            ],
            "warnings": (
                pattern_warnings + quality_warnings + trading_warnings
                + (
                    []
                    if forward_qualification["qualified"]
                    else ["尚未通過V12.2後勢持續性門檻，保留觀察但不列正式前十"]
                )
            ),
            "executionScoreDetails": execution_reasons,
            "liquidity": liquidity,
            "tradingPlan": plan,
            # These are deliberately frozen in the signal snapshot.
            "signal_defense_price": plan["signalDefensePrice"],
            "hard_stop_price": plan["hardStopPrice"],
            # This remains rolling for reference only, never for retroactive stop changes.
            "rolling_massive_volume_low": _number(row, "large_volume_low") or None,
            "dailyChangePercent": round(daily_change_percent, 2),
            "closePosition": round(_close_position(row), 4),
            "upperShadowPercent": round(_upper_shadow_pct(row), 2),
            "lowerShadowPercent": round(_lower_shadow_pct(row), 2),
        }
    )
    if strategy == "reversal_reclaim":
        bottom = _bottom_context(row)
        item["bottomContext"] = {
            key: round(value, 4) if isinstance(value, float) else value
            for key, value in bottom.items()
        }
    return item


def split_v12_price_tiers(
    candidates: Sequence[Mapping[str, Any]],
    config: V12Config,
) -> dict[str, list[dict[str, Any]]]:
    """Split radar output into the <=NT$200 main board and strict exceptions."""
    main: list[dict[str, Any]] = []
    high_price: list[dict[str, Any]] = []
    rejected_high_price: list[dict[str, Any]] = []

    for candidate in candidates:
        item = dict(candidate)
        close = _number(item, "close")
        if close <= config.primary_max_price:
            item.update(
                {
                    "priceTier": "MAIN_UNDER_200",
                    "priceTierLabel": f"{config.primary_max_price:.0f}元以下主榜",
                    "priceRuleFailures": [],
                }
            )
            main.append(item)
            continue

        score = _number(item, "total_score")
        plan = item.get("tradingPlan")
        plan = plan if isinstance(plan, Mapping) else {}
        action_code = str(
            item.get("actionCode")
            or plan.get("statusCode")
            or ""
        )
        maximum_risk = _number(plan, "maximumRiskPercent", 999.0)
        warnings = [str(value) for value in item.get("warnings") or [] if value]
        liquidity = item.get("liquidity")
        liquidity_eligible = (
            isinstance(liquidity, Mapping)
            and bool(liquidity.get("eligible"))
        )
        failures: list[str] = []

        if score < config.high_price_min_score:
            failures.append(
                f"總分{score:.1f}低於高價股門檻{config.high_price_min_score:.0f}"
            )
        if action_code not in V12_HIGH_PRICE_ELIGIBLE_STATUS_CODES:
            failures.append("目前操作狀態不適合列入高價強勢股")
        if not bool(item.get("forwardQualified", True)):
            failures.append("尚未通過後勢持續性門檻")
        if maximum_risk > config.high_price_max_risk_pct:
            failures.append(
                f"最大風險{maximum_risk:.1f}%高於"
                f"{config.high_price_max_risk_pct:.1f}%"
            )
        if warnings:
            failures.append("仍有過熱、乖離或走弱警示")
        if not liquidity_eligible:
            failures.append("未通過流動性硬門檻")

        if failures:
            item.update(
                {
                    "priceTier": "HIGH_PRICE_REJECTED",
                    "priceTierLabel": (
                        f"{config.primary_max_price:.0f}元以上未達例外標準"
                    ),
                    "priceRuleFailures": failures,
                }
            )
            rejected_high_price.append(item)
            continue

        item.update(
            {
                "priceTier": "HIGH_PRICE_EXCEPTION",
                "priceTierLabel": (
                    f"{config.primary_max_price:.0f}元以上強勢例外"
                ),
                "priceRuleFailures": [],
                "highPriceQualification": [
                    f"總分{score:.1f}",
                    "操作狀態可執行",
                    "後勢持續性門檻已通過",
                    f"最大風險{maximum_risk:.1f}%",
                    "無過熱、乖離或走弱警示",
                ],
            }
        )
        high_price.append(item)

    return {
        "main": main,
        "highPrice": high_price,
        "rejectedHighPrice": rejected_high_price,
    }


def validate_v12_candidates(
    candidates: Sequence[Mapping[str, Any]], context: str = "results"
) -> list[dict[str, Any]]:
    """Return semantic output problems that functional release checks can miss."""
    issues: list[dict[str, Any]] = []
    tradable_statuses = {
        "BUY_ZONE",
        "BUY_ON_BREAKOUT",
        "EARLY_ENTRY",
        "EARLY_ENTRY_SMALL_POSITION",
        "PRICE_CONFIRMATION_REQUIRED",
        "SMALL_POSITION_OR_SKIP",
    }

    for index, candidate in enumerate(candidates):
        symbol = str(candidate.get("symbol") or "UNKNOWN")
        path = f"{context}[{index}]"
        plan = candidate.get("tradingPlan")
        if not isinstance(plan, Mapping):
            issues.append({"code": "MISSING_TRADING_PLAN", "symbol": symbol, "path": path})
            continue

        close = _number(candidate, "close")
        previous_close = _number(candidate, "prev_close")
        actual_change_percent = _number(candidate, "change_percent")
        expected_change_percent = (
            (close / previous_close - 1) * 100 if previous_close > 0 else 0.0
        )
        if previous_close > 0 and abs(actual_change_percent - expected_change_percent) > 0.02:
            issues.append(
                {
                    "code": "CHANGE_PERCENT_MISMATCH",
                    "symbol": symbol,
                    "path": path,
                    "expected": round(expected_change_percent, 4),
                    "actual": round(actual_change_percent, 4),
                }
            )

        entry_low = _number(plan, "idealEntryLow")
        entry_high = _number(plan, "idealEntryHigh")
        maximum_buy = _number(plan, "maximumBuyPrice")
        no_chase = _number(plan, "noChasePrice")
        signal_price = _number(plan, "signalPrice")
        status = str(plan.get("statusCode") or plan.get("status") or "")

        if not (entry_low <= entry_high <= maximum_buy <= no_chase):
            issues.append(
                {
                    "code": "INVALID_PRICE_BAND_ORDER",
                    "symbol": symbol,
                    "path": path,
                    "idealEntryLow": entry_low,
                    "idealEntryHigh": entry_high,
                    "maximumBuyPrice": maximum_buy,
                    "noChasePrice": no_chase,
                }
            )

        if status == "BUY_ZONE" and not (entry_low <= signal_price <= entry_high):
            issues.append(
                {
                    "code": "BUY_ZONE_OUTSIDE_ENTRY_RANGE",
                    "symbol": symbol,
                    "path": path,
                    "signalPrice": signal_price,
                }
            )
        if status in tradable_statuses and signal_price > maximum_buy:
            issues.append(
                {
                    "code": "TRADABLE_STATUS_ABOVE_MAXIMUM_BUY",
                    "symbol": symbol,
                    "path": path,
                    "status": status,
                    "signalPrice": signal_price,
                    "maximumBuyPrice": maximum_buy,
                }
            )
        if signal_price >= no_chase and status != "DO_NOT_CHASE":
            issues.append(
                {
                    "code": "NO_CHASE_STATUS_MISSING",
                    "symbol": symbol,
                    "path": path,
                    "status": status,
                    "signalPrice": signal_price,
                    "noChasePrice": no_chase,
                }
            )

        aggressive = plan.get("aggressiveEntry")
        confirmation = plan.get("confirmationEntry")
        position_plan = plan.get("positionPlan")
        failure = plan.get("failureCondition")
        if not all(
            isinstance(value, Mapping)
            for value in (aggressive, confirmation, position_plan, failure)
        ):
            issues.append(
                {
                    "code": "MISSING_DUAL_ENTRY_PLAN",
                    "symbol": symbol,
                    "path": path,
                }
            )
            continue

        aggressive_low = _number(aggressive, "entryLow")
        aggressive_high = _number(aggressive, "entryHigh")
        if not (
            0 < aggressive_low
            <= aggressive_high
            <= maximum_buy
            <= no_chase
        ):
            issues.append(
                {
                    "code": "INVALID_AGGRESSIVE_ENTRY_RANGE",
                    "symbol": symbol,
                    "path": path,
                    "entryLow": aggressive_low,
                    "entryHigh": aggressive_high,
                    "maximumBuyPrice": maximum_buy,
                    "noChasePrice": no_chase,
                }
            )

        aggressive_percent = int(_number(position_plan, "aggressiveEntryPercent"))
        confirmation_percent = int(_number(position_plan, "confirmationEntryPercent"))
        maximum_planned = int(_number(position_plan, "maximumPlannedPercent"))
        if not (
            0 <= aggressive_percent <= 100
            and 0 <= confirmation_percent <= 100
            and aggressive_percent + confirmation_percent == maximum_planned
            and maximum_planned <= 100
        ):
            issues.append(
                {
                    "code": "INVALID_DUAL_ENTRY_POSITION_SPLIT",
                    "symbol": symbol,
                    "path": path,
                    "aggressiveEntryPercent": aggressive_percent,
                    "confirmationEntryPercent": confirmation_percent,
                    "maximumPlannedPercent": maximum_planned,
                }
            )

        failure_price = _number(failure, "price")
        signal_defense = _number(plan, "signalDefensePrice")
        if failure_price != signal_defense:
            issues.append(
                {
                    "code": "FAILURE_PRICE_MISMATCH",
                    "symbol": symbol,
                    "path": path,
                    "failurePrice": failure_price,
                    "signalDefensePrice": signal_defense,
                }
            )

        confirmation_price = _number(confirmation, "price")
        confirmation_available = bool(
            confirmation.get("availableBelowNoChase")
        )
        if confirmation_available != (confirmation_price <= no_chase):
            issues.append(
                {
                    "code": "CONFIRMATION_AVAILABILITY_MISMATCH",
                    "symbol": symbol,
                    "path": path,
                    "confirmationPrice": confirmation_price,
                    "noChasePrice": no_chase,
                }
            )

    return issues


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
            float(item.get("ranking_score") or item.get("total_score") or 0),
            float(item.get("bullish_score") or item.get("total_score") or 0),
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
