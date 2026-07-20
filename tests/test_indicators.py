from datetime import date, timedelta
from stock_db.indicators import calculate_indicators

def test_calculate_indicators():
    start = date(2026, 1, 1)
    rows = [
        {
            "symbol": "2330", "trade_date": start + timedelta(days=i),
            "close": 100 + i, "low": 99 + i, "volume": 1000 + i * 10,
        }
        for i in range(70)
    ]
    result = calculate_indicators(rows)
    assert len(result) == 70
    assert result[-1]["ma60"] is not None
    assert 0 <= result[-1]["technical_score"] <= 100
