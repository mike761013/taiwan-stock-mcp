from decimal import Decimal

from stock_db.portfolio import calculate_net_break_even_price


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
