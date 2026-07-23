from datetime import date, datetime

from stock_db.importers import parse_date


def test_parse_date_supports_gregorian_formats() -> None:
    assert parse_date("2026-07-21") == date(2026, 7, 21)
    assert parse_date("20260721") == date(2026, 7, 21)
    assert parse_date(datetime(2026, 7, 21, 15, 30)) == date(2026, 7, 21)


def test_parse_date_supports_roc_formats_from_tpex() -> None:
    assert parse_date("1150721") == date(2026, 7, 21)
    assert parse_date("115/07/21") == date(2026, 7, 21)
    assert parse_date("115-07-21") == date(2026, 7, 21)
