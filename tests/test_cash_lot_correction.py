from decimal import Decimal
import pytest
from stock_db.portfolio import corrected_cash_lot_allocation, PortfolioLedgerError, calculate_realized_profit_zero_price


def test_broker_lot_match_preserves_cash_and_reduces_booked_gain():
    a = dict(quantity=200, buy_transaction_id=6, buy_cost=Decimal('5942.3701'),
             sell_proceeds=Decimal('7972.808'), realized_pnl=Decimal('2030.4379'),
             margin_interest=0, tax_rate=Decimal('.003'))
    b = dict(id=11, account_type='CASH', quantity=2000, price=Decimal('30.6'), commission=Decimal('24.4188'))
    c = corrected_cash_lot_allocation(a,b)
    assert c['buy_cost'] == Decimal('6122.4419')
    assert c['realized_pnl'] == Decimal('1850.3661')
    assert c['sell_proceeds'] == a['sell_proceeds']
    assert c['tax_rate'] == a['tax_rate']
    assert a['buy_transaction_id'] == 6
    # Across the full holding cycle, allocation correction cannot change total P/L zero price.
    old = calculate_realized_profit_zero_price(2000,Decimal('61224.4188'),Decimal('16004.0462'))
    new = calculate_realized_profit_zero_price(2000,Decimal('59423.7006'),Decimal('14203.3280'))
    assert old == new


def test_margin_correction_is_rejected():
    with pytest.raises(PortfolioLedgerError):
        corrected_cash_lot_allocation(dict(margin_interest=0),dict(account_type='MARGIN'))
