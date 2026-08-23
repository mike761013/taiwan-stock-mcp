"""Radar-signal performance updater and date-scoped weekly reports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .connection import stock_database


_HORIZONS = (
    ("d1", "return_d1"),
    ("d3", "return_d3"),
    ("d5", "return_d5"),
    ("d10", "return_d10"),
    ("d20", "return_d20"),
)
_ALLOWED_REPORT_VERSIONS = {"V12", "V11", "ALL"}
_V12_ONLY_STRATEGY_ALIASES = {
    "reversal_reclaim": "v12_reversal_reclaim",
}
DEFAULT_PERFORMANCE_UPDATE_LIMIT = 5000
MAX_PERFORMANCE_UPDATE_LIMIT = 20000

_EXECUTION_TERMINAL_STATUSES = {"NO_TRADE", "CANCELLED", "EXITED"}
_EXECUTION_FILLED_STATUSES = {
    "FILLED",
    "FILLED_PENDING_EXIT",
    "EXITED",
}

_EXECUTION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS signal_execution_performance (
    radar_run_id BIGINT NOT NULL,
    symbol VARCHAR(16) NOT NULL,
    strategy VARCHAR(32) NOT NULL,
    signal_date DATE NOT NULL,
    execution_status VARCHAR(24) NOT NULL,
    status_reason TEXT,
    aggressive_fill_date DATE,
    aggressive_fill_price NUMERIC(14,4),
    aggressive_fill_percent NUMERIC(8,4),
    confirmation_fill_date DATE,
    confirmation_fill_price NUMERIC(14,4),
    confirmation_fill_percent NUMERIC(8,4),
    entry_date DATE,
    weighted_entry_price NUMERIC(14,4),
    filled_position_percent NUMERIC(8,4),
    exit_date DATE,
    exit_price NUMERIC(14,4),
    exit_reason VARCHAR(32),
    return_d1 NUMERIC(10,4),
    return_d3 NUMERIC(10,4),
    return_d5 NUMERIC(10,4),
    return_d10 NUMERIC(10,4),
    return_d20 NUMERIC(10,4),
    max_favorable_percent NUMERIC(10,4),
    max_adverse_percent NUMERIC(10,4),
    evaluated_through DATE,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (radar_run_id, symbol),
    FOREIGN KEY (radar_run_id, symbol)
        REFERENCES radar_candidates(radar_run_id, symbol) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_signal_execution_strategy
    ON signal_execution_performance(strategy, execution_status, signal_date);
ALTER TABLE signal_execution_performance
    ADD COLUMN IF NOT EXISTS label_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS accuracy_engine VARCHAR(64),
    ADD COLUMN IF NOT EXISTS action_code VARCHAR(40),
    ADD COLUMN IF NOT EXISTS market_regime VARCHAR(24),
    ADD COLUMN IF NOT EXISTS industry VARCHAR(80),
    ADD COLUMN IF NOT EXISTS factor_confidence NUMERIC(10,4);
"""


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _parse_date(value: str | date | None, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須使用 YYYY-MM-DD 格式") from exc


def _normalise_version(version: str | None) -> str:
    normalised = str(version or "V12").strip().upper()
    if normalised not in _ALLOWED_REPORT_VERSIONS:
        allowed = ", ".join(sorted(_ALLOWED_REPORT_VERSIONS))
        raise ValueError(f"version 必須是 {allowed} 其中之一")
    return normalised


def _version_where(version: str, alias: str = "r") -> str:
    """Return a safe SQL fragment for a whitelisted report version."""
    if version == "V12":
        return (
            f"(LEFT(LOWER({alias}.strategy), 4) = 'v12_' "
            f"OR LOWER(COALESCE({alias}.configuration->>'engine', '')) "
            "= 'postgres-v12')"
        )
    if version == "V11":
        return (
            f"(LEFT(LOWER({alias}.strategy), 4) <> 'v12_' "
            f"AND LOWER(COALESCE({alias}.configuration->>'engine', '')) "
            "<> 'postgres-v12')"
        )
    return "TRUE"


def _normalise_strategy(strategy: Any) -> str:
    value = str(strategy or "unknown").strip().lower()
    if value.startswith("v12_"):
        return value[4:]
    return value


def _row_score(row: Mapping[str, Any]) -> tuple[float, int]:
    score = _as_float(row.get("total_score"))
    run_id = int(row.get("radar_run_id") or 0)
    return (score if score is not None else float("-inf"), run_id)


def _prefer_row(
    current: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> Mapping[str, Any]:
    if current is None or _row_score(candidate) > _row_score(current):
        return candidate
    return current


def _metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"signals": len(rows)}
    for label, field in _HORIZONS:
        values = [
            value
            for value in (_as_float(row.get(field)) for row in rows)
            if value is not None
        ]
        result[label] = {
            "samples": len(values),
            "pending": len(rows) - len(values),
            "averagePercent": (
                round(sum(values) / len(values), 4) if values else None
            ),
            "winRatePercent": (
                round(sum(value > 0 for value in values) / len(values) * 100, 2)
                if values else None
            ),
            "bestPercent": round(max(values), 4) if values else None,
            "worstPercent": round(min(values), 4) if values else None,
        }

    mfe_values = [
        value
        for value in (
            _as_float(row.get("max_favorable_percent")) for row in rows
        )
        if value is not None
    ]
    mae_values = [
        value
        for value in (
            _as_float(row.get("max_adverse_percent")) for row in rows
        )
        if value is not None
    ]
    result["availableWindow"] = {
        "mfeSamples": len(mfe_values),
        "averageMfePercent": (
            round(sum(mfe_values) / len(mfe_values), 4)
            if mfe_values else None
        ),
        "maeSamples": len(mae_values),
        "averageMaePercent": (
            round(sum(mae_values) / len(mae_values), 4)
            if mae_values else None
        ),
    }
    return result


def _ranking_item(
    row: Mapping[str, Any],
    horizon_field: str,
) -> dict[str, Any]:
    return {
        "runDate": _as_iso_date(row.get("run_date")),
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or ""),
        "strategies": list(row.get("strategies") or []),
        "score": _as_float(row.get("total_score")),
        "entryDate": _as_iso_date(row.get("entry_date")),
        "entryClose": _as_float(row.get("entry_close")),
        "returnPercent": _as_float(row.get(horizon_field)),
        "availableWindowMfePercent": _as_float(
            row.get("max_favorable_percent")
        ),
        "availableWindowMaePercent": _as_float(
            row.get("max_adverse_percent")
        ),
    }


def build_weekly_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    version: str,
    start_date: date,
    end_date: date,
    top_n: int = 10,
    latest_market_date: date | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe report from database rows.

    The overall section keeps one signal per run-date and symbol.  Repeated
    executions of the same strategy and cross-strategy appearances therefore
    do not inflate the weekly hit rate.  Strategy sections separately retain
    one signal per run-date, symbol and strategy.
    """
    materialised = [dict(row) for row in rows]
    top_n = max(1, min(int(top_n), 20))

    strategy_dedup: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    strategies_by_signal: dict[tuple[str, str], set[str]] = defaultdict(set)
    run_ids: set[int] = set()
    run_dates: set[str] = set()

    for row in materialised:
        run_id = int(row.get("radar_run_id") or 0)
        if run_id:
            run_ids.add(run_id)
        run_date = _as_iso_date(row.get("run_date")) or ""
        if run_date:
            run_dates.add(run_date)
        symbol = str(row.get("symbol") or "").strip()
        strategy = _normalise_strategy(row.get("strategy"))
        if not run_date or not symbol:
            continue
        row["normalised_strategy"] = strategy
        key = (run_date, symbol, strategy)
        strategy_dedup[key] = _prefer_row(strategy_dedup.get(key), row)
        strategies_by_signal[(run_date, symbol)].add(strategy)

    overall_dedup: dict[tuple[str, str], Mapping[str, Any]] = {}
    strategy_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for (run_date, symbol, strategy), row in strategy_dedup.items():
        strategy_groups[strategy].append(row)
        overall_key = (run_date, symbol)
        overall_dedup[overall_key] = _prefer_row(
            overall_dedup.get(overall_key), row
        )

    unique_rows: list[dict[str, Any]] = []
    for key, selected in overall_dedup.items():
        item = dict(selected)
        item["strategies"] = sorted(strategies_by_signal[key])
        unique_rows.append(item)
    unique_rows.sort(
        key=lambda row: (
            _as_iso_date(row.get("run_date")) or "",
            str(row.get("symbol") or ""),
        )
    )

    by_strategy = []
    for strategy in sorted(strategy_groups):
        group = list(strategy_groups[strategy])
        by_strategy.append({
            "strategy": strategy,
            **_metric_summary(group),
        })

    by_date = []
    date_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in unique_rows:
        run_date = _as_iso_date(row.get("run_date")) or ""
        date_groups[run_date].append(row)
    for run_date in sorted(date_groups):
        by_date.append({
            "runDate": run_date,
            **_metric_summary(date_groups[run_date]),
        })

    best: dict[str, list[dict[str, Any]]] = {}
    worst: dict[str, list[dict[str, Any]]] = {}
    for label, field in _HORIZONS:
        matured = [
            row for row in unique_rows if _as_float(row.get(field)) is not None
        ]
        descending = sorted(
            matured,
            key=lambda row: _as_float(row.get(field)) or 0.0,
            reverse=True,
        )
        ascending = list(reversed(descending))
        best[label] = [
            _ranking_item(row, field) for row in descending[:top_n]
        ]
        worst[label] = [
            _ranking_item(row, field) for row in ascending[:top_n]
        ]

    return {
        "ok": True,
        "version": version,
        "dateRange": {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        },
        "latestMarketDate": _as_iso_date(latest_market_date),
        "radarRuns": len(run_ids),
        "runDates": sorted(run_dates),
        "rawSignals": len(materialised),
        "strategySignalsAfterDedup": len(strategy_dedup),
        "uniqueSignals": len(unique_rows),
        "duplicatesRemoved": len(materialised) - len(unique_rows),
        "overall": _metric_summary(unique_rows),
        "byStrategy": by_strategy,
        "byDate": by_date,
        "best": best,
        "worst": worst,
        "notes": [
            "報酬以雷達當日收盤價為訊號基準，不等於使用者實際成交損益。",
            "各期間勝率只使用已具備該期間報酬的成熟樣本；空值不列為失敗。",
            "同日同股跨策略或重複執行雷達時，整體統計只保留一筆。",
            "MFE與MAE是資料庫目前可取得期間，不保證每筆都已滿20個交易日。",
        ],
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _bar_number(row: Mapping[str, Any], key: str) -> float | None:
    value = _as_float(row.get(key))
    if value is None or not math.isfinite(value):
        return None
    return value


def _execution_return(
    fills: Sequence[Mapping[str, Any]],
    price: float,
    maximum_fill_index: int,
) -> float | None:
    active = [
        fill for fill in fills if int(fill["index"]) <= maximum_fill_index
    ]
    if not active:
        return None
    cost = sum(float(fill["percent"]) for fill in active)
    shares = sum(
        float(fill["percent"]) / float(fill["price"])
        for fill in active
        if float(fill["price"]) > 0
    )
    if cost <= 0 or shares <= 0:
        return None
    return round((shares * price / cost - 1) * 100, 4)


def simulate_signal_execution(
    snapshot: Mapping[str, Any],
    bars: Sequence[Mapping[str, Any]],
    *,
    entry_window_sessions: int = 3,
) -> dict[str, Any]:
    """Conservatively simulate the published dual-entry plan.

    Bars must begin after the signal date.  No same-day high/low is used to
    manufacture a fill.  A closing failure cancels an unfilled signal; after a
    fill it exits at the following session's open, matching the published
    close-confirmation rule.
    """
    plan = _mapping(snapshot.get("tradingPlan"))
    action_code = str(
        snapshot.get("actionCode")
        or plan.get("statusCode")
        or ""
    ).strip().upper()
    aggressive = _mapping(plan.get("aggressiveEntry"))
    confirmation = _mapping(plan.get("confirmationEntry"))
    position_plan = _mapping(plan.get("positionPlan"))
    failure = _mapping(plan.get("failureCondition"))

    ordered = sorted(
        (dict(bar) for bar in bars),
        key=lambda row: str(row.get("trade_date") or ""),
    )
    result: dict[str, Any] = {
        "execution_status": "PENDING",
        "status_reason": "等待後續交易日",
        "aggressive_fill_date": None,
        "aggressive_fill_price": None,
        "aggressive_fill_percent": 0.0,
        "confirmation_fill_date": None,
        "confirmation_fill_price": None,
        "confirmation_fill_percent": 0.0,
        "entry_date": None,
        "weighted_entry_price": None,
        "filled_position_percent": 0.0,
        "exit_date": None,
        "exit_price": None,
        "exit_reason": None,
        "return_d1": None,
        "return_d3": None,
        "return_d5": None,
        "return_d10": None,
        "return_d20": None,
        "max_favorable_percent": None,
        "max_adverse_percent": None,
        "evaluated_through": (
            ordered[-1].get("trade_date") if ordered else None
        ),
    }

    if not plan or not aggressive or not confirmation or not failure:
        result.update(
            execution_status="NO_TRADE",
            status_reason="舊訊號缺少V12.1雙買點快照",
        )
        return result
    if not ordered:
        return result

    entry_window_sessions = max(1, int(entry_window_sessions))
    aggressive_low = _as_float(aggressive.get("entryLow")) or 0.0
    aggressive_high = _as_float(aggressive.get("entryHigh")) or 0.0
    aggressive_pct = _as_float(
        aggressive.get("positionPercent")
    )
    if aggressive_pct is None:
        aggressive_pct = _as_float(
            position_plan.get("aggressiveEntryPercent")
        ) or 0.0
    confirmation_price = _as_float(confirmation.get("price")) or 0.0
    confirmation_pct = _as_float(
        confirmation.get("positionPercent")
    )
    if confirmation_pct is None:
        confirmation_pct = _as_float(
            position_plan.get("confirmationEntryPercent")
        ) or 0.0
    confirmation_available = bool(
        confirmation.get("availableBelowNoChase")
    )
    failure_price = _as_float(failure.get("price")) or 0.0
    no_chase = _as_float(plan.get("noChasePrice")) or float("inf")

    fills: list[dict[str, Any]] = []
    failure_close_index: int | None = None

    for index, bar in enumerate(ordered):
        open_price = _bar_number(bar, "open")
        high = _bar_number(bar, "high")
        low = _bar_number(bar, "low")
        close = _bar_number(bar, "close")
        if None in (open_price, high, low, close):
            continue
        assert open_price is not None and high is not None
        assert low is not None and close is not None

        # A close-confirmed failure invalidates a not-yet-filled setup.  This
        # conservative ordering avoids pretending we knew the intraday path.
        if not fills and failure_price > 0 and close < failure_price:
            result.update(
                execution_status="CANCELLED",
                status_reason="成交前已收盤跌破失敗條件",
            )
            return result

        if index < entry_window_sessions:
            has_aggressive = any(fill["kind"] == "aggressive" for fill in fills)
            if (
                not has_aggressive
                and aggressive_pct > 0
                and aggressive_low > 0
                and aggressive_high >= aggressive_low
                and low <= aggressive_high
                and high >= aggressive_low
            ):
                if open_price > aggressive_high:
                    fill_price = aggressive_high
                elif open_price >= aggressive_low:
                    fill_price = open_price
                else:
                    # A gap below the zone is priced at the zone floor rather
                    # than granting the backtest an unrealistically good fill.
                    fill_price = aggressive_low
                fills.append(
                    {
                        "kind": "aggressive",
                        "index": index,
                        "date": bar.get("trade_date"),
                        "price": fill_price,
                        "percent": aggressive_pct,
                    }
                )

            has_confirmation = any(
                fill["kind"] == "confirmation" for fill in fills
            )
            if (
                not has_confirmation
                and confirmation_pct > 0
                and confirmation_available
                and confirmation_price > 0
                and confirmation_price <= no_chase
                and high >= confirmation_price
                and close >= confirmation_price
                and open_price <= no_chase
            ):
                fill_price = max(open_price, confirmation_price)
                if fill_price <= no_chase:
                    fills.append(
                        {
                            "kind": "confirmation",
                            "index": index,
                            "date": bar.get("trade_date"),
                            "price": fill_price,
                            "percent": confirmation_pct,
                        }
                    )

        if fills and failure_price > 0 and close < failure_price:
            failure_close_index = index
            break

    if not fills:
        if len(ordered) >= entry_window_sessions:
            result.update(
                execution_status="NO_TRADE",
                status_reason=f"{entry_window_sessions}個交易日內未觸及有效買點",
            )
        return result

    aggressive_fill = next(
        (fill for fill in fills if fill["kind"] == "aggressive"), None
    )
    confirmation_fill = next(
        (fill for fill in fills if fill["kind"] == "confirmation"), None
    )
    first_index = min(int(fill["index"]) for fill in fills)
    cost = sum(float(fill["percent"]) for fill in fills)
    shares = sum(
        float(fill["percent"]) / float(fill["price"]) for fill in fills
    )
    weighted_entry = cost / shares if shares > 0 else None

    exit_index: int | None = None
    exit_price: float | None = None
    exit_date: Any = None
    status = "FILLED"
    reason = "已依V12.1交易劇本成交"
    if failure_close_index is not None:
        next_index = failure_close_index + 1
        if next_index < len(ordered):
            next_open = _bar_number(ordered[next_index], "open")
            if next_open is not None:
                exit_index = next_index
                exit_price = next_open
                exit_date = ordered[next_index].get("trade_date")
                status = "EXITED"
                reason = "收盤跌破失敗條件，隔日開盤退出"
        else:
            status = "FILLED_PENDING_EXIT"
            reason = "已收盤跌破失敗條件，等待下一交易日開盤價"

    result.update(
        execution_status=status,
        status_reason=reason,
        aggressive_fill_date=(
            aggressive_fill.get("date") if aggressive_fill else None
        ),
        aggressive_fill_price=(
            round(float(aggressive_fill["price"]), 4)
            if aggressive_fill else None
        ),
        aggressive_fill_percent=(
            float(aggressive_fill["percent"]) if aggressive_fill else 0.0
        ),
        confirmation_fill_date=(
            confirmation_fill.get("date") if confirmation_fill else None
        ),
        confirmation_fill_price=(
            round(float(confirmation_fill["price"]), 4)
            if confirmation_fill else None
        ),
        confirmation_fill_percent=(
            float(confirmation_fill["percent"])
            if confirmation_fill else 0.0
        ),
        entry_date=ordered[first_index].get("trade_date"),
        weighted_entry_price=(
            round(weighted_entry, 4) if weighted_entry is not None else None
        ),
        filled_position_percent=cost,
        exit_date=exit_date,
        exit_price=round(exit_price, 4) if exit_price is not None else None,
        exit_reason=("CLOSE_FAILURE" if exit_index is not None else None),
    )

    horizon_fields = {
        1: "return_d1",
        3: "return_d3",
        5: "return_d5",
        10: "return_d10",
        20: "return_d20",
    }
    for horizon, field in horizon_fields.items():
        target_index = first_index + horizon
        if exit_index is not None and exit_index <= target_index:
            result[field] = _execution_return(fills, exit_price, exit_index)
        elif target_index < len(ordered):
            target_close = _bar_number(ordered[target_index], "close")
            if target_close is not None:
                result[field] = _execution_return(
                    fills, target_close, target_index
                )

    window_end = exit_index if exit_index is not None else len(ordered) - 1
    favorable: list[float] = []
    adverse: list[float] = []
    for index in range(first_index, window_end + 1):
        high = _bar_number(ordered[index], "high")
        low = _bar_number(ordered[index], "low")
        if high is not None:
            value = _execution_return(fills, high, index)
            if value is not None:
                favorable.append(value)
        if low is not None:
            value = _execution_return(fills, low, index)
            if value is not None:
                adverse.append(value)
    if exit_index is not None and exit_price is not None:
        exit_return = _execution_return(fills, exit_price, exit_index)
        if exit_return is not None:
            favorable.append(exit_return)
            adverse.append(exit_return)
    result["max_favorable_percent"] = max(favorable) if favorable else None
    result["max_adverse_percent"] = min(adverse) if adverse else None
    return result


async def _ensure_execution_schema(connection: Any) -> None:
    await connection.execute(_EXECUTION_TABLE_SQL)


async def update_signal_execution_performance(
    limit: int = DEFAULT_PERFORMANCE_UPDATE_LIMIT,
    entry_window_sessions: int = 3,
) -> dict[str, Any]:
    """Update V12 execution-aware results in one batched market-data read."""
    limit = max(1, min(int(limit), MAX_PERFORMANCE_UPDATE_LIMIT))
    entry_window_sessions = max(1, min(int(entry_window_sessions), 10))
    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        signals = await connection.fetch(
            """
            SELECT c.radar_run_id, c.symbol, c.snapshot,
                   COALESCE(NULLIF(c.snapshot->>'strategy',''), r.strategy)
                       AS strategy,
                   r.run_date
            FROM radar_candidates c
            JOIN radar_runs r ON r.id=c.radar_run_id
            LEFT JOIN signal_execution_performance e
              ON e.radar_run_id=c.radar_run_id AND e.symbol=c.symbol
            WHERE LOWER(r.strategy) LIKE 'v12_%'
              AND (
                COALESCE(c.snapshot->>'accuracyEngine','') <> 'V12.3_SEVEN_FACTOR'
                OR LOWER(r.strategy) = 'v12_combined'
              )
              AND (
                e.radar_run_id IS NULL
                OR e.execution_status IN (
                    'PENDING', 'FILLED', 'FILLED_PENDING_EXIT'
                )
              )
              AND (e.return_d20 IS NULL OR e.execution_status='PENDING')
            ORDER BY
              CASE WHEN e.radar_run_id IS NULL THEN 0 ELSE 1 END,
              r.run_date DESC,
              c.radar_run_id DESC,
              c.symbol ASC
            LIMIT $1
            """,
            limit,
        )
        if not signals:
            return {
                "ok": True,
                "processed": 0,
                "selected": 0,
                "limit": limit,
                "entryWindowSessions": entry_window_sessions,
            }

        symbols = sorted({str(row["symbol"]) for row in signals})
        minimum_date = min(row["run_date"] for row in signals)
        all_bars = await connection.fetch(
            """
            SELECT symbol, trade_date, open, high, low, close
            FROM daily_bars
            WHERE symbol=ANY($1::varchar[])
              AND trade_date > $2
            ORDER BY symbol, trade_date
            """,
            symbols,
            minimum_date,
        )
        bars_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for bar in all_bars:
            bars_by_symbol[str(bar["symbol"])].append(dict(bar))

        records: list[tuple[Any, ...]] = []
        status_counts: dict[str, int] = defaultdict(int)
        for signal in signals:
            snapshot = _mapping(signal["snapshot"])
            signal_date = signal["run_date"]
            relevant_bars = [
                bar
                for bar in bars_by_symbol.get(str(signal["symbol"]), [])
                if bar.get("trade_date") is not None
                and bar["trade_date"] > signal_date
            ][:25]
            simulation = simulate_signal_execution(
                snapshot,
                relevant_bars,
                entry_window_sessions=entry_window_sessions,
            )
            status_counts[str(simulation["execution_status"])] += 1
            records.append(
                (
                    signal["radar_run_id"],
                    signal["symbol"],
                    signal["strategy"],
                    signal_date,
                    simulation["execution_status"],
                    simulation["status_reason"],
                    simulation["aggressive_fill_date"],
                    simulation["aggressive_fill_price"],
                    simulation["aggressive_fill_percent"],
                    simulation["confirmation_fill_date"],
                    simulation["confirmation_fill_price"],
                    simulation["confirmation_fill_percent"],
                    simulation["entry_date"],
                    simulation["weighted_entry_price"],
                    simulation["filled_position_percent"],
                    simulation["exit_date"],
                    simulation["exit_price"],
                    simulation["exit_reason"],
                    simulation["return_d1"],
                    simulation["return_d3"],
                    simulation["return_d5"],
                    simulation["return_d10"],
                    simulation["return_d20"],
                    simulation["max_favorable_percent"],
                    simulation["max_adverse_percent"],
                    simulation["evaluated_through"],
                    "V12.3_DUAL_ENTRY_20D",
                    str(snapshot.get("accuracyEngine") or ""),
                    str(snapshot.get("actionCode") or ""),
                    str(_mapping(snapshot.get("marketContext")).get("regime") or ""),
                    str(snapshot.get("industry") or ""),
                    _as_float(snapshot.get("dataConfidence")),
                )
            )

        await connection.executemany(
            """
            INSERT INTO signal_execution_performance(
                radar_run_id, symbol, strategy, signal_date,
                execution_status, status_reason,
                aggressive_fill_date, aggressive_fill_price,
                aggressive_fill_percent,
                confirmation_fill_date, confirmation_fill_price,
                confirmation_fill_percent,
                entry_date, weighted_entry_price, filled_position_percent,
                exit_date, exit_price, exit_reason,
                return_d1, return_d3, return_d5, return_d10, return_d20,
                max_favorable_percent, max_adverse_percent,
                evaluated_through, label_version, accuracy_engine,
                action_code, market_regime, industry, factor_confidence,
                calculated_at
            ) VALUES(
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                $16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,
                $29,$30,$31,$32,NOW()
            )
            ON CONFLICT(radar_run_id, symbol) DO UPDATE SET
                execution_status=EXCLUDED.execution_status,
                status_reason=EXCLUDED.status_reason,
                aggressive_fill_date=EXCLUDED.aggressive_fill_date,
                aggressive_fill_price=EXCLUDED.aggressive_fill_price,
                aggressive_fill_percent=EXCLUDED.aggressive_fill_percent,
                confirmation_fill_date=EXCLUDED.confirmation_fill_date,
                confirmation_fill_price=EXCLUDED.confirmation_fill_price,
                confirmation_fill_percent=EXCLUDED.confirmation_fill_percent,
                entry_date=EXCLUDED.entry_date,
                weighted_entry_price=EXCLUDED.weighted_entry_price,
                filled_position_percent=EXCLUDED.filled_position_percent,
                exit_date=EXCLUDED.exit_date,
                exit_price=EXCLUDED.exit_price,
                exit_reason=EXCLUDED.exit_reason,
                return_d1=EXCLUDED.return_d1,
                return_d3=EXCLUDED.return_d3,
                return_d5=EXCLUDED.return_d5,
                return_d10=EXCLUDED.return_d10,
                return_d20=EXCLUDED.return_d20,
                max_favorable_percent=EXCLUDED.max_favorable_percent,
                max_adverse_percent=EXCLUDED.max_adverse_percent,
                evaluated_through=EXCLUDED.evaluated_through,
                label_version=EXCLUDED.label_version,
                accuracy_engine=EXCLUDED.accuracy_engine,
                action_code=EXCLUDED.action_code,
                market_regime=EXCLUDED.market_regime,
                industry=EXCLUDED.industry,
                factor_confidence=EXCLUDED.factor_confidence,
                calculated_at=NOW()
            """,
            records,
        )
    return {
        "ok": True,
        "processed": len(records),
        "selected": len(signals),
        "limit": limit,
        "entryWindowSessions": entry_window_sessions,
        "statusCounts": dict(status_counts),
        "batchedBarRead": True,
    }


async def execution_performance_summary(
    strategy: str | None = None,
    accuracy_engine: str | None = None,
) -> dict[str, Any]:
    """Return performance only for plans that would really have filled."""
    requested = str(strategy or "").strip().lower()
    resolved = requested
    if resolved and not resolved.startswith("v12_"):
        resolved = f"v12_{resolved}"
    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        conditions: list[str] = []
        args: list[Any] = []
        if resolved:
            args.append(resolved)
            conditions.append(f"LOWER(e.strategy)=${len(args)}")
        requested_engine = str(accuracy_engine or "").strip()
        if requested_engine:
            args.append(requested_engine)
            conditions.append(
                f"COALESCE(c.snapshot->>'accuracyEngine', '')=${len(args)}"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        distinct_keys = (
            "e.signal_date, e.symbol, e.strategy"
            if resolved else "e.signal_date, e.symbol"
        )
        row = await connection.fetchrow(
            f"""
            WITH dedup AS (
              SELECT DISTINCT ON ({distinct_keys}) e.*
              FROM signal_execution_performance e
              JOIN radar_candidates c
                ON c.radar_run_id=e.radar_run_id AND c.symbol=e.symbol
              {where}
              ORDER BY {distinct_keys},
                       c.total_score DESC NULLS LAST,
                       e.radar_run_id DESC
            )
            SELECT COUNT(*) AS evaluated_signals,
              COUNT(*) FILTER (WHERE execution_status='PENDING') AS pending,
              COUNT(*) FILTER (WHERE execution_status='NO_TRADE') AS no_trade,
              COUNT(*) FILTER (WHERE execution_status='CANCELLED') AS cancelled,
              COUNT(*) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                )
              ) AS filled,
              COUNT(*) FILTER (WHERE execution_status='EXITED') AS stopped,
              AVG(filled_position_percent) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                )
              ) AS avg_filled_position,
              COUNT(return_d1) AS samples_d1,
              COUNT(return_d3) AS samples_d3,
              COUNT(return_d5) AS samples_d5,
              COUNT(return_d10) AS samples_d10,
              COUNT(return_d20) AS samples_d20,
              AVG(return_d1) AS avg_d1,
              AVG(return_d3) AS avg_d3,
              AVG(return_d5) AS avg_d5,
              AVG(return_d10) AS avg_d10,
              AVG(return_d20) AS avg_d20,
              AVG(CASE WHEN return_d5 IS NULL THEN NULL
                       WHEN return_d5 > 0 THEN 1.0 ELSE 0.0 END)*100
                AS win_d5,
              AVG(max_favorable_percent) AS avg_mfe,
              AVG(max_adverse_percent) AS avg_mae
            FROM dedup
            """,
            *args,
        )
    summary = dict(row)
    matured = (
        int(summary.get("evaluated_signals") or 0)
        - int(summary.get("pending") or 0)
    )
    filled = int(summary.get("filled") or 0)
    summary["entry_rate_percent"] = (
        round(filled / matured * 100, 2) if matured > 0 else None
    )
    return {
        "ok": True,
        "strategy": requested or None,
        "resolvedStrategy": resolved or None,
        "accuracyEngine": requested_engine or None,
        "summary": summary,
        "method": "V12.1_DUAL_ENTRY_EXECUTION",
        "notes": [
            "只有實際觸及激進低接或確認買點的訊號才計算勝率。",
            "不追價、未觸價與成交前失效不列為虧損交易。",
            "收盤確認失敗後，以次一交易日開盤價退出。",
        ],
    }


async def execution_strategy_priors(
    minimum_samples: int = 30,
    full_confidence_samples: int = 120,
    maximum_adjustment: float = 8.0,
    accuracy_engine: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build small confidence-weighted priors from matured executable trades."""
    minimum_samples = max(10, int(minimum_samples))
    full_confidence_samples = max(minimum_samples, int(full_confidence_samples))
    maximum_adjustment = max(0.0, min(float(maximum_adjustment), 15.0))
    requested_engine = str(accuracy_engine or "").strip()
    engine_where = (
        "WHERE COALESCE(c.snapshot->>'accuracyEngine', '')=$1"
        if requested_engine
        else ""
    )
    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        rows = await connection.fetch(
            f"""
            WITH dedup AS (
              SELECT DISTINCT ON (e.signal_date, e.symbol, e.strategy) e.*
              FROM signal_execution_performance e
              JOIN radar_candidates c
                ON c.radar_run_id=e.radar_run_id AND c.symbol=e.symbol
              {engine_where}
              ORDER BY e.signal_date, e.symbol, e.strategy,
                       c.total_score DESC NULLS LAST,
                       e.radar_run_id DESC
            )
            SELECT LOWER(strategy) AS strategy,
                   COUNT(return_d5) AS samples_d5,
                   COUNT(return_d10) AS samples_d10,
                   AVG(return_d5) AS avg_d5,
                   AVG(return_d10) AS avg_d10,
                   AVG(CASE WHEN return_d5 IS NULL THEN NULL
                            WHEN return_d5 > 0 THEN 1.0 ELSE 0.0 END)*100
                     AS win_d5
            FROM dedup
            WHERE execution_status IN ('FILLED','EXITED')
            GROUP BY LOWER(strategy)
            """,
            *([requested_engine] if requested_engine else []),
        )

    priors: dict[str, dict[str, Any]] = {}
    for row in rows:
        samples5 = int(row["samples_d5"] or 0)
        samples10 = int(row["samples_d10"] or 0)
        samples = max(samples5, samples10)
        averages = [
            value
            for value in (
                _as_float(row["avg_d5"]),
                _as_float(row["avg_d10"]),
            )
            if value is not None
        ]
        win5 = _as_float(row["win_d5"])
        confidence = (
            min(1.0, samples / full_confidence_samples)
            if samples >= minimum_samples else 0.0
        )
        expected = sum(averages) / len(averages) if averages else 0.0
        win_component = ((win5 or 50.0) - 50.0) * 0.08
        raw_adjustment = expected + win_component
        adjustment = max(
            -maximum_adjustment,
            min(raw_adjustment * confidence, maximum_adjustment),
        )
        strategy = str(row["strategy"] or "")
        if strategy.startswith("v12_"):
            strategy = strategy[4:]
        priors[strategy] = {
            "samplesD5": samples5,
            "samplesD10": samples10,
            "averageD5Percent": _as_float(row["avg_d5"]),
            "averageD10Percent": _as_float(row["avg_d10"]),
            "winRateD5Percent": win5,
            "confidence": round(confidence, 4),
            "adjustment": round(adjustment, 4),
            "active": confidence > 0,
            "accuracyEngine": requested_engine or None,
        }
    return priors


async def update_signal_performance(
    limit: int = DEFAULT_PERFORMANCE_UPDATE_LIMIT,
) -> dict[str, Any]:
    """Update radar returns without starving newly recorded signals.

    Pending D20 rows remain eligible for several weeks.  Ordering only by the
    oldest run date caused those rows to consume the old 500-row limit every
    day, so newer signals were never calculated.  Never-processed signals are
    now selected first, followed by the stalest calculated rows, and the daily
    default is large enough to cover the active 20-session window.
    """
    limit = max(1, min(int(limit), MAX_PERFORMANCE_UPDATE_LIMIT))
    async with stock_database.acquire() as connection:
        signals = await connection.fetch("""
            SELECT c.radar_run_id, c.symbol, r.run_date
            FROM radar_candidates c
            JOIN radar_runs r ON r.id=c.radar_run_id
            LEFT JOIN signal_performance p
              ON p.radar_run_id=c.radar_run_id AND p.symbol=c.symbol
            WHERE p.radar_run_id IS NULL OR p.return_d20 IS NULL
            ORDER BY
              CASE WHEN p.radar_run_id IS NULL THEN 0 ELSE 1 END,
              p.calculated_at ASC NULLS FIRST,
              r.run_date DESC,
              c.radar_run_id DESC,
              c.symbol ASC
            LIMIT $1
        """, limit)
        processed = 0
        for signal in signals:
            bars = await connection.fetch("""
                SELECT trade_date, close, high, low
                FROM daily_bars
                WHERE symbol=$1 AND trade_date >= $2
                ORDER BY trade_date ASC
                LIMIT 21
            """, signal["symbol"], signal["run_date"])
            if not bars:
                continue
            entry = float(bars[0]["close"])

            def ret(index: int):
                if len(bars) <= index or not entry:
                    return None
                return round(
                    (float(bars[index]["close"]) / entry - 1) * 100,
                    4,
                )

            highs = [
                float(row["high"])
                for row in bars
                if row["high"] is not None
            ]
            lows = [
                float(row["low"])
                for row in bars
                if row["low"] is not None
            ]
            await connection.execute("""
                INSERT INTO signal_performance(
                    radar_run_id, symbol, entry_date, entry_close,
                    return_d1, return_d3, return_d5, return_d10, return_d20,
                    max_favorable_percent, max_adverse_percent, calculated_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
                ON CONFLICT(radar_run_id, symbol) DO UPDATE SET
                    return_d1=EXCLUDED.return_d1,
                    return_d3=EXCLUDED.return_d3,
                    return_d5=EXCLUDED.return_d5,
                    return_d10=EXCLUDED.return_d10,
                    return_d20=EXCLUDED.return_d20,
                    max_favorable_percent=EXCLUDED.max_favorable_percent,
                    max_adverse_percent=EXCLUDED.max_adverse_percent,
                    calculated_at=NOW()
            """, signal["radar_run_id"], signal["symbol"],
                bars[0]["trade_date"], entry, ret(1), ret(3), ret(5),
                ret(10), ret(20),
                round((max(highs) / entry - 1) * 100, 4) if highs else None,
                round((min(lows) / entry - 1) * 100, 4) if lows else None)
            processed += 1
    return {
        "ok": True,
        "processed": processed,
        "selected": len(signals),
        "limit": limit,
        "newSignalsPrioritised": True,
    }


def _resolve_summary_strategy(strategy: str | None) -> str | None:
    """Resolve public strategy names to their persisted database names.

    reversal_reclaim exists only in V12, where radar runs are stored with
    the v12_ prefix. Keep legacy strategy names unchanged so existing V11
    summary queries remain backward compatible.
    """
    if strategy is None:
        return None
    normalised = str(strategy).strip().lower()
    if not normalised:
        return None
    return _V12_ONLY_STRATEGY_ALIASES.get(normalised, normalised)


async def performance_summary(strategy: str | None = None) -> dict[str, Any]:
    """Return cumulative performance without counting pending D5 rows as losses."""
    requested_strategy = (
        str(strategy).strip().lower() if strategy is not None else None
    )
    resolved_strategy = _resolve_summary_strategy(strategy)
    where = "WHERE LOWER(r.strategy)=$1" if resolved_strategy else ""
    args = [resolved_strategy] if resolved_strategy else []
    async with stock_database.acquire() as connection:
        row = await connection.fetchrow(f"""
            SELECT COUNT(*) AS samples,
              COUNT(p.return_d1) AS samples_d1,
              COUNT(p.return_d3) AS samples_d3,
              COUNT(p.return_d5) AS samples_d5,
              COUNT(p.return_d10) AS samples_d10,
              COUNT(p.return_d20) AS samples_d20,
              AVG(p.return_d1) AS avg_d1,
              AVG(p.return_d3) AS avg_d3,
              AVG(p.return_d5) AS avg_d5,
              AVG(p.return_d10) AS avg_d10,
              AVG(p.return_d20) AS avg_d20,
              AVG(
                CASE
                  WHEN p.return_d5 IS NULL THEN NULL
                  WHEN p.return_d5 > 0 THEN 1.0
                  ELSE 0.0
                END
              )*100 AS win_d5,
              AVG(p.max_favorable_percent) AS avg_mfe,
              AVG(p.max_adverse_percent) AS avg_mae
            FROM signal_performance p
            JOIN radar_runs r ON r.id=p.radar_run_id
            {where}
        """, *args)
    return {
        "ok": True,
        "strategy": requested_strategy,
        "resolvedStrategy": resolved_strategy,
        "summary": dict(row),
    }


async def weekly_performance_report(
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    version: str = "V12",
    top_n: int = 10,
) -> dict[str, Any]:
    """Return a date-scoped, version-filtered radar performance report."""
    normalised_version = _normalise_version(version)
    parsed_start = _parse_date(start_date, "start_date")
    parsed_end = _parse_date(end_date, "end_date")
    top_n = max(1, min(int(top_n), 20))
    version_where = _version_where(normalised_version)

    async with stock_database.acquire() as connection:
        latest_run_date = await connection.fetchval(f"""
            SELECT MAX(r.run_date)
            FROM radar_runs r
            WHERE {version_where}
        """)
        latest_market_date = await connection.fetchval(
            "SELECT MAX(trade_date) FROM daily_bars"
        )

        if latest_run_date is None:
            return {
                "ok": True,
                "version": normalised_version,
                "dateRange": None,
                "latestMarketDate": _as_iso_date(latest_market_date),
                "radarRuns": 0,
                "rawSignals": 0,
                "uniqueSignals": 0,
                "message": "指定版本尚無已保存的雷達紀錄。",
            }

        if parsed_start is None and parsed_end is None:
            parsed_end = latest_run_date
            parsed_start = parsed_end - timedelta(days=parsed_end.weekday())
        elif parsed_start is None:
            parsed_start = parsed_end - timedelta(days=parsed_end.weekday())
        elif parsed_end is None:
            parsed_end = min(
                parsed_start + timedelta(days=6),
                latest_run_date,
            )

        assert parsed_start is not None and parsed_end is not None
        if parsed_start > parsed_end:
            raise ValueError("start_date 不可晚於 end_date")
        if (parsed_end - parsed_start).days > 366:
            raise ValueError("單次週報查詢區間不可超過366天")

        run_rows = await connection.fetch(f"""
            SELECT r.id, r.run_date, r.strategy, r.candidate_count
            FROM radar_runs r
            WHERE r.run_date BETWEEN $1 AND $2
              AND {version_where}
            ORDER BY r.run_date, r.id
        """, parsed_start, parsed_end)

        rows = await connection.fetch(f"""
            SELECT
              r.id AS radar_run_id,
              r.run_date,
              r.strategy,
              c.symbol,
              s.name,
              c.rank,
              c.total_score,
              p.entry_date,
              p.entry_close,
              p.return_d1,
              p.return_d3,
              p.return_d5,
              p.return_d10,
              p.return_d20,
              p.max_favorable_percent,
              p.max_adverse_percent
            FROM radar_runs r
            JOIN radar_candidates c ON c.radar_run_id=r.id
            JOIN securities s ON s.symbol=c.symbol
            LEFT JOIN signal_performance p
              ON p.radar_run_id=c.radar_run_id AND p.symbol=c.symbol
            WHERE r.run_date BETWEEN $1 AND $2
              AND {version_where}
            ORDER BY r.run_date, c.symbol, c.total_score DESC NULLS LAST, r.id DESC
        """, parsed_start, parsed_end)

    report = build_weekly_report(
        rows,
        version=normalised_version,
        start_date=parsed_start,
        end_date=parsed_end,
        top_n=top_n,
        latest_market_date=latest_market_date,
    )
    report["radarRuns"] = len(run_rows)
    report["zeroCandidateRuns"] = sum(
        int(row["candidate_count"] or 0) == 0 for row in run_rows
    )
    report["runDates"] = sorted({
        _as_iso_date(row["run_date"])
        for row in run_rows
        if row["run_date"] is not None
    })
    return report
