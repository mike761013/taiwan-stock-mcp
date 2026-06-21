from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

BROKER_FEE_RATE = 0.000399  # user configured: 0.1425% * 28%
DAY_TRADE_TAX_RATE = 0.0015
NORMAL_SELL_TAX_RATE = 0.003


def build_order_preview(
    symbol: str,
    side: str,
    entry_price: float,
    stop_price: float,
    budget: float = 50000,
    max_risk: float = 500,
    day_trade: bool = True,
    odd_lot: bool = True,
) -> dict[str, Any]:
    symbol = str(symbol).strip().upper()
    side = str(side).strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side 僅支援 buy 或 sell。")
    entry_price = float(entry_price)
    stop_price = float(stop_price)
    budget = float(budget)
    max_risk = float(max_risk)
    if entry_price <= 0 or stop_price <= 0 or budget <= 0 or max_risk <= 0:
        raise ValueError("entry_price、stop_price、budget、max_risk 都必須大於 0。")

    per_share_price_risk = abs(entry_price - stop_price)
    if per_share_price_risk <= 0:
        raise ValueError("停損價不能等於進場價。")

    max_shares_by_budget = int(budget // entry_price)
    max_shares_by_risk = int(max_risk // per_share_price_risk)
    shares = max(0, min(max_shares_by_budget, max_shares_by_risk))
    if not odd_lot:
        shares = (shares // 1000) * 1000

    gross_amount = entry_price * shares
    buy_fee = gross_amount * BROKER_FEE_RATE
    sell_fee_est = gross_amount * BROKER_FEE_RATE
    tax_rate = DAY_TRADE_TAX_RATE if day_trade else NORMAL_SELL_TAX_RATE
    sell_tax_est = gross_amount * tax_rate
    total_round_trip_cost_est = buy_fee + sell_fee_est + sell_tax_est
    price_risk = per_share_price_risk * shares
    total_risk_est = price_risk + total_round_trip_cost_est

    warnings = []
    if shares <= 0:
        warnings.append("依目前預算與風險上限，建議股數為 0，代表停損距離太大或預算不足。")
    if total_risk_est > max_risk * 1.2:
        warnings.append("加入交易成本後，總風險可能明顯高於設定的 max_risk。")
    if shares < 1000:
        warnings.append("股數低於 1 張，屬零股預覽；實際成交流動性與價格可能不同。")

    return {
        "symbol": symbol,
        "side": side,
        "entryPrice": round(entry_price, 4),
        "stopPrice": round(stop_price, 4),
        "budget": round(budget, 2),
        "maxRisk": round(max_risk, 2),
        "dayTrade": bool(day_trade),
        "shares": shares,
        "lots": round(shares / 1000, 3),
        "grossAmount": round(gross_amount, 2),
        "estimatedBuyFee": round(buy_fee, 2),
        "estimatedSellFee": round(sell_fee_est, 2),
        "estimatedSellTax": round(sell_tax_est, 2),
        "estimatedRoundTripCost": round(total_round_trip_cost_est, 2),
        "priceRiskOnly": round(price_risk, 2),
        "estimatedTotalRisk": round(total_risk_est, 2),
        "perSharePriceRisk": round(per_share_price_risk, 4),
        "warnings": warnings,
        "note": "這是下單預覽，不會送出委託。實際下單仍請在券商 App 人工確認。",
    }
