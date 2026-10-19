from copy import deepcopy
from decimal import Decimal

import pytest

from stock_db.portfolio import (
    PortfolioLedgerError, allocate_sale, calculate_buy_values,
    calculate_realized_profit_zero_price, plan_cash_lot_corrections,
)


def fixture():
    def buy(i, day, price):
        b = calculate_buy_values(1000, price)
        return dict(id=i, side="BUY", symbol="TEST", trade_date=day,
                    quantity=1000, price=Decimal(str(price)), commission=b["commission"],
                    account_type="CASH", asset_type="STOCK", lot_type="REGULAR")

    first, second, new = buy(1, "2026-01-01", 40), buy(2, "2026-01-02", 55), buy(12, "2026-01-04", 62)
    trades = [first, second, new]
    allocations = []
    for sid, day, price, pairs in [(9, "2026-01-02", 45, [(first, 600)]),
                                    (10, "2026-01-03", 60, [(first, 400), (second, 600)]),
                                    (11, "2026-01-04", 65, [(second, 100)])]:
        sale_allocations = []
        for b, qty in pairs:
            a = allocate_sale([{**b, "remaining_quantity": qty}], quantity=qty,
                              sell_price=price, sell_date=day, tax_treatment="NORMAL")[0]
            sale_allocations.append(dict(
                sell_transaction_id=sid, buy_transaction_id=b["id"], quantity=qty,
                buy_cost=a["buyCost"], sell_proceeds=a["sellProceeds"],
                sell_commission=a["sellCommission"], transaction_tax=a["transactionTax"],
                tax_rate=a["taxRate"], margin_interest=a["marginInterest"],
                realized_pnl=a["realizedPnl"], matching_rule="FIFO"))
        totals = {k: sum((a[k] for a in sale_allocations), Decimal(0))
                  for k in ["sell_commission", "transaction_tax", "sell_proceeds"]}
        trades.append(dict(id=sid, side="SELL", symbol="TEST", trade_date=day,
                           quantity=sum(q for _, q in pairs), price=Decimal(price),
                           account_type="CASH", asset_type="STOCK", lot_type="REGULAR",
                           commission=totals["sell_commission"], transaction_tax=totals["transaction_tax"],
                           net_cash_flow=totals["sell_proceeds"]))
        allocations += sale_allocations
    corrections = [dict(sellTransactionId=10,
                        expectedAllocations=[dict(buyTransactionId=1, quantity=400), dict(buyTransactionId=2, quantity=600)],
                        allocations=[dict(buyTransactionId=2, quantity=1000)]),
                   dict(sellTransactionId=11,
                        expectedAllocations=[dict(buyTransactionId=2, quantity=100)],
                        allocations=[dict(buyTransactionId=1, quantity=100)])]
    return trades, allocations, corrections


def test_atomic_partial_cross_date_correction_preserves_cash_and_zero_price():
    trades, original, corrections = fixture()
    snapshot = deepcopy((trades, original, corrections))
    plans = plan_cash_lot_corrections(trades, original, corrections)
    assert (trades, original, corrections) == snapshot
    corrected = [a for a in original if a["sell_transaction_id"] == 9]
    corrected += [a for p in plans for a in p["correctedAllocations"]]
    for plan in plans:
        for k in ["quantity", "sell_proceeds", "sell_commission", "transaction_tax", "margin_interest"]:
            assert sum(a[k] for a in plan["originalAllocations"]) == sum(a[k] for a in plan["correctedAllocations"])
    used = {i: sum(a["quantity"] for a in corrected if a["buy_transaction_id"] == i) for i in [1, 2, 12]}
    assert used == {1: 700, 2: 1000, 12: 0}
    def zero(rows):
        costs = sum((t["price"] * t["quantity"] + t["commission"] for t in trades if t["side"] == "BUY"), Decimal(0))
        remaining = costs - sum(a["buy_cost"] for a in rows)
        realized = sum(a["realized_pnl"] for a in rows)
        return calculate_realized_profit_zero_price(1300, remaining, realized)
    assert zero(corrected) == zero(original)
    assert sum(a["realized_pnl"] for a in original) - sum(a["realized_pnl"] for a in corrected) == Decimal("4501.7955")
    replay = plan_cash_lot_corrections(trades, corrected, corrections)
    assert all(p["alreadyApplied"] for p in replay)


def test_partial_correction_cannot_overdraw_a_lot_between_sales():
    trades, allocations, corrections = fixture()
    with pytest.raises(PortfolioLedgerError, match="overdraw"):
        plan_cash_lot_corrections(trades, allocations, corrections[:1])


@pytest.mark.parametrize("bad", ["stale", "future", "scope", "quantity", "duplicate", "tax", "cash", "margin"])
def test_invalid_corrections_are_rejected_without_mutating_inputs(bad):
    trades, allocations, corrections = fixture()
    if bad == "stale":
        corrections[0]["expectedAllocations"][0]["quantity"] = 399
    elif bad == "future":
        corrections[1]["allocations"][0]["buyTransactionId"] = 12
    elif bad == "scope":
        trades[0]["lot_type"] = "RECURRING"
    elif bad == "quantity":
        corrections[1]["allocations"][0]["quantity"] = 101
    elif bad == "duplicate":
        corrections.append(deepcopy(corrections[0]))
    elif bad == "tax":
        allocations[-1]["tax_rate"] = Decimal(".0015")
    elif bad == "cash":
        trades[-1]["net_cash_flow"] += 1
    elif bad == "margin":
        allocations[-1]["margin_interest"] = 1
    snapshot = deepcopy((trades, allocations, corrections))
    with pytest.raises(PortfolioLedgerError):
        plan_cash_lot_corrections(trades, allocations, corrections)
    assert (trades, allocations, corrections) == snapshot
