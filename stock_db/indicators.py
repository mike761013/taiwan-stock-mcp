"""Pure-Python daily technical indicator calculation."""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Sequence


def _avg(values: Sequence[float]) -> float | None:
    return mean(values) if values else None


def _technical_score(
    close: float, ma5: float | None, ma20: float | None, ma60: float | None,
    volume_ratio: float | None, bollinger_upper: float | None
) -> float:
    score = 0.0
    if ma5 is not None and close > ma5:
        score += 18
    if ma20 is not None and close > ma20:
        score += 22
    if ma60 is not None and close > ma60:
        score += 20
    if ma5 is not None and ma20 is not None and ma5 > ma20:
        score += 15
    if volume_ratio is not None:
        score += min(max((volume_ratio - 1) * 15, 0), 15)
    if bollinger_upper and close >= bollinger_upper * 0.98:
        score += 10
    return round(min(score, 100), 4)


def calculate_indicators(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["trade_date"])
    closes: list[float] = []
    volumes: list[float] = []
    lows: list[float] = []
    output: list[dict[str, Any]] = []

    for row in ordered:
        close = float(row["close"]) if row.get("close") is not None else math.nan
        volume = float(row.get("volume") or 0)
        low = float(row["low"]) if row.get("low") is not None else close
        closes.append(close)
        volumes.append(volume)
        lows.append(low)

        def window_avg(values: list[float], size: int) -> float | None:
            return _avg(values[-size:]) if len(values) >= size else None

        ma5 = window_avg(closes, 5)
        ma10 = window_avg(closes, 10)
        ma20 = window_avg(closes, 20)
        ma60 = window_avg(closes, 60)
        vma5 = window_avg(volumes, 5)
        vma20 = window_avg(volumes, 20)
        std20 = pstdev(closes[-20:]) if len(closes) >= 20 else None
        upper = ma20 + 2 * std20 if ma20 is not None and std20 is not None else None
        lower = ma20 - 2 * std20 if ma20 is not None and std20 is not None else None
        volume_ratio = volume / vma20 if vma20 and vma20 > 0 else None
        returns = []
        if len(closes) >= 21:
            for left, right in zip(closes[-21:-1], closes[-20:]):
                if left:
                    returns.append((right / left) - 1)
        volatility = pstdev(returns) if len(returns) >= 20 else None

        large_low = None
        if len(volumes) >= 20:
            recent_volumes = volumes[-20:]
            max_index = max(range(len(recent_volumes)), key=recent_volumes.__getitem__)
            large_low = lows[-20:][max_index]

        output.append({
            "symbol": row["symbol"],
            "trade_date": row["trade_date"],
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
            "volume_ma5": vma5, "volume_ma20": vma20,
            "bollinger_mid": ma20, "bollinger_upper": upper,
            "bollinger_lower": lower, "volume_ratio": volume_ratio,
            "volatility_20": volatility, "large_volume_low": large_low,
            "technical_score": _technical_score(
                close, ma5, ma20, ma60, volume_ratio, upper
            ),
        })
    return output
