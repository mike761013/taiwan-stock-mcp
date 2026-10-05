from decimal import Decimal
from stock_db.portfolio import allocate_sale, current_position_cycles, calculate_buy_values

def lot(i, day, price, q):
    return dict(transaction_id=i, trade_date=day, price=price, quantity=q,
                remaining_quantity=q, commission=calculate_buy_values(q,price)['commission'])

def test_normal_sale_consumes_old_inventory_even_with_same_day_buy():
    a=allocate_sale([lot(51,'2026-09-24',53.9,350),lot(80,'2026-10-05',69.9,1000)],
                    quantity=350,sell_price=69.7,sell_date='2026-10-05',tax_treatment='NORMAL')
    assert len(a)==1 and a[0]['buyTransactionId']==51
    assert a[0]['taxRate']==Decimal('.003') and a[0]['matchingRule']=='FIFO'

def trade(i,day,side,q,pnl=0):
    return dict(id=i,trade_date=day,side=side,quantity=q,realized_pnl=pnl,
                symbol='3094',account_type='CASH',asset_type='STOCK',lot_type='REGULAR')

def test_same_session_inventory_replacement_keeps_realized_profit():
    ts=[trade(1,'2026-09-24','BUY',1000),trade(2,'2026-10-02','SELL',650,5383.1236),
        trade(90,'2026-10-05','SELL',50,867.6206),trade(91,'2026-10-05','SELL',300,4661.6278),
        trade(92,'2026-10-05','BUY',1000)]
    c=next(iter(current_position_cycles(ts).values()))
    assert c['quantity']==1000 and c['startDate']=='2026-09-24'
    assert c['realizedNetPnl']==Decimal('10912.3720')

def test_flat_position_reopened_next_day_starts_new_cycle():
    ts=[trade(1,'2026-09-24','BUY',350),trade(2,'2026-10-05','SELL',350,5529),
        trade(3,'2026-10-06','BUY',1000)]
    c=next(iter(current_position_cycles(ts).values()))
    assert c['startDate']=='2026-10-06' and c['realizedNetPnl']==0
