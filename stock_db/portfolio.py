"""Permanent portfolio ledger and immutable original-entry plans.

The ledger deliberately keeps actual trades separate from the plan that led to
an entry.  Closing reviews may compare a holding with its original plan, but
they never rewrite that plan with a newer technical level.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .connection import StockDatabase, stock_database
from .repository import StockRepository


COMMISSION_RATE = Decimal("0.000399")
STOCK_TAX_RATE = Decimal("0.003")
STOCK_DAY_TRADE_TAX_RATE = Decimal("0.0015")
ETF_TAX_RATE = Decimal("0.001")
DEFAULT_MARGIN_ANNUAL_RATE = Decimal("0.0645")
MONEY_QUANTUM = Decimal("0.0001")
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

VALID_SIDES = {"BUY", "SELL"}
VALID_ACCOUNT_TYPES = {"CASH", "MARGIN", "SHORT"}
VALID_ASSET_TYPES = {"STOCK", "ETF"}
VALID_LOT_TYPES = {"REGULAR", "RECURRING"}
VALID_TAX_TREATMENTS = {"AUTO", "NORMAL", "DAY_TRADE"}
VALID_PLAN_STATUSES = {"VERIFIED", "UNVERIFIED", "DISPUTED"}
VALID_ENTRY_BASES = {"OPENING_ONLY", "INTRADAY", "CLOSE", "UNSPECIFIED"}


class PortfolioLedgerError(ValueError):
    """User-correctable portfolio input or consistency error."""


def _decimal(value: Any, field: str, *, allow_none: bool = False) -> Decimal | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise PortfolioLedgerError(f"{field} is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioLedgerError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise PortfolioLedgerError(f"{field} must be finite")
    return parsed


def _positive_decimal(
    value: Any,
    field: str,
    *,
    allow_none: bool = False,
) -> Decimal | None:
    parsed = _decimal(value, field, allow_none=allow_none)
    if parsed is not None and parsed <= 0:
        raise PortfolioLedgerError(f"{field} must be greater than zero")
    return parsed


def _parse_date(value: date | str, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PortfolioLedgerError(f"{field} must use YYYY-MM-DD") from exc


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def calculate_buy_values(
    quantity: int,
    price: Decimal | float | str,
    commission_rate: Decimal = COMMISSION_RATE,
) -> dict[str, Decimal]:
    """Return the user's Cathay 28%-discount stock purchase values."""
    unit_price = _positive_decimal(price, "price")
    assert unit_price is not None
    gross = unit_price * int(quantity)
    commission = gross * commission_rate
    return {
        "grossAmount": _money(gross),
        "commission": _money(commission),
        "netCashFlow": _money(-(gross + commission)),
        "effectiveCost": _money(gross + commission),
    }


def _sell_tax_rate(
    asset_type: str,
    tax_treatment: str,
    same_day: bool,
) -> Decimal:
    if asset_type == "ETF":
        return ETF_TAX_RATE
    if tax_treatment == "DAY_TRADE":
        if not same_day:
            raise PortfolioLedgerError(
                "DAY_TRADE sale cannot allocate to an earlier-day lot"
            )
        return STOCK_DAY_TRADE_TAX_RATE
    if tax_treatment == "AUTO" and same_day:
        return STOCK_DAY_TRADE_TAX_RATE
    return STOCK_TAX_RATE


def allocate_sale(
    open_lots: Sequence[Mapping[str, Any]],
    *,
    quantity: int,
    sell_price: Decimal | float | str,
    sell_date: date | str,
    asset_type: str = "STOCK",
    tax_treatment: str = "AUTO",
    commission_rate: Decimal = COMMISSION_RATE,
) -> list[dict[str, Any]]:
    """Allocate a sale using same-day best-P/L matching, then FIFO.

    Same-day buys are matched before inventory and sorted by lowest effective
    purchase cost.  Remaining quantity is matched to older lots by date/id.
    """
    if int(quantity) <= 0:
        raise PortfolioLedgerError("quantity must be greater than zero")
    sell_day = _parse_date(sell_date, "sell_date")
    unit_sell_price = _positive_decimal(sell_price, "sell_price")
    assert unit_sell_price is not None
    asset = str(asset_type).upper()
    treatment = str(tax_treatment).upper()
    if asset not in VALID_ASSET_TYPES:
        raise PortfolioLedgerError(f"unsupported asset_type: {asset_type}")
    if treatment not in VALID_TAX_TREATMENTS:
        raise PortfolioLedgerError(
            f"unsupported tax_treatment: {tax_treatment}"
        )

    eligible: list[dict[str, Any]] = []
    for raw in open_lots:
        remaining = int(raw.get("remaining_quantity") or 0)
        if remaining <= 0:
            continue
        buy_date = _parse_date(raw.get("trade_date"), "buy trade_date")
        if buy_date > sell_day:
            continue
        buy_quantity = int(raw.get("quantity") or 0)
        if buy_quantity <= 0:
            raise PortfolioLedgerError("open lot has invalid original quantity")
        buy_price = _positive_decimal(raw.get("price"), "buy price")
        buy_commission = _decimal(
            raw.get("commission") or 0,
            "buy commission",
        )
        assert buy_price is not None and buy_commission is not None
        effective_unit_cost = buy_price + buy_commission / buy_quantity
        eligible.append({
            **dict(raw),
            "trade_date": buy_date,
            "remaining_quantity": remaining,
            "quantity": buy_quantity,
            "price": buy_price,
            "commission": buy_commission,
            "effective_unit_cost": effective_unit_cost,
        })

    same_day_lots = sorted(
        (lot for lot in eligible if lot["trade_date"] == sell_day),
        key=lambda lot: (
            lot["effective_unit_cost"],
            int(lot.get("transaction_id") or lot.get("id") or 0),
        ),
    )
    fifo_lots = sorted(
        (lot for lot in eligible if lot["trade_date"] < sell_day),
        key=lambda lot: (
            lot["trade_date"],
            int(lot.get("transaction_id") or lot.get("id") or 0),
        ),
    )
    ordered = [*same_day_lots, *fifo_lots]
    available = sum(lot["remaining_quantity"] for lot in ordered)
    if available < int(quantity):
        raise PortfolioLedgerError(
            f"insufficient open quantity: requested {quantity}, available {available}"
        )

    outstanding = int(quantity)
    allocations: list[dict[str, Any]] = []
    for lot in ordered:
        if outstanding <= 0:
            break
        allocated_quantity = min(outstanding, lot["remaining_quantity"])
        same_day = lot["trade_date"] == sell_day
        tax_rate = _sell_tax_rate(asset, treatment, same_day)
        sell_gross = unit_sell_price * allocated_quantity
        sell_commission = sell_gross * commission_rate
        transaction_tax = sell_gross * tax_rate
        sell_net = sell_gross - sell_commission - transaction_tax
        buy_fee = lot["commission"] * Decimal(allocated_quantity) / lot["quantity"]
        buy_cost = lot["price"] * allocated_quantity + buy_fee

        margin_interest = Decimal("0")
        margin_principal = _decimal(
            lot.get("margin_principal"),
            "margin_principal",
            allow_none=True,
        )
        if margin_principal is not None:
            annual_rate = _decimal(
                lot.get("margin_annual_rate") or DEFAULT_MARGIN_ANNUAL_RATE,
                "margin_annual_rate",
            )
            assert annual_rate is not None
            held_days = max((sell_day - lot["trade_date"]).days, 0)
            allocated_principal = (
                margin_principal
                * Decimal(allocated_quantity)
                / Decimal(lot["quantity"])
            )
            margin_interest = (
                allocated_principal
                * annual_rate
                * Decimal(held_days)
                / Decimal(365)
            )

        allocations.append({
            "buyTransactionId": int(
                lot.get("transaction_id") or lot.get("id")
            ),
            "quantity": allocated_quantity,
            "matchingRule": (
                "BROKER_SAME_DAY_BEST_PNL" if same_day else "FIFO"
            ),
            "buyCost": _money(buy_cost),
            "sellGross": _money(sell_gross),
            "sellProceeds": _money(sell_net),
            "sellCommission": _money(sell_commission),
            "transactionTax": _money(transaction_tax),
            "taxRate": tax_rate,
            "marginInterest": _money(margin_interest),
            "realizedPnl": _money(sell_net - buy_cost - margin_interest),
        })
        outstanding -= allocated_quantity

    return allocations


def audit_original_plan(
    plan: Mapping[str, Any] | None,
    latest_close: Decimal | float | str | None,
) -> dict[str, Any]:
    """Compare a close with explicit original-plan fields without rewriting it."""
    if not plan:
        return {
            "status": "NO_LINKED_ORIGINAL_PLAN",
            "exitSignal": "UNVERIFIED",
            "entryConditionIsNotExitRule": True,
        }
    status = str(
        plan.get("verification_status")
        or plan.get("verificationStatus")
        or "UNVERIFIED"
    ).upper()
    close = _decimal(latest_close, "latest_close", allow_none=True)
    entry_price = _decimal(
        plan.get("entry_condition_price")
        if "entry_condition_price" in plan
        else plan.get("entryConditionPrice"),
        "entry_condition_price",
        allow_none=True,
    )
    defense = _decimal(
        plan.get("signal_defense_price")
        if "signal_defense_price" in plan
        else plan.get("signalDefensePrice"),
        "signal_defense_price",
        allow_none=True,
    )
    hard_stop = _decimal(
        plan.get("hard_stop_price")
        if "hard_stop_price" in plan
        else plan.get("hardStopPrice"),
        "hard_stop_price",
        allow_none=True,
    )

    entry_held = None if close is None or entry_price is None else close >= entry_price
    if status != "VERIFIED":
        audit_status = "ORIGINAL_PLAN_UNVERIFIED"
        exit_signal = "UNVERIFIED"
    elif close is None:
        audit_status = "MARKET_PRICE_UNAVAILABLE"
        exit_signal = "UNAVAILABLE"
    elif hard_stop is not None and close <= hard_stop:
        audit_status = "HARD_STOP_BREACHED"
        exit_signal = "HARD_STOP_BREACHED"
    elif defense is not None and close < defense:
        audit_status = "SIGNAL_DEFENSE_BREACHED"
        exit_signal = "SIGNAL_DEFENSE_BREACHED"
    elif hard_stop is not None or defense is not None:
        audit_status = "EXPLICIT_DEFENSE_HELD"
        exit_signal = "NONE"
    else:
        audit_status = "NO_EXPLICIT_EXIT_LEVEL"
        exit_signal = "NOT_DEFINED"

    return {
        "status": audit_status,
        "exitSignal": exit_signal,
        "verificationStatus": status,
        "latestClose": _json_safe(close),
        "entryConditionPrice": _json_safe(entry_price),
        "entryConditionBasis": plan.get("entry_condition_basis")
            or plan.get("entryConditionBasis"),
        "entryConditionNowHeld": entry_held,
        "entryConditionIsNotExitRule": True,
        "signalDefensePrice": _json_safe(defense),
        "hardStopPrice": _json_safe(hard_stop),
        "originalPlanImmutable": True,
    }


class PortfolioLedger:
    def __init__(self, database: StockDatabase | None = None) -> None:
        self.database = database or stock_database
        self._schema_checked = False
        self._schema_lock = asyncio.Lock()

    async def _ensure_schema(self) -> None:
        if self._schema_checked:
            return
        async with self._schema_lock:
            if self._schema_checked:
                return
            async with self.database.acquire() as connection:
                exists = await connection.fetchval(
                    "SELECT to_regclass('portfolio_transactions') IS NOT NULL"
                )
            if not exists:
                await StockRepository(self.database).initialize_schema()
            self._schema_checked = True

    async def _fetch_transaction(self, transaction_id: int) -> dict[str, Any]:
        async with self.database.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM portfolio_transactions WHERE id=$1",
                int(transaction_id),
            )
        if row is None:
            raise PortfolioLedgerError(
                f"portfolio transaction {transaction_id} was not found"
            )
        return _json_safe(dict(row))

    async def record_trade(
        self,
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal | float | str,
        trade_date: date | str,
        account_type: str = "CASH",
        asset_type: str = "STOCK",
        lot_type: str = "REGULAR",
        tax_treatment: str = "AUTO",
        margin_principal: Decimal | float | str | None = None,
        margin_annual_rate: Decimal | float | str | None = None,
        plan_id: int | None = None,
        client_reference: str | None = None,
        source: str = "manual",
        notes: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_schema()
        normalized_symbol = str(symbol).strip().upper()
        normalized_side = str(side).strip().upper()
        account = str(account_type).strip().upper()
        asset = str(asset_type).strip().upper()
        lot = str(lot_type).strip().upper()
        treatment = str(tax_treatment).strip().upper()
        day = _parse_date(trade_date, "trade_date")
        unit_price = _positive_decimal(price, "price")
        assert unit_price is not None
        shares = int(quantity)

        if not normalized_symbol:
            raise PortfolioLedgerError("symbol is required")
        if normalized_side not in VALID_SIDES:
            raise PortfolioLedgerError(f"unsupported side: {side}")
        if shares <= 0:
            raise PortfolioLedgerError("quantity must be greater than zero")
        if account not in VALID_ACCOUNT_TYPES:
            raise PortfolioLedgerError(f"unsupported account_type: {account_type}")
        if asset not in VALID_ASSET_TYPES:
            raise PortfolioLedgerError(f"unsupported asset_type: {asset_type}")
        if lot not in VALID_LOT_TYPES:
            raise PortfolioLedgerError(f"unsupported lot_type: {lot_type}")
        if treatment not in VALID_TAX_TREATMENTS:
            raise PortfolioLedgerError(
                f"unsupported tax_treatment: {tax_treatment}"
            )
        if account != "MARGIN" and margin_principal not in (None, ""):
            raise PortfolioLedgerError(
                "margin_principal is allowed only for MARGIN trades"
            )

        principal = _positive_decimal(
            margin_principal,
            "margin_principal",
            allow_none=True,
        )
        annual_rate = _positive_decimal(
            margin_annual_rate,
            "margin_annual_rate",
            allow_none=True,
        )
        if account == "MARGIN" and annual_rate is None:
            annual_rate = DEFAULT_MARGIN_ANNUAL_RATE
        reference = str(client_reference).strip() if client_reference else None

        async with self.database.acquire() as connection:
            async with connection.transaction():
                if reference:
                    existing = await connection.fetchrow(
                        """
                        SELECT * FROM portfolio_transactions
                        WHERE client_reference=$1
                        """,
                        reference,
                    )
                    if existing is not None:
                        return {
                            "ok": True,
                            "idempotentReplay": True,
                            "transaction": _json_safe(dict(existing)),
                        }

                if plan_id is not None:
                    plan_symbol = await connection.fetchval(
                        "SELECT symbol FROM portfolio_position_plans WHERE id=$1",
                        int(plan_id),
                    )
                    if plan_symbol is None:
                        raise PortfolioLedgerError(
                            f"position plan {plan_id} was not found"
                        )
                    if str(plan_symbol) != normalized_symbol:
                        raise PortfolioLedgerError(
                            "trade symbol does not match linked plan symbol"
                        )

                if normalized_side == "BUY":
                    values = calculate_buy_values(shares, unit_price)
                    transaction_id = int(await connection.fetchval(
                        """
                        INSERT INTO portfolio_transactions(
                            client_reference,symbol,trade_date,side,quantity,price,
                            account_type,asset_type,lot_type,tax_treatment,
                            commission_rate,gross_amount,commission,
                            transaction_tax,margin_principal,margin_annual_rate,
                            margin_interest,net_cash_flow,source,notes,metadata
                        ) VALUES(
                            $1,$2,$3,'BUY',$4,$5,$6,$7,$8,$9,$10,$11,$12,0,
                            $13,$14,0,$15,$16,$17,$18::jsonb
                        ) RETURNING id
                        """,
                        reference, normalized_symbol, day, shares, unit_price,
                        account, asset, lot, treatment, COMMISSION_RATE,
                        values["grossAmount"], values["commission"], principal,
                        annual_rate, values["netCashFlow"], source, notes,
                        json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    ))
                    allocations: list[dict[str, Any]] = []
                else:
                    raw_open_lots = await connection.fetch(
                        """
                        SELECT
                            buy.id AS transaction_id,
                            buy.trade_date,
                            buy.quantity,
                            buy.price,
                            buy.commission,
                            buy.margin_principal,
                            buy.margin_annual_rate,
                            buy.quantity - COALESCE((
                                SELECT SUM(allocation.quantity)
                                FROM portfolio_lot_allocations allocation
                                WHERE allocation.buy_transaction_id=buy.id
                            ), 0) AS remaining_quantity
                        FROM portfolio_transactions buy
                        WHERE buy.symbol=$1
                          AND buy.side='BUY'
                          AND buy.account_type=$2
                          AND buy.asset_type=$3
                          AND buy.lot_type=$4
                          AND buy.voided_at IS NULL
                          AND buy.trade_date <= $5
                        ORDER BY buy.trade_date, buy.id
                        FOR UPDATE
                        """,
                        normalized_symbol, account, asset, lot, day,
                    )
                    allocations = allocate_sale(
                        [dict(item) for item in raw_open_lots],
                        quantity=shares,
                        sell_price=unit_price,
                        sell_date=day,
                        asset_type=asset,
                        tax_treatment=treatment,
                    )
                    gross = _money(unit_price * shares)
                    commission = _money(sum(
                        (item["sellCommission"] for item in allocations),
                        Decimal("0"),
                    ))
                    transaction_tax = _money(sum(
                        (item["transactionTax"] for item in allocations),
                        Decimal("0"),
                    ))
                    interest = _money(sum(
                        (item["marginInterest"] for item in allocations),
                        Decimal("0"),
                    ))
                    realized = _money(sum(
                        (item["realizedPnl"] for item in allocations),
                        Decimal("0"),
                    ))
                    net_cash_flow = _money(
                        gross - commission - transaction_tax - interest
                    )
                    transaction_id = int(await connection.fetchval(
                        """
                        INSERT INTO portfolio_transactions(
                            client_reference,symbol,trade_date,side,quantity,price,
                            account_type,asset_type,lot_type,tax_treatment,
                            commission_rate,gross_amount,commission,
                            transaction_tax,margin_interest,net_cash_flow,
                            realized_pnl,source,notes,metadata
                        ) VALUES(
                            $1,$2,$3,'SELL',$4,$5,$6,$7,$8,$9,$10,$11,$12,
                            $13,$14,$15,$16,$17,$18,$19::jsonb
                        ) RETURNING id
                        """,
                        reference, normalized_symbol, day, shares, unit_price,
                        account, asset, lot, treatment, COMMISSION_RATE, gross,
                        commission, transaction_tax, interest, net_cash_flow,
                        realized, source, notes,
                        json.dumps(metadata or {}, ensure_ascii=False, default=str),
                    ))
                    for allocation in allocations:
                        await connection.execute(
                            """
                            INSERT INTO portfolio_lot_allocations(
                                sell_transaction_id,buy_transaction_id,quantity,
                                matching_rule,buy_cost,sell_proceeds,
                                sell_commission,transaction_tax,tax_rate,
                                margin_interest,realized_pnl
                            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                            """,
                            transaction_id,
                            allocation["buyTransactionId"],
                            allocation["quantity"],
                            allocation["matchingRule"],
                            allocation["buyCost"],
                            allocation["sellProceeds"],
                            allocation["sellCommission"],
                            allocation["transactionTax"],
                            allocation["taxRate"],
                            allocation["marginInterest"],
                            allocation["realizedPnl"],
                        )

                if plan_id is not None:
                    await connection.execute(
                        """
                        INSERT INTO portfolio_trade_plan_links(
                            transaction_id,plan_id,relation
                        ) VALUES($1,$2,'ORIGINAL_ENTRY')
                        ON CONFLICT(transaction_id,plan_id) DO NOTHING
                        """,
                        transaction_id,
                        int(plan_id),
                    )

                saved = await connection.fetchrow(
                    "SELECT * FROM portfolio_transactions WHERE id=$1",
                    transaction_id,
                )

        result = {
            "ok": True,
            "idempotentReplay": False,
            "transaction": _json_safe(dict(saved)),
            "allocations": _json_safe(allocations),
            "costRules": {
                "commissionRate": float(COMMISSION_RATE),
                "stockTaxRate": float(STOCK_TAX_RATE),
                "stockDayTradeTaxRate": float(STOCK_DAY_TRADE_TAX_RATE),
                "etfTaxRate": float(ETF_TAX_RATE),
                "marginAnnualRateDefault": float(DEFAULT_MARGIN_ANNUAL_RATE),
                "matching": "same-day best realized P/L, then FIFO",
            },
        }
        result["position"] = await self.get_positions(
            symbol=normalized_symbol,
            include_closed=True,
        )
        return result

    async def record_plan(
        self,
        *,
        symbol: str,
        plan_date: date | str,
        source_type: str,
        verification_status: str = "UNVERIFIED",
        source_reference: str | None = None,
        action_code: str | None = None,
        trial_price: Any = None,
        entry_low: Any = None,
        entry_high: Any = None,
        confirmation_price: Any = None,
        maximum_entry_price: Any = None,
        planned_position_percent: Any = None,
        entry_condition_price: Any = None,
        entry_condition_basis: str | None = None,
        signal_defense_price: Any = None,
        hard_stop_price: Any = None,
        entry_condition: str | None = None,
        invalidation_condition: str | None = None,
        evidence_reference: str | None = None,
        notes: str | None = None,
        plan_snapshot: Mapping[str, Any] | None = None,
        supersedes_plan_id: int | None = None,
        link_transaction_id: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_schema()
        normalized_symbol = str(symbol).strip().upper()
        day = _parse_date(plan_date, "plan_date")
        status = str(verification_status).strip().upper()
        basis = (
            str(entry_condition_basis).strip().upper()
            if entry_condition_basis
            else None
        )
        if not normalized_symbol:
            raise PortfolioLedgerError("symbol is required")
        if status not in VALID_PLAN_STATUSES:
            raise PortfolioLedgerError(
                f"unsupported verification_status: {verification_status}"
            )
        if basis not in VALID_ENTRY_BASES and basis is not None:
            raise PortfolioLedgerError(
                f"unsupported entry_condition_basis: {entry_condition_basis}"
            )

        numeric = {
            "trial_price": _positive_decimal(
                trial_price, "trial_price", allow_none=True
            ),
            "entry_low": _positive_decimal(
                entry_low, "entry_low", allow_none=True
            ),
            "entry_high": _positive_decimal(
                entry_high, "entry_high", allow_none=True
            ),
            "confirmation_price": _positive_decimal(
                confirmation_price, "confirmation_price", allow_none=True
            ),
            "maximum_entry_price": _positive_decimal(
                maximum_entry_price,
                "maximum_entry_price",
                allow_none=True,
            ),
            "planned_position_percent": _positive_decimal(
                planned_position_percent,
                "planned_position_percent",
                allow_none=True,
            ),
            "entry_condition_price": _positive_decimal(
                entry_condition_price,
                "entry_condition_price",
                allow_none=True,
            ),
            "signal_defense_price": _positive_decimal(
                signal_defense_price,
                "signal_defense_price",
                allow_none=True,
            ),
            "hard_stop_price": _positive_decimal(
                hard_stop_price,
                "hard_stop_price",
                allow_none=True,
            ),
        }
        if (
            numeric["entry_low"] is not None
            and numeric["entry_high"] is not None
            and numeric["entry_low"] > numeric["entry_high"]
        ):
            raise PortfolioLedgerError("entry_low cannot exceed entry_high")
        if (
            numeric["planned_position_percent"] is not None
            and numeric["planned_position_percent"] > 100
        ):
            raise PortfolioLedgerError(
                "planned_position_percent cannot exceed 100"
            )

        async with self.database.acquire() as connection:
            async with connection.transaction():
                if supersedes_plan_id is not None:
                    old_symbol = await connection.fetchval(
                        "SELECT symbol FROM portfolio_position_plans WHERE id=$1",
                        int(supersedes_plan_id),
                    )
                    if old_symbol is None:
                        raise PortfolioLedgerError(
                            f"superseded plan {supersedes_plan_id} was not found"
                        )
                    if str(old_symbol) != normalized_symbol:
                        raise PortfolioLedgerError(
                            "superseded plan belongs to another symbol"
                        )
                if link_transaction_id is not None:
                    trade_symbol = await connection.fetchval(
                        "SELECT symbol FROM portfolio_transactions WHERE id=$1",
                        int(link_transaction_id),
                    )
                    if trade_symbol is None:
                        raise PortfolioLedgerError(
                            f"portfolio transaction {link_transaction_id} was not found"
                        )
                    if str(trade_symbol) != normalized_symbol:
                        raise PortfolioLedgerError(
                            "linked transaction belongs to another symbol"
                        )

                row = await connection.fetchrow(
                    """
                    INSERT INTO portfolio_position_plans(
                        symbol,plan_date,source_type,source_reference,
                        verification_status,action_code,trial_price,entry_low,
                        entry_high,confirmation_price,maximum_entry_price,
                        planned_position_percent,entry_condition_price,
                        entry_condition_basis,signal_defense_price,
                        hard_stop_price,entry_condition,invalidation_condition,
                        evidence_reference,notes,plan_snapshot,supersedes_plan_id
                    ) VALUES(
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                        $16,$17,$18,$19,$20,$21::jsonb,$22
                    ) RETURNING *
                    """,
                    normalized_symbol, day, source_type, source_reference,
                    status, action_code, numeric["trial_price"],
                    numeric["entry_low"], numeric["entry_high"],
                    numeric["confirmation_price"],
                    numeric["maximum_entry_price"],
                    numeric["planned_position_percent"],
                    numeric["entry_condition_price"], basis,
                    numeric["signal_defense_price"],
                    numeric["hard_stop_price"], entry_condition,
                    invalidation_condition, evidence_reference, notes,
                    json.dumps(
                        plan_snapshot or {}, ensure_ascii=False, default=str
                    ),
                    supersedes_plan_id,
                )
                if link_transaction_id is not None:
                    await connection.execute(
                        """
                        INSERT INTO portfolio_trade_plan_links(
                            transaction_id,plan_id,relation
                        ) VALUES($1,$2,'ORIGINAL_ENTRY')
                        ON CONFLICT(transaction_id,plan_id) DO NOTHING
                        """,
                        int(link_transaction_id),
                        int(row["id"]),
                    )
        return {
            "ok": True,
            "plan": _json_safe(dict(row)),
            "immutable": True,
            "message": (
                "Original plan stored. Future reviews compare against it "
                "without overwriting it."
            ),
        }

    async def get_positions(
        self,
        *,
        symbol: str | None = None,
        include_closed: bool = False,
        exclude_symbols: Sequence[str] = ("0050",),
        as_of_date: date | str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_schema()
        normalized_symbol = str(symbol).strip().upper() if symbol else None
        exclusions = {
            str(item).strip().upper() for item in exclude_symbols if str(item).strip()
        }
        through = (
            _parse_date(as_of_date, "as_of_date")
            if as_of_date
            else datetime.now(TAIPEI_TZ).date()
        )

        clauses = ["buy.side='BUY'", "buy.voided_at IS NULL"]
        args: list[Any] = []
        if normalized_symbol:
            args.append(normalized_symbol)
            clauses.append(f"buy.symbol=${len(args)}")
        if exclusions:
            args.append(sorted(exclusions))
            clauses.append(f"NOT (buy.symbol=ANY(${len(args)}::varchar[]))")
        args.append(through)
        clauses.append(f"buy.trade_date<=${len(args)}")

        async with self.database.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT
                    buy.*,
                    buy.quantity - COALESCE((
                        SELECT SUM(allocation.quantity)
                        FROM portfolio_lot_allocations allocation
                        JOIN portfolio_transactions sell
                          ON sell.id=allocation.sell_transaction_id
                        WHERE allocation.buy_transaction_id=buy.id
                          AND sell.voided_at IS NULL
                          AND sell.trade_date <= ${len(args)}
                    ), 0) AS remaining_quantity
                FROM portfolio_transactions buy
                WHERE {' AND '.join(clauses)}
                ORDER BY buy.symbol, buy.trade_date DESC, buy.id DESC
                """,
                *args,
            )
            lots = [dict(row) for row in rows]
            if not include_closed:
                lots = [lot for lot in lots if int(lot["remaining_quantity"]) > 0]

            symbols = sorted({str(lot["symbol"]) for lot in lots})
            market_rows = []
            if symbols:
                market_rows = await connection.fetch(
                    """
                    SELECT DISTINCT ON (bar.symbol)
                        bar.symbol,bar.trade_date,bar.close,
                        indicator.ma5,indicator.ma10,indicator.ma20,indicator.ma60,
                        indicator.bollinger_lower,indicator.bollinger_mid,
                        indicator.bollinger_upper,indicator.large_volume_low
                    FROM daily_bars bar
                    LEFT JOIN daily_indicators indicator
                      ON indicator.symbol=bar.symbol
                     AND indicator.trade_date=bar.trade_date
                    WHERE bar.symbol=ANY($1::varchar[])
                      AND bar.trade_date <= $2
                    ORDER BY bar.symbol,bar.trade_date DESC
                    """,
                    symbols,
                    through,
                )
            market = {str(row["symbol"]): dict(row) for row in market_rows}

            transaction_ids = [int(lot["id"]) for lot in lots]
            plan_rows = []
            if transaction_ids:
                plan_rows = await connection.fetch(
                    """
                    SELECT link.transaction_id,plan.*
                    FROM portfolio_trade_plan_links link
                    JOIN portfolio_position_plans plan ON plan.id=link.plan_id
                    WHERE link.transaction_id=ANY($1::bigint[])
                    ORDER BY plan.plan_date,plan.id
                    """,
                    transaction_ids,
                )

        plans_by_transaction: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for raw in plan_rows:
            item = dict(raw)
            transaction_id = int(item.pop("transaction_id"))
            plans_by_transaction[transaction_id].append(item)

        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for lot_row in lots:
            remaining = int(lot_row["remaining_quantity"])
            if remaining <= 0 and not include_closed:
                continue
            key = (
                str(lot_row["symbol"]),
                str(lot_row["account_type"]),
                str(lot_row["asset_type"]),
                str(lot_row["lot_type"]),
            )
            group = grouped.setdefault(key, {
                "symbol": key[0],
                "accountType": key[1],
                "assetType": key[2],
                "lotType": key[3],
                "quantity": 0,
                "rawPurchaseAmount": Decimal("0"),
                "costBasisIncludingBuyFee": Decimal("0"),
                "accruedMarginInterest": Decimal("0"),
                "lots": [],
                "originalPlans": [],
            })
            price = Decimal(str(lot_row["price"]))
            original_quantity = int(lot_row["quantity"])
            buy_commission = Decimal(str(lot_row["commission"]))
            fee_share = (
                buy_commission * Decimal(remaining) / Decimal(original_quantity)
                if original_quantity
                else Decimal("0")
            )
            raw_amount = price * remaining
            cost_basis = raw_amount + fee_share
            accrued_interest = Decimal("0")
            principal = lot_row.get("margin_principal")
            if principal is not None and remaining > 0:
                held_days = max((through - lot_row["trade_date"]).days, 0)
                allocated_principal = (
                    Decimal(str(principal))
                    * Decimal(remaining)
                    / Decimal(original_quantity)
                )
                annual_rate = Decimal(str(
                    lot_row.get("margin_annual_rate")
                    or DEFAULT_MARGIN_ANNUAL_RATE
                ))
                accrued_interest = (
                    allocated_principal
                    * annual_rate
                    * Decimal(held_days)
                    / Decimal(365)
                )
            linked_plans = plans_by_transaction.get(int(lot_row["id"]), [])
            latest = market.get(key[0]) or {}
            plan_audits = [
                audit_original_plan(plan, latest.get("close"))
                for plan in linked_plans
            ]
            group["quantity"] += remaining
            group["rawPurchaseAmount"] += raw_amount
            group["costBasisIncludingBuyFee"] += cost_basis
            group["accruedMarginInterest"] += accrued_interest
            group["lots"].append({
                "transactionId": int(lot_row["id"]),
                "tradeDate": lot_row["trade_date"].isoformat(),
                "originalQuantity": original_quantity,
                "remainingQuantity": remaining,
                "price": float(price),
                "buyFeeAllocated": float(_money(fee_share)),
                "costBasisIncludingBuyFee": float(_money(cost_basis)),
                "accruedMarginInterest": float(_money(accrued_interest)),
                "source": lot_row.get("source"),
                "notes": lot_row.get("notes"),
                "originalPlans": _json_safe(linked_plans),
                "planAudits": plan_audits,
            })
            group["originalPlans"].extend(_json_safe(linked_plans))

        positions: list[dict[str, Any]] = []
        for key, group in grouped.items():
            latest = market.get(key[0]) or {}
            quantity_open = int(group["quantity"])
            raw_amount = group["rawPurchaseAmount"]
            cost_basis = group["costBasisIncludingBuyFee"]
            accrued_interest = group["accruedMarginInterest"]
            latest_close = _decimal(
                latest.get("close"), "latest close", allow_none=True
            )
            current_value = (
                latest_close * quantity_open if latest_close is not None else None
            )
            exit_fee = (
                current_value * COMMISSION_RATE
                if current_value is not None
                else None
            )
            exit_tax_rate = (
                ETF_TAX_RATE if key[2] == "ETF" else STOCK_TAX_RATE
            )
            exit_tax = (
                current_value * exit_tax_rate
                if current_value is not None
                else None
            )
            estimated_net_proceeds = (
                current_value - exit_fee - exit_tax
                if current_value is not None
                and exit_fee is not None
                and exit_tax is not None
                else None
            )
            gross_pnl = (
                current_value - raw_amount
                if current_value is not None
                else None
            )
            net_pnl = (
                estimated_net_proceeds - cost_basis - accrued_interest
                if estimated_net_proceeds is not None
                else None
            )
            positions.append({
                "symbol": key[0],
                "accountType": key[1],
                "assetType": key[2],
                "lotType": key[3],
                "quantity": quantity_open,
                "averagePrice": (
                    round(float(raw_amount / quantity_open), 4)
                    if quantity_open > 0 else None
                ),
                "averageCostIncludingBuyFee": (
                    round(float(cost_basis / quantity_open), 4)
                    if quantity_open > 0 else None
                ),
                "rawPurchaseAmount": float(_money(raw_amount)),
                "costBasisIncludingBuyFee": float(_money(cost_basis)),
                "accruedMarginInterest": float(_money(accrued_interest)),
                "market": _json_safe(latest),
                "currentMarketValue": (
                    float(_money(current_value))
                    if current_value is not None else None
                ),
                "estimatedExitFee": (
                    float(_money(exit_fee)) if exit_fee is not None else None
                ),
                "estimatedExitTax": (
                    float(_money(exit_tax)) if exit_tax is not None else None
                ),
                "estimatedNetProceeds": (
                    float(_money(estimated_net_proceeds))
                    if estimated_net_proceeds is not None else None
                ),
                "grossPnl": (
                    float(_money(gross_pnl)) if gross_pnl is not None else None
                ),
                "grossReturnPercent": (
                    round(float(gross_pnl / raw_amount * 100), 4)
                    if gross_pnl is not None and raw_amount else None
                ),
                "estimatedNetPnl": (
                    float(_money(net_pnl)) if net_pnl is not None else None
                ),
                "estimatedNetReturnPercent": (
                    round(float(net_pnl / cost_basis * 100), 4)
                    if net_pnl is not None and cost_basis else None
                ),
                "lots": group["lots"],
                "originalPlans": group["originalPlans"],
            })

        positions.sort(key=lambda item: (
            item["symbol"], item["accountType"], item["lotType"]
        ))
        return {
            "ok": True,
            "asOfDate": through.isoformat(),
            "positionCount": sum(1 for item in positions if item["quantity"] > 0),
            "positions": positions,
            "excludedSymbols": sorted(exclusions),
            "costRules": {
                "buyFactor": 1.000399,
                "stockSellFactor": 0.996601,
                "etfSellFactor": 0.998601,
                "stockDayTradeSellFactor": 0.998101,
                "inventoryMatching": "same-day best realized P/L, then FIFO",
                "lotDisplayOrder": "newest to oldest",
            },
            "planRule": (
                "Entry conditions, defense, and hard stops are separate. "
                "An entry condition is never promoted to a stop automatically."
            ),
        }

    async def get_history(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        include_voided: bool = False,
    ) -> dict[str, Any]:
        await self._ensure_schema()
        clauses = ["TRUE"]
        args: list[Any] = []
        normalized_symbol = str(symbol).strip().upper() if symbol else None
        if normalized_symbol:
            args.append(normalized_symbol)
            clauses.append(f"trade.symbol=${len(args)}")
        if not include_voided:
            clauses.append("trade.voided_at IS NULL")
        args.append(max(1, min(int(limit), 500)))
        async with self.database.acquire() as connection:
            transactions = await connection.fetch(
                f"""
                SELECT trade.*
                FROM portfolio_transactions trade
                WHERE {' AND '.join(clauses)}
                ORDER BY trade.trade_date DESC,trade.id DESC
                LIMIT ${len(args)}
                """,
                *args,
            )
            ids = [int(row["id"]) for row in transactions]
            allocations = []
            if ids:
                allocations = await connection.fetch(
                    """
                    SELECT * FROM portfolio_lot_allocations
                    WHERE sell_transaction_id=ANY($1::bigint[])
                       OR buy_transaction_id=ANY($1::bigint[])
                    ORDER BY sell_transaction_id,buy_transaction_id
                    """,
                    ids,
                )
            plan_args: list[Any] = []
            plan_clause = "TRUE"
            if normalized_symbol:
                plan_args.append(normalized_symbol)
                plan_clause = "plan.symbol=$1"
            plans = await connection.fetch(
                f"""
                SELECT plan.*,
                       COALESCE((
                           SELECT JSONB_AGG(link.transaction_id ORDER BY link.transaction_id)
                           FROM portfolio_trade_plan_links link
                           WHERE link.plan_id=plan.id
                       ), '[]'::jsonb) AS linked_transaction_ids
                FROM portfolio_position_plans plan
                WHERE {plan_clause}
                ORDER BY plan.plan_date DESC,plan.id DESC
                """,
                *plan_args,
            )
        return {
            "ok": True,
            "symbol": normalized_symbol,
            "transactions": _json_safe([dict(row) for row in transactions]),
            "allocations": _json_safe([dict(row) for row in allocations]),
            "plans": _json_safe([dict(row) for row in plans]),
        }

    async def void_latest_trade(
        self,
        transaction_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """Void only the latest active trade in its scope, preserving history."""
        await self._ensure_schema()
        if not str(reason).strip():
            raise PortfolioLedgerError("reason is required")
        async with self.database.acquire() as connection:
            async with connection.transaction():
                target = await connection.fetchrow(
                    """
                    SELECT * FROM portfolio_transactions
                    WHERE id=$1 FOR UPDATE
                    """,
                    int(transaction_id),
                )
                if target is None:
                    raise PortfolioLedgerError(
                        f"portfolio transaction {transaction_id} was not found"
                    )
                if target["voided_at"] is not None:
                    return {
                        "ok": True,
                        "alreadyVoided": True,
                        "transaction": _json_safe(dict(target)),
                    }
                latest_id = await connection.fetchval(
                    """
                    SELECT id FROM portfolio_transactions
                    WHERE symbol=$1 AND account_type=$2 AND asset_type=$3
                      AND lot_type=$4 AND voided_at IS NULL
                    ORDER BY trade_date DESC,id DESC LIMIT 1
                    """,
                    target["symbol"], target["account_type"],
                    target["asset_type"], target["lot_type"],
                )
                if int(latest_id) != int(transaction_id):
                    raise PortfolioLedgerError(
                        "only the latest active trade in a position may be voided; "
                        "record a correcting trade or rebuild the affected history"
                    )
                if target["side"] == "BUY":
                    allocated = int(await connection.fetchval(
                        """
                        SELECT COALESCE(SUM(quantity),0)
                        FROM portfolio_lot_allocations
                        WHERE buy_transaction_id=$1
                        """,
                        int(transaction_id),
                    ) or 0)
                    if allocated:
                        raise PortfolioLedgerError(
                            "this buy lot has sell allocations and cannot be voided safely"
                        )
                else:
                    await connection.execute(
                        """
                        DELETE FROM portfolio_lot_allocations
                        WHERE sell_transaction_id=$1
                        """,
                        int(transaction_id),
                    )
                updated = await connection.fetchrow(
                    """
                    UPDATE portfolio_transactions
                    SET voided_at=NOW(),void_reason=$2
                    WHERE id=$1 RETURNING *
                    """,
                    int(transaction_id), str(reason).strip(),
                )
        return {
            "ok": True,
            "alreadyVoided": False,
            "transaction": _json_safe(dict(updated)),
            "auditTrailPreserved": True,
        }


portfolio_ledger = PortfolioLedger()
