from decimal import Decimal

from stock_db.portfolio import calculate_net_break_even_price
from stock_db.portfolio import calculate_realized_profit_zero_price, current_position_cycles


def test_remaining_inventory_exit_at_break_even_has_zero_net_pnl():
    cost = Decimal("67166.7889")
    price = calculate_net_break_even_price(2200, cost)
    assert price is not None
    assert abs(price * 2200 * Decimal("0.996601") - cost) < Decimal("1e-20")
    assert round(price, 2) == Decimal("30.63")


def test_margin_interest_is_recovered_at_break_even():
    cost, interest = Decimal("100039.9"), Decimal("645")
    cash = calculate_net_break_even_price(1000, cost)
    margin = calculate_net_break_even_price(1000, cost, interest, account_type="MARGIN")
    assert margin > cash
    assert abs(margin * 1000 * Decimal("0.996601") - cost - interest) < Decimal("1e-20")


def test_etf_uses_its_own_sell_tax_and_stock_never_assumes_day_trade():
    cost = Decimal("100039.9")
    etf = calculate_net_break_even_price(1000, cost, asset_type="ETF")
    stock = calculate_net_break_even_price(1000, cost)
    assert etf < stock
    assert abs(etf * 1000 * Decimal("0.998601") - cost) < Decimal("1e-20")


def test_closed_and_short_positions_do_not_get_a_long_break_even_price():
    assert calculate_net_break_even_price(0, Decimal("0")) is None
    assert calculate_net_break_even_price(1000, Decimal("100000"), account_type="SHORT") is None


def trade(id, side, quantity, pnl=0, account="CASH"):
    return dict(id=id, symbol="3094", account_type=account, asset_type="STOCK",
                lot_type="REGULAR", trade_date=f"2026-09-{id:02d}",
                side=side, quantity=quantity, realized_pnl=pnl)


def test_realized_profit_is_fully_consumed_at_3094_threshold():
    cost, realized = Decimal("21568.6024"), Decimal("4561.1974")
    p = calculate_realized_profit_zero_price(400, cost, realized)
    assert round(p, 2) == Decimal("42.66")
    assert abs(p * 400 * Decimal("0.996601") - cost + realized) < Decimal("1e-20")


def test_old_closed_cycle_profit_is_not_credited_to_reentry():
    cycles = current_position_cycles([
        trade(1, "BUY", 1000), trade(2, "SELL", 1000, 9000),
        trade(3, "BUY", 1000), trade(4, "SELL", 600, "4561.1974"),
    ])
    c = cycles[("3094", "CASH", "STOCK", "REGULAR")]
    assert c["quantity"] == 400
    assert c["soldQuantity"] == 600
    assert c["realizedNetPnl"] == Decimal("4561.1974")


def test_fully_sold_fifo_lots_in_same_cycle_still_contribute_profit():
    cycles = current_position_cycles([
        trade(1, "BUY", 1000), trade(2, "BUY", 1000),
        trade(3, "SELL", 1000, 2000), trade(4, "SELL", 600, 3000),
        trade(5, "BUY", 1000, account="MARGIN"),
        trade(6, "SELL", 200, 100, account="MARGIN"),
    ])
    assert cycles[("3094", "CASH", "STOCK", "REGULAR")]["realizedNetPnl"] == 5000
    assert cycles[("3094", "MARGIN", "STOCK", "REGULAR")]["realizedNetPnl"] == 100


def test_no_booked_pnl_uses_normal_break_even_and_protected_profit_is_explicit():
    assert calculate_realized_profit_zero_price(400, Decimal("20000"), Decimal("0")) == calculate_net_break_even_price(400, Decimal("20000"))
    assert calculate_realized_profit_zero_price(400, Decimal("20000"), Decimal("21000")) is None
    assert calculate_realized_profit_zero_price(400, Decimal("20000"), Decimal("20000")) == 0


def test_realized_loss_raises_zero_price_and_is_recovered_after_costs():
    cost, loss = Decimal("20000"), Decimal("-1000")
    zero = calculate_realized_profit_zero_price(400, cost, loss)
    assert zero > calculate_net_break_even_price(400, cost)
    assert abs(zero * 400 * Decimal("0.996601") - cost + loss) < Decimal("1e-20")
