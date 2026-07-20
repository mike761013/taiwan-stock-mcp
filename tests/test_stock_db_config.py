from stock_db.config import parse_bool, parse_int

def test_parse_bool():
    assert parse_bool("TRUE") is True
    assert parse_bool("0", True) is False
    assert parse_bool("unknown", True) is True

def test_parse_int():
    assert parse_int("0", 3, 1, 10) == 1
    assert parse_int("99", 3, 1, 10) == 10
    assert parse_int("x", 3, 1, 10) == 3
