from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from server_v10_tools import register_v10_tools
from stock_db.portfolio import (
    PortfolioLedgerError,
    allocate_sale,
    audit_original_plan,
    calculate_buy_values,
)


def lot(
    transaction_id: int,
    trade_date: str,
    price: float,
    remaining: int = 1_000,
    quantity: int = 1_000,
) -> dict:
    buy = calculate_buy_values(quantity, price)
    return {
        "transaction_id": transaction_id,
        "trade_date": trade_date,
        "quantity": quantity,
        "remaining_quantity": remaining,
        "price": price,
        "commission": buy["commission"],
        "margin_principal": None,
        "margin_annual_rate": None,
    }


def test_cathay_28_percent_discount_cost_factors() -> None:
    buy = calculate_buy_values(1_000, 100)
    assert buy == {
        "grossAmount": Decimal("100000.0000"),
        "commission": Decimal("39.9000"),
        "netCashFlow": Decimal("-100039.9000"),
        "effectiveCost": Decimal("100039.9000"),
    }

    normal = allocate_sale(
        [lot(1, "2026-09-01", 100)],
        quantity=1_000,
        sell_price=100,
        sell_date="2026-09-02",
    )[0]
    assert normal["sellProceeds"] == Decimal("99660.1000")
    assert normal["taxRate"] == Decimal("0.003")
    assert normal["realizedPnl"] == Decimal("-379.8000")

    day_trade = allocate_sale(
        [lot(2, "2026-09-02", 100)],
        quantity=1_000,
        sell_price=100,
        sell_date="2026-09-02",
    )[0]
    assert day_trade["sellProceeds"] == Decimal("99810.1000")
    assert day_trade["taxRate"] == Decimal("0.0015")

    etf = allocate_sale(
        [lot(3, "2026-09-01", 100)],
        quantity=1_000,
        sell_price=100,
        sell_date="2026-09-02",
        asset_type="ETF",
    )[0]
    assert etf["sellProceeds"] == Decimal("99860.1000")
    assert etf["taxRate"] == Decimal("0.001")


def test_same_day_best_pnl_is_matched_before_fifo() -> None:
    allocations = allocate_sale(
        [
            lot(1, "2026-08-01", 30),
            lot(2, "2026-09-02", 45),
            lot(3, "2026-09-02", 40),
        ],
        quantity=2_500,
        sell_price=50,
        sell_date="2026-09-02",
    )

    assert [item["buyTransactionId"] for item in allocations] == [3, 2, 1]
    assert [item["quantity"] for item in allocations] == [1_000, 1_000, 500]
    assert [item["matchingRule"] for item in allocations] == [
        "BROKER_SAME_DAY_BEST_PNL",
        "BROKER_SAME_DAY_BEST_PNL",
        "FIFO",
    ]
    assert [item["taxRate"] for item in allocations] == [
        Decimal("0.0015"),
        Decimal("0.0015"),
        Decimal("0.003"),
    ]


def test_sale_rejects_insufficient_inventory_and_invalid_day_trade() -> None:
    try:
        allocate_sale(
            [lot(1, "2026-09-01", 30)],
            quantity=2_000,
            sell_price=31,
            sell_date="2026-09-02",
        )
    except PortfolioLedgerError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("insufficient inventory must be rejected")

    try:
        allocate_sale(
            [lot(1, "2026-09-01", 30)],
            quantity=1_000,
            sell_price=31,
            sell_date="2026-09-02",
            tax_treatment="DAY_TRADE",
        )
    except PortfolioLedgerError as exc:
        assert "earlier-day" in str(exc)
    else:
        raise AssertionError("old inventory cannot use day-trade tax")


def test_entry_condition_is_never_invented_as_a_hard_stop() -> None:
    plan = {
        "verification_status": "VERIFIED",
        "entry_condition_price": Decimal("39"),
        "entry_condition_basis": "OPENING_ONLY",
        "signal_defense_price": None,
        "hard_stop_price": None,
    }
    audit = audit_original_plan(plan, Decimal("38.2"))

    assert audit["entryConditionNowHeld"] is False
    assert audit["entryConditionIsNotExitRule"] is True
    assert audit["status"] == "NO_EXPLICIT_EXIT_LEVEL"
    assert audit["exitSignal"] == "NOT_DEFINED"


def test_unverified_plan_stays_unverified_even_when_price_is_available() -> None:
    audit = audit_original_plan(
        {
            "verification_status": "UNVERIFIED",
            "hard_stop_price": Decimal("70"),
        },
        Decimal("65"),
    )
    assert audit["status"] == "ORIGINAL_PLAN_UNVERIFIED"
    assert audit["exitSignal"] == "UNVERIFIED"


def test_schema_and_mcp_tools_expose_the_permanent_ledger() -> None:
    schema = (
        Path(__file__).resolve().parents[1] / "stock_db" / "schema.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS portfolio_transactions" in schema
    assert "CREATE TABLE IF NOT EXISTS portfolio_position_plans" in schema
    assert "CREATE TABLE IF NOT EXISTS portfolio_lot_allocations" in schema
    assert "CREATE TABLE IF NOT EXISTS portfolio_trade_plan_links" in schema
    assert "Permanent portfolio trade and immutable entry-plan ledger" in schema

    class FakeMcp:
        def __init__(self) -> None:
            self.names: list[str] = []

        def tool(self):
            def decorator(function):
                self.names.append(function.__name__)
                return function
            return decorator

    mcp = FakeMcp()
    register_v10_tools(mcp)
    assert {
        "record_portfolio_trade",
        "record_position_plan",
        "get_portfolio_positions",
        "get_portfolio_history",
        "void_latest_portfolio_trade",
    }.issubset(mcp.names)

