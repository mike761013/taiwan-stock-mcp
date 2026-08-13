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
    if change_pct < 0:
        score += 15
        reasons.append("收跌但MA5與大量低點支撐未破")
        if volume_ratio <= 1.0:
            score += 5
            reasons.append("收跌量縮，屬健康整理")
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

    core_pass = (
        trend_ok
        and near_ma5
        and massive_low_hold
        and 0 < volume_ratio <= config.pullback_max_volume_ratio20
    )
    return core_pass, _clamp(score), reasons, warnings, signals

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
    confirmation_available = confirmation_price <= no_chase
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

    # Price tradability takes precedence over strategy/risk labels. Previously
    # pullback candidates could be reported as BUY_ZONE even when the signal
    # price was already above maximumBuyPrice.
    change_pct = _daily_change_pct(row)
    if strategy == "reversal_reclaim" and change_pct >= config.strong_day_change_pct:
        status = "DO_NOT_CHASE"
        initial_position = 0
    elif close >= no_chase:
        status = "DO_NOT_CHASE"
        initial_position = 0
    elif close > maximum_buy:
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
        status = "BUY_ZONE"
        initial_position = 40
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

    adjustment, trading_warnings = trading_adjustment(row, config)
    final_score = _clamp(raw_score + adjustment)
    plan = build_trading_plan(row, strategy, config)

    # A heavily penalised setup is observable but should not be presented as a buy.
    action_code = str(plan["statusCode"])
    if adjustment <= -25:
        action_code = "DO_NOT_CHASE"
    elif adjustment <= -10 and action_code not in {
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
            "raw_score": round(raw_score, 2),
            "trading_adjustment": round(adjustment, 2),
            "total_score": round(final_score, 2),
            "finalScore": round(final_score, 2),
            "action": v12_status_label(action_code),
            "actionCode": action_code,
            "reasons": reasons,
            "warnings": pattern_warnings + trading_warnings,
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
