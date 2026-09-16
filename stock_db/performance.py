"""Radar-signal performance updater and date-scoped weekly reports."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
import math
import os
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
_REPORT_VERSION_ALIASES = {"V12.4": "V12"}
_V12_ONLY_STRATEGY_ALIASES = {
    "reversal_reclaim": "v12_reversal_reclaim",
}
_STRATEGY_LABELS = {
    "early_stage": "多頭初升段",
    "breakout": "放量突破",
    "pullback": "多頭拉回",
    "reversal_reclaim": "底部反轉收復",
    "reversal_continuation": "反轉續強",
    "trend_support_probe": "多頭支撐試單",
    "combined": "綜合雷達",
}
_FORMAL_ACTION_CODES = frozenset({
    "BUY_ZONE",
    "BUY_ON_BREAKOUT",
    "EARLY_ENTRY",
    "EARLY_ENTRY_SMALL_POSITION",
    "PRICE_CONFIRMATION_REQUIRED",
})
_PROBE_ACTION_CODES = frozenset({"PROBE_ENTRY"})
_ACTION_TIER_LABELS = {
    "ACTIONABLE": "正式進場",
    "PROBE": "小部位試單",
    "WATCH": "等待觀察",
    "UNCLASSIFIED": "舊版未分層",
}
DEFAULT_PERFORMANCE_UPDATE_LIMIT = 5000
MAX_PERFORMANCE_UPDATE_LIMIT = 20000

# User-confirmed Cathay e-order cost factors for ordinary Taiwan stocks.
# The factors already include the discounted brokerage fee; the sell factor
# also includes transaction tax.  Slippage is kept separate so the execution
# assumption remains auditable and can be overridden without changing code.
BUY_COST_FACTOR = float(os.getenv("V12_BUY_COST_FACTOR", "1.000399"))
SELL_PROCEEDS_FACTOR = float(
    os.getenv("V12_SELL_PROCEEDS_FACTOR", "0.996601")
)
CONFIRMATION_ENTRY_SLIPPAGE_BPS = float(
    os.getenv("V12_CONFIRMATION_ENTRY_SLIPPAGE_BPS", "5")
)
EXIT_SLIPPAGE_BPS = float(os.getenv("V12_EXIT_SLIPPAGE_BPS", "5"))
WICK_TOUCH_FILL_RATIO = max(
    0.0,
    min(float(os.getenv("V12_WICK_TOUCH_FILL_RATIO", "0.5")), 1.0),
)
TAKE_PROFIT_1_R = max(
    0.25,
    float(os.getenv("V12_TAKE_PROFIT_1_R", "1.0")),
)
TAKE_PROFIT_2_R = max(
    TAKE_PROFIT_1_R,
    float(os.getenv("V12_TAKE_PROFIT_2_R", "2.0")),
)
TAKE_PROFIT_1_RATIO = max(
    0.0,
    min(float(os.getenv("V12_TAKE_PROFIT_1_RATIO", "0.5")), 1.0),
)
TAKE_PROFIT_2_RATIO = max(
    0.0,
    min(
        float(os.getenv("V12_TAKE_PROFIT_2_RATIO", "0.25")),
        1.0 - TAKE_PROFIT_1_RATIO,
    ),
)
TRAILING_DISTANCE_R = max(
    0.25,
    float(os.getenv("V12_TRAILING_DISTANCE_R", "1.0")),
)
EXECUTION_MODEL_REVISION = "V12.4-NET-EXECUTION-2"
_execution_schema_ready = False
_execution_schema_lock = asyncio.Lock()

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
    ADD COLUMN IF NOT EXISTS factor_model_revision VARCHAR(80),
    ADD COLUMN IF NOT EXISTS execution_model_revision VARCHAR(80),
    ADD COLUMN IF NOT EXISTS cost_model JSONB NOT NULL DEFAULT '{}'::JSONB,
    ADD COLUMN IF NOT EXISTS fill_assumption VARCHAR(80),
    ADD COLUMN IF NOT EXISTS planned_position_percent NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS fill_ratio_percent NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS action_code VARCHAR(40),
    ADD COLUMN IF NOT EXISTS market_regime VARCHAR(24),
    ADD COLUMN IF NOT EXISTS industry VARCHAR(80),
    ADD COLUMN IF NOT EXISTS factor_confidence NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS exit_ledger JSONB NOT NULL DEFAULT '[]'::JSONB,
    ADD COLUMN IF NOT EXISTS profit_management JSONB NOT NULL DEFAULT '{}'::JSONB;
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
    normalised = _REPORT_VERSION_ALIASES.get(normalised, normalised)
    if normalised not in _ALLOWED_REPORT_VERSIONS:
        allowed = ", ".join(
            sorted(_ALLOWED_REPORT_VERSIONS | set(_REPORT_VERSION_ALIASES))
        )
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


def _strategy_label(strategy: Any) -> str:
    normalised = _normalise_strategy(strategy)
    return _STRATEGY_LABELS.get(normalised, normalised)


def _snapshot_strategies(
    snapshot: Mapping[str, Any],
    fallback_strategy: Any = None,
) -> list[str]:
    raw = snapshot.get("strategies")
    strategies: list[str] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        strategies.extend(
            _normalise_strategy(value)
            for value in raw
            if str(value or "").strip()
        )
    primary = snapshot.get("strategy")
    if primary:
        strategies.append(_normalise_strategy(primary))
    if fallback_strategy:
        strategies.append(_normalise_strategy(fallback_strategy))
    return list(dict.fromkeys(
        value for value in strategies if value and value != "unknown"
    ))


def _action_tier(row: Mapping[str, Any]) -> str:
    snapshot = _mapping(row.get("snapshot"))
    plan = _mapping(snapshot.get("tradingPlan"))
    action_code = str(
        row.get("action_code")
        or snapshot.get("actionCode")
        or plan.get("statusCode")
        or ""
    ).strip().upper()
    forward_qualified = bool(snapshot.get("forwardQualified", True))
    strategies = _snapshot_strategies(
        snapshot,
        row.get("normalised_strategy") or row.get("strategy"),
    )
    if action_code in _FORMAL_ACTION_CODES and forward_qualified:
        return "ACTIONABLE"
    if (
        action_code in _PROBE_ACTION_CODES
        or "trend_support_probe" in strategies
    ):
        return "PROBE"
    if action_code or snapshot:
        return "WATCH"
    return "UNCLASSIFIED"


def _canonical_execution_strategy(strategy: Any) -> str:
    normalised = _normalise_strategy(strategy)
    return (
        f"v12_{normalised}"
        if normalised and normalised != "unknown"
        else "v12_combined"
    )


def _row_score(row: Mapping[str, Any]) -> tuple[float, int]:
    score = _as_float(row.get("total_score"))
    run_id = int(row.get("radar_run_id") or 0)
    return (score if score is not None else float("-inf"), run_id)


def _prefer_row(
    current: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> Mapping[str, Any]:
    tier_priority = {
        "ACTIONABLE": 3,
        "PROBE": 2,
        "WATCH": 1,
        "UNCLASSIFIED": 0,
    }
    candidate_priority = (
        tier_priority.get(str(candidate.get("action_tier") or ""), -1),
        *_row_score(candidate),
    )
    current_priority = (
        tier_priority.get(str((current or {}).get("action_tier") or ""), -1),
        *_row_score(current or {}),
    )
    if current is None or candidate_priority > current_priority:
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


def _action_tier_summaries(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("action_tier") or _action_tier(row))].append(row)
    order = ("ACTIONABLE", "PROBE", "WATCH", "UNCLASSIFIED")
    return [
        {
            "actionTier": tier,
            "actionTierLabel": _ACTION_TIER_LABELS[tier],
            **_metric_summary(grouped.get(tier, [])),
        }
        for tier in order
    ]


def _ranking_item(
    row: Mapping[str, Any],
    horizon_field: str,
) -> dict[str, Any]:
    strategies = list(row.get("strategies") or [])
    action_tier = str(row.get("action_tier") or _action_tier(row))
    return {
        "runDate": _as_iso_date(row.get("run_date")),
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or ""),
        "strategies": strategies,
        "strategyLabels": [_strategy_label(value) for value in strategies],
        "actionTier": action_tier,
        "actionTierLabel": _ACTION_TIER_LABELS.get(
            action_tier,
            action_tier,
        ),
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
        row["action_tier"] = _action_tier(row)
        key = (run_date, symbol, strategy)
        strategy_dedup[key] = _prefer_row(strategy_dedup.get(key), row)
        strategies_by_signal[(run_date, symbol)].add(strategy)

    overall_dedup: dict[tuple[str, str], Mapping[str, Any]] = {}
    combined_dedup: dict[tuple[str, str], Mapping[str, Any]] = {}
    for (run_date, symbol, strategy), row in strategy_dedup.items():
        overall_key = (run_date, symbol)
        overall_dedup[overall_key] = _prefer_row(
            overall_dedup.get(overall_key), row
        )
        if strategy == "combined":
            current = combined_dedup.get(overall_key)
            if (
                current is None
                or int(row.get("radar_run_id") or 0)
                > int(current.get("radar_run_id") or 0)
            ):
                combined_dedup[overall_key] = row

    # The combined row has passed complete-factor enrichment and is therefore
    # authoritative over the earlier per-strategy prefilter saved by the same
    # full-radar run.  Without this override a preliminary BUY_ZONE can replace
    # a final WATCH decision merely because it has the more optimistic tier.
    overall_dedup.update(combined_dedup)

    unique_rows: list[dict[str, Any]] = []
    for key, selected in overall_dedup.items():
        item = dict(selected)
        snapshot = _mapping(item.get("snapshot"))
        explicit_strategies = snapshot.get("strategies")
        if (
            isinstance(explicit_strategies, Sequence)
            and not isinstance(explicit_strategies, (str, bytes))
            and explicit_strategies
        ):
            strategies = _snapshot_strategies(snapshot)
        else:
            strategies = sorted(
                strategy
                for strategy in strategies_by_signal[key]
                if strategy != "combined"
            )
        if not strategies:
            strategies = [
                _normalise_strategy(item.get("normalised_strategy"))
            ]
        item["strategies"] = sorted({
            strategy for strategy in strategies
            if strategy and strategy not in {"combined", "unknown"}
        }) or ["combined"]
        item["has_combined_snapshot"] = key in combined_dedup
        unique_rows.append(item)
    unique_rows.sort(
        key=lambda row: (
            _as_iso_date(row.get("run_date")) or "",
            str(row.get("symbol") or ""),
        )
    )

    strategy_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in unique_rows:
        for strategy in row.get("strategies") or []:
            strategy_groups[str(strategy)].append(row)
        if row.get("has_combined_snapshot"):
            strategy_groups["combined"].append(row)

    by_strategy = []
    for strategy in sorted(strategy_groups):
        group = list(strategy_groups[strategy])
        by_strategy.append({
            "strategy": strategy,
            "strategyLabel": _strategy_label(strategy),
            **_metric_summary(group),
            "byActionTier": _action_tier_summaries(group),
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
            "byActionTier": _action_tier_summaries(date_groups[run_date]),
        })

    actionable_rows = [
        row for row in unique_rows
        if row.get("action_tier") == "ACTIONABLE"
    ]
    best: dict[str, list[dict[str, Any]]] = {}
    worst: dict[str, list[dict[str, Any]]] = {}
    all_candidate_best: dict[str, list[dict[str, Any]]] = {}
    all_candidate_worst: dict[str, list[dict[str, Any]]] = {}
    for label, field in _HORIZONS:
        matured = [
            row for row in actionable_rows
            if _as_float(row.get(field)) is not None
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
        all_matured = [
            row for row in unique_rows
            if _as_float(row.get(field)) is not None
        ]
        all_descending = sorted(
            all_matured,
            key=lambda row: _as_float(row.get(field)) or 0.0,
            reverse=True,
        )
        all_candidate_best[label] = [
            _ranking_item(row, field) for row in all_descending[:top_n]
        ]
        all_candidate_worst[label] = [
            _ranking_item(row, field)
            for row in reversed(all_descending[-top_n:])
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
        "headlineBasis": "FORMAL_ACTIONABLE_ONLY",
        "headlineBasisLabel": "只統計正式進場候選",
        "overall": _metric_summary(actionable_rows),
        "allCandidates": _metric_summary(unique_rows),
        "byActionTier": _action_tier_summaries(unique_rows),
        "byStrategy": by_strategy,
        "byDate": by_date,
        "best": best,
        "worst": worst,
        "allCandidateBest": all_candidate_best,
        "allCandidateWorst": all_candidate_worst,
        "notes": [
            "主績效只統計正式進場候選；小部位試單與等待觀察分開顯示。",
            "本報表仍以雷達當日收盤價為訊號基準，不等於使用者實際成交損益。",
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
    exits: Sequence[Mapping[str, Any]] = (),
) -> float | None:
    active = [
        fill for fill in fills if int(fill["index"]) <= maximum_fill_index
    ]
    if not active:
        return None
    capital = sum(float(fill["percent"]) for fill in active)
    shares = sum(
        float(fill["percent"])
        / (float(fill["price"]) * BUY_COST_FACTOR)
        for fill in active
        if float(fill["price"]) > 0
    )
    if capital <= 0 or shares <= 0:
        return None
    realised_ratio = 0.0
    realised_proceeds = 0.0
    for exit_item in exits:
        if int(exit_item["index"]) > maximum_fill_index:
            continue
        share_ratio = max(0.0, min(float(exit_item["share_ratio"]), 1.0))
        share_ratio = min(share_ratio, 1.0 - realised_ratio)
        realised_proceeds += (
            shares
            * share_ratio
            * float(exit_item["price"])
            * SELL_PROCEEDS_FACTOR
        )
        realised_ratio += share_ratio
    remaining_ratio = max(0.0, 1.0 - realised_ratio)
    net_proceeds = (
        realised_proceeds
        + shares * remaining_ratio * price * SELL_PROCEEDS_FACTOR
    )
    return round((net_proceeds / capital - 1) * 100, 4)


def _execution_cost_model() -> dict[str, Any]:
    return {
        "returnBasis": "NET_AFTER_COST_AND_SLIPPAGE",
        "buyCostFactor": BUY_COST_FACTOR,
        "sellProceedsFactor": SELL_PROCEEDS_FACTOR,
        "confirmationEntrySlippageBps": CONFIRMATION_ENTRY_SLIPPAGE_BPS,
        "exitSlippageBps": EXIT_SLIPPAGE_BPS,
        "wickTouchFillRatio": WICK_TOUCH_FILL_RATIO,
        "takeProfit1R": TAKE_PROFIT_1_R,
        "takeProfit1ShareRatio": TAKE_PROFIT_1_RATIO,
        "takeProfit2R": TAKE_PROFIT_2_R,
        "takeProfit2ShareRatio": TAKE_PROFIT_2_RATIO,
        "trailingDistanceR": TRAILING_DISTANCE_R,
        "dailyBarOrdering": (
            "prior-session trailing stop before current-session profit targets"
        ),
        "broker": "Cathay e-order 28% fee, ordinary stock",
        "executionModelRevision": EXECUTION_MODEL_REVISION,
    }


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
    close-confirmation rule.  A daily wick touching the aggressive zone is
    only a partial fill and still needs a visible support/rebound proxy.  A
    confirmation entry is priced near that session's close instead of granting
    the backtest the earlier trigger price with hindsight.
    """
    plan = _mapping(snapshot.get("tradingPlan"))
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
        "planned_position_percent": None,
        "fill_ratio_percent": 0.0,
        "exit_date": None,
        "exit_price": None,
        "exit_reason": None,
        "exit_ledger": [],
        "profit_management": {},
        "return_d1": None,
        "return_d3": None,
        "return_d5": None,
        "return_d10": None,
        "return_d20": None,
        "max_favorable_percent": None,
        "max_adverse_percent": None,
        "factor_model_revision": str(
            snapshot.get("factorModelRevision") or ""
        ),
        "execution_model_revision": EXECUTION_MODEL_REVISION,
        "fill_assumption": (
            "SUPPORT_PROXY_WICK_PARTIAL_CLOSE_CONFIRMATION_PROFIT_TRAIL"
        ),
        "cost_model": _execution_cost_model(),
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
    planned_position = aggressive_pct + (
        confirmation_pct if confirmation_available else 0.0
    )
    result["planned_position_percent"] = planned_position

    fills: list[dict[str, Any]] = []
    failure_close_index: int | None = None
    signal_low = _as_float(snapshot.get("low"))
    prior_bar_low = signal_low

    for index, bar in enumerate(ordered):
        aggressive_filled_this_bar = False
        open_price = _bar_number(bar, "open")
        high = _bar_number(bar, "high")
        low = _bar_number(bar, "low")
        close = _bar_number(bar, "close")
        if None in (open_price, high, low, close):
            continue
        assert open_price is not None and high is not None
        assert low is not None and close is not None

        day_range = max(high - low, 0.0)
        close_position = (
            (close - low) / day_range if day_range > 0 else 0.5
        )
        lower_body = min(open_price, close)
        lower_shadow_ratio = (
            max(0.0, lower_body - low) / day_range
            if day_range > 0 else 0.0
        )
        low_not_lower = (
            prior_bar_low is not None and low >= prior_bar_low
        )
        support_proxy = (
            close_position >= 0.45
            or lower_shadow_ratio >= 0.20
            or low_not_lower
        )

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
                and support_proxy
            ):
                if open_price > aggressive_high:
                    fill_price = aggressive_high
                elif open_price >= aggressive_low:
                    fill_price = open_price
                else:
                    # A gap below the zone is priced at the zone floor rather
                    # than granting the backtest an unrealistically good fill.
                    fill_price = aggressive_low
                wick_only = (
                    open_price > aggressive_high
                    and close > aggressive_high
                    and low > aggressive_low
                )
                filled_percent = aggressive_pct * (
                    WICK_TOUCH_FILL_RATIO if wick_only else 1.0
                )
                fills.append(
                    {
                        "kind": "aggressive",
                        "index": index,
                        "date": bar.get("trade_date"),
                        "price": fill_price,
                        "percent": filled_percent,
                        "planned_percent": aggressive_pct,
                        "wick_only": wick_only,
                        "support_proxy": {
                            "closePosition": round(close_position, 4),
                            "lowerShadowRatio": round(lower_shadow_ratio, 4),
                            "lowNotLower": low_not_lower,
                        },
                    }
                )
                aggressive_filled_this_bar = True

            has_confirmation = any(
                fill["kind"] == "confirmation" for fill in fills
            )
            if (
                not has_confirmation
                and not aggressive_filled_this_bar
                and confirmation_pct > 0
                and confirmation_available
                and confirmation_price > 0
                and confirmation_price <= no_chase
                and high >= confirmation_price
                and close >= confirmation_price
                and close <= no_chase
            ):
                fill_price = close * (
                    1 + CONFIRMATION_ENTRY_SLIPPAGE_BPS / 10_000
                )
                if fill_price <= no_chase:
                    fills.append(
                        {
                            "kind": "confirmation",
                            "index": index,
                            "date": bar.get("trade_date"),
                            "price": fill_price,
                            "percent": confirmation_pct,
                            "planned_percent": confirmation_pct,
                            "priced_from": "confirmation session close",
                        }
                    )

        if fills and failure_price > 0 and close < failure_price:
            failure_close_index = index
            break
        prior_bar_low = low

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

    exits: list[dict[str, Any]] = []
    remaining_share_ratio = 1.0
    target1_hit = False
    target2_hit = False
    trailing_stop: float | None = None
    peak_after_target1: float | None = None
    final_exit_index: int | None = None
    status = "FILLED"
    reason = "已依V12.4可執行劇本成交"

    atr = _as_float(snapshot.get("atr14")) or _as_float(snapshot.get("atr"))
    risk_unit = (
        weighted_entry - failure_price
        if weighted_entry is not None and failure_price > 0
        else 0.0
    )
    if risk_unit <= 0 and weighted_entry is not None:
        risk_unit = max(atr or 0.0, weighted_entry * 0.03)
    target1_price = (
        weighted_entry + TAKE_PROFIT_1_R * risk_unit
        if weighted_entry is not None and risk_unit > 0
        else None
    )
    target2_price = (
        weighted_entry + TAKE_PROFIT_2_R * risk_unit
        if weighted_entry is not None and risk_unit > 0
        else None
    )
    total_entry_shares = sum(
        float(fill["percent"])
        / (float(fill["price"]) * BUY_COST_FACTOR)
        for fill in fills
        if float(fill["price"]) > 0
    )
    break_even_price = (
        cost / total_entry_shares / SELL_PROCEEDS_FACTOR
        if total_entry_shares > 0
        else weighted_entry
    )
    management_start_index = max(
        entry_window_sessions,
        max(int(fill["index"]) for fill in fills) + 1,
    )

    def append_exit(
        index: int,
        raw_price: float,
        share_ratio: float,
        exit_reason: str,
    ) -> None:
        nonlocal remaining_share_ratio, final_exit_index
        ratio = max(0.0, min(share_ratio, remaining_share_ratio))
        if ratio <= 0 or raw_price <= 0:
            return
        executed_price = raw_price * (1 - EXIT_SLIPPAGE_BPS / 10_000)
        exits.append({
            "index": index,
            "date": _as_iso_date(ordered[index].get("trade_date")),
            "price": round(executed_price, 4),
            "share_ratio": round(ratio, 6),
            "reason": exit_reason,
        })
        remaining_share_ratio = max(0.0, remaining_share_ratio - ratio)
        if remaining_share_ratio <= 1e-9:
            remaining_share_ratio = 0.0
            final_exit_index = index

    # Profit management starts only after the complete entry window and the
    # last fill.  A stop calculated from today's high can therefore take effect
    # no earlier than the next session, avoiding optimistic daily-bar ordering.
    management_end = (
        failure_close_index
        if failure_close_index is not None
        else len(ordered)
    )
    if target1_price is not None and target2_price is not None:
        for index in range(management_start_index, management_end):
            bar = ordered[index]
            open_price = _bar_number(bar, "open")
            high = _bar_number(bar, "high")
            low = _bar_number(bar, "low")
            if None in (open_price, high, low):
                continue
            assert open_price is not None and high is not None and low is not None

            # The prior session's trailing level is checked before today's high.
            if trailing_stop is not None and low <= trailing_stop:
                raw_exit = open_price if open_price <= trailing_stop else trailing_stop
                append_exit(
                    index,
                    raw_exit,
                    remaining_share_ratio,
                    "TRAILING_STOP",
                )
                break

            if not target1_hit and high >= target1_price:
                append_exit(
                    index,
                    target1_price,
                    TAKE_PROFIT_1_RATIO,
                    "TAKE_PROFIT_1",
                )
                target1_hit = True
            if (
                target1_hit
                and not target2_hit
                and remaining_share_ratio > 0
                and high >= target2_price
            ):
                append_exit(
                    index,
                    target2_price,
                    TAKE_PROFIT_2_RATIO,
                    "TAKE_PROFIT_2",
                )
                target2_hit = True
            if target1_hit and remaining_share_ratio > 0:
                peak_after_target1 = max(peak_after_target1 or high, high)
                trailing_stop = max(
                    break_even_price or 0.0,
                    peak_after_target1 - TRAILING_DISTANCE_R * risk_unit,
                )

    # The close-confirmed failure rule remains authoritative for any shares
    # that have not already been sold by profit management.
    pending_failure_exit = False
    if remaining_share_ratio > 0 and failure_close_index is not None:
        next_index = failure_close_index + 1
        if next_index < len(ordered):
            next_open = _bar_number(ordered[next_index], "open")
            if next_open is not None:
                append_exit(
                    next_index,
                    next_open,
                    remaining_share_ratio,
                    "CLOSE_FAILURE",
                )
        if remaining_share_ratio > 0:
            pending_failure_exit = True

    has_partial_profit = any(
        item["reason"] in {"TAKE_PROFIT_1", "TAKE_PROFIT_2"}
        for item in exits
    )
    final_reason = exits[-1]["reason"] if exits else None
    if final_exit_index is not None:
        status = "EXITED"
        if final_reason == "TRAILING_STOP" and has_partial_profit:
            exit_reason = "PARTIAL_PROFIT_TRAILING_STOP"
            reason = "已分批停利，剩餘部位依移動停損退出"
        elif final_reason == "CLOSE_FAILURE" and has_partial_profit:
            exit_reason = "PARTIAL_PROFIT_CLOSE_FAILURE"
            reason = "已分批停利，剩餘部位於失敗條件後退出"
        elif final_reason == "CLOSE_FAILURE":
            exit_reason = "CLOSE_FAILURE"
            reason = "收盤跌破失敗條件，隔日開盤退出"
        else:
            exit_reason = str(final_reason or "PROFIT_MANAGEMENT")
            reason = "已依分批停利規則退出"
    elif pending_failure_exit:
        status = "FILLED_PENDING_EXIT"
        exit_reason = (
            "PENDING_CLOSE_FAILURE"
            if has_partial_profit
            else None
        )
        reason = "已收盤跌破失敗條件，等待下一交易日開盤價"
    elif has_partial_profit:
        exit_reason = "PARTIAL_PROFIT"
        reason = "已分批停利，剩餘部位續抱並以移動停損管理"
    else:
        exit_reason = None

    exited_ratio = sum(float(item["share_ratio"]) for item in exits)
    average_exit_price = (
        sum(
            float(item["price"]) * float(item["share_ratio"])
            for item in exits
        ) / exited_ratio
        if exited_ratio > 0
        else None
    )
    last_exit = exits[-1] if exits else None
    profit_management = {
        "enabled": True,
        "riskUnit": round(risk_unit, 4),
        "failurePrice": round(failure_price, 4) if failure_price > 0 else None,
        "managementStartDate": (
            _as_iso_date(ordered[management_start_index].get("trade_date"))
            if management_start_index < len(ordered)
            else None
        ),
        "target1": {
            "rMultiple": TAKE_PROFIT_1_R,
            "price": round(target1_price, 4) if target1_price else None,
            "shareRatio": TAKE_PROFIT_1_RATIO,
            "hit": target1_hit,
        },
        "target2": {
            "rMultiple": TAKE_PROFIT_2_R,
            "price": round(target2_price, 4) if target2_price else None,
            "shareRatio": TAKE_PROFIT_2_RATIO,
            "hit": target2_hit,
        },
        "trailingStop": {
            "distanceR": TRAILING_DISTANCE_R,
            "lastLevel": round(trailing_stop, 4) if trailing_stop else None,
            "active": target1_hit and remaining_share_ratio > 0,
        },
        "remainingShareRatio": round(remaining_share_ratio, 6),
        "dailyBarOrdering": (
            "前一交易日形成的移動停損，才可於下一交易日觸發"
        ),
    }

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
        planned_position_percent=planned_position,
        fill_ratio_percent=(
            round(cost / planned_position * 100, 4)
            if planned_position > 0 else 0.0
        ),
        exit_date=(
            ordered[int(last_exit["index"])].get("trade_date")
            if last_exit else None
        ),
        exit_price=(
            round(average_exit_price, 4)
            if average_exit_price is not None else None
        ),
        exit_reason=exit_reason,
        exit_ledger=exits,
        profit_management=profit_management,
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
        if final_exit_index is not None and final_exit_index <= target_index:
            result[field] = _execution_return(
                fills,
                float(exits[-1]["price"]),
                final_exit_index,
                exits,
            )
        elif target_index < len(ordered):
            target_close = _bar_number(ordered[target_index], "close")
            if target_close is not None:
                result[field] = _execution_return(
                    fills,
                    target_close * (1 - EXIT_SLIPPAGE_BPS / 10_000),
                    target_index,
                    exits,
                )

    window_end = (
        final_exit_index
        if final_exit_index is not None
        else len(ordered) - 1
    )
    favorable: list[float] = []
    adverse: list[float] = []
    # The daily bar cannot tell whether its high/low occurred before or after
    # the first fill.  Start excursion statistics on the following session so
    # same-bar ordering never manufactures MFE or MAE.
    for index in range(first_index + 1, window_end + 1):
        high = _bar_number(ordered[index], "high")
        low = _bar_number(ordered[index], "low")
        if high is not None:
            value = _execution_return(
                fills,
                high * (1 - EXIT_SLIPPAGE_BPS / 10_000),
                index,
                exits,
            )
            if value is not None:
                favorable.append(value)
        if low is not None:
            value = _execution_return(
                fills,
                low * (1 - EXIT_SLIPPAGE_BPS / 10_000),
                index,
                exits,
            )
            if value is not None:
                adverse.append(value)
    if final_exit_index is not None and exits:
        exit_return = _execution_return(
            fills,
            float(exits[-1]["price"]),
            final_exit_index,
            exits,
        )
        if exit_return is not None:
            favorable.append(exit_return)
            adverse.append(exit_return)
    result["max_favorable_percent"] = max(favorable) if favorable else None
    result["max_adverse_percent"] = min(adverse) if adverse else None
    return result


async def _ensure_execution_schema(connection: Any) -> None:
    global _execution_schema_ready
    if _execution_schema_ready:
        return
    async with _execution_schema_lock:
        if _execution_schema_ready:
            return
        await connection.execute(_EXECUTION_TABLE_SQL)
        _execution_schema_ready = True


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
                COALESCE(c.snapshot->>'accuracyEngine','') NOT LIKE 'V12.%'
                OR LOWER(r.strategy) = 'v12_combined'
              )
              AND (
                e.radar_run_id IS NULL
                OR e.execution_model_revision IS DISTINCT FROM $2
                OR (
                  e.execution_status IN (
                      'PENDING', 'FILLED', 'FILLED_PENDING_EXIT'
                  )
                  AND (e.return_d20 IS NULL OR e.execution_status='PENDING')
                )
              )
            ORDER BY
              CASE
                WHEN e.radar_run_id IS NULL THEN 0
                WHEN e.execution_model_revision IS DISTINCT FROM $2 THEN 1
                ELSE 2
              END,
              r.run_date DESC,
              c.radar_run_id DESC,
              c.symbol ASC
            LIMIT $1
            """,
            limit,
            EXECUTION_MODEL_REVISION,
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
                    _canonical_execution_strategy(signal["strategy"]),
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
                    simulation["planned_position_percent"],
                    simulation["fill_ratio_percent"],
                    simulation["exit_date"],
                    simulation["exit_price"],
                    simulation["exit_reason"],
                    json.dumps(
                        simulation["exit_ledger"], ensure_ascii=False
                    ),
                    json.dumps(
                        simulation["profit_management"], ensure_ascii=False
                    ),
                    simulation["return_d1"],
                    simulation["return_d3"],
                    simulation["return_d5"],
                    simulation["return_d10"],
                    simulation["return_d20"],
                    simulation["max_favorable_percent"],
                    simulation["max_adverse_percent"],
                    simulation["evaluated_through"],
                    "V12.4",
                    str(snapshot.get("accuracyEngine") or ""),
                    str(snapshot.get("factorModelRevision") or ""),
                    simulation["execution_model_revision"],
                    json.dumps(simulation["cost_model"], ensure_ascii=False),
                    simulation["fill_assumption"],
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
                planned_position_percent, fill_ratio_percent,
                exit_date, exit_price, exit_reason,
                exit_ledger, profit_management,
                return_d1, return_d3, return_d5, return_d10, return_d20,
                max_favorable_percent, max_adverse_percent,
                evaluated_through, label_version, accuracy_engine,
                factor_model_revision, execution_model_revision,
                cost_model, fill_assumption,
                action_code, market_regime, industry, factor_confidence,
                calculated_at
            ) VALUES(
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                $16,$17,$18,$19,$20,$21::jsonb,$22::jsonb,$23,$24,$25,
                $26,$27,$28,$29,$30,$31,$32,$33,$34,$35::jsonb,$36,
                $37,$38,$39,$40,NOW()
            )
            ON CONFLICT(radar_run_id, symbol) DO UPDATE SET
                strategy=EXCLUDED.strategy,
                signal_date=EXCLUDED.signal_date,
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
                planned_position_percent=EXCLUDED.planned_position_percent,
                fill_ratio_percent=EXCLUDED.fill_ratio_percent,
                exit_date=EXCLUDED.exit_date,
                exit_price=EXCLUDED.exit_price,
                exit_reason=EXCLUDED.exit_reason,
                exit_ledger=EXCLUDED.exit_ledger,
                profit_management=EXCLUDED.profit_management,
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
                factor_model_revision=EXCLUDED.factor_model_revision,
                execution_model_revision=EXCLUDED.execution_model_revision,
                cost_model=EXCLUDED.cost_model,
                fill_assumption=EXCLUDED.fill_assumption,
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
    factor_model_revision: str | None = None,
) -> dict[str, Any]:
    """Return performance only for plans that would really have filled."""
    requested = str(strategy or "").strip().lower()
    resolved = _normalise_strategy(requested) if requested else ""
    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        conditions: list[str] = []
        args: list[Any] = []
        if resolved:
            args.append(resolved)
            parameter = f"${len(args)}"
            conditions.append(f"""
                (
                  REGEXP_REPLACE(
                    LOWER(COALESCE(
                      NULLIF(c.snapshot->>'strategy', ''),
                      NULLIF(e.strategy, ''),
                      ''
                    )),
                    '^v12_',
                    ''
                  )={parameter}
                  OR EXISTS (
                    SELECT 1
                    FROM JSONB_ARRAY_ELEMENTS_TEXT(
                      CASE
                        WHEN JSONB_TYPEOF(c.snapshot->'strategies')='array'
                        THEN c.snapshot->'strategies'
                        ELSE '[]'::jsonb
                      END
                    ) AS matched_strategy(value)
                    WHERE REGEXP_REPLACE(
                      LOWER(matched_strategy.value),
                      '^v12_',
                      ''
                    )={parameter}
                  )
                )
            """)
        requested_engine = str(accuracy_engine or "").strip()
        if requested_engine:
            args.append(requested_engine)
            conditions.append(
                "COALESCE(NULLIF(e.accuracy_engine, ''), "
                f"c.snapshot->>'accuracyEngine', '')=${len(args)}"
            )
        requested_factor_model = str(factor_model_revision or "").strip()
        if requested_factor_model:
            args.append(requested_factor_model)
            conditions.append(
                "COALESCE(NULLIF(e.factor_model_revision, ''), "
                f"c.snapshot->>'factorModelRevision', '')=${len(args)}"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        distinct_keys = "e.signal_date, e.symbol"
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
              COUNT(*) FILTER (WHERE execution_status='EXITED') AS exited,
              COUNT(*) FILTER (
                WHERE exit_reason IN (
                  'CLOSE_FAILURE','PARTIAL_PROFIT_CLOSE_FAILURE'
                )
              ) AS stopped,
              COUNT(*) FILTER (
                WHERE exit_reason='PARTIAL_PROFIT_TRAILING_STOP'
              ) AS trailing_exits,
              COUNT(*) FILTER (
                WHERE COALESCE(
                  profit_management->'target1'->>'hit', 'false'
                )='true'
              ) AS first_profit_taken,
              COUNT(*) FILTER (
                WHERE COALESCE(
                  profit_management->'target2'->>'hit', 'false'
                )='true'
              ) AS second_profit_taken,
              COUNT(*) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                ) AND fill_ratio_percent >= 99.9
              ) AS fully_filled,
              COUNT(*) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                ) AND fill_ratio_percent < 99.9
              ) AS partially_filled,
              AVG(filled_position_percent) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                )
              ) AS avg_filled_position,
              AVG(fill_ratio_percent) FILTER (
                WHERE execution_status IN (
                  'FILLED','FILLED_PENDING_EXIT','EXITED'
                )
              ) AS avg_fill_ratio,
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
    fully_filled = int(summary.get("fully_filled") or 0)
    summary["full_fill_rate_percent"] = (
        round(fully_filled / matured * 100, 2) if matured > 0 else None
    )
    return {
        "ok": True,
        "strategy": requested or None,
        "resolvedStrategy": resolved or None,
        "strategyLabel": _strategy_label(resolved) if resolved else None,
        "accuracyEngine": requested_engine or None,
        "factorModelRevision": requested_factor_model or None,
        "summary": summary,
        "method": EXECUTION_MODEL_REVISION,
        "costModel": _execution_cost_model(),
        "notes": [
            "碰到激進區間仍須有承接代理訊號；只有影線碰價只算部分成交。",
            "確認買點以確認日收盤附近加滑價成交，不回填較早的盤中觸價。",
            "不追價、未觸價與成交前失效不列為虧損交易。",
            "進場期結束後，以1R停利50%、2R停利25%，其餘採1R移動停損。",
            "移動停損只使用前一交易日已知高點，避免日K先後順序偷看。",
            "收盤確認失敗後，以次一交易日開盤價退出。",
            "所有報酬均已套用買進1.000399、賣出0.996601與設定滑價。",
        ],
    }


async def simulated_open_positions(
    factor_model_revision: str | None = None,
) -> dict[str, Any]:
    """Return deduplicated V12 simulated lots that still have exposure.

    Execution rows are deduplicated with the same signal-date/symbol rule used
    by :func:`execution_performance_summary`.  Different signal dates remain
    separate lots because the execution backtest evaluates them independently.
    Position values are model percentages; the simulator has no fixed capital
    base and therefore cannot truthfully manufacture share or lot counts.
    """
    requested_factor_model = str(factor_model_revision or "").strip()
    args: list[Any] = [EXECUTION_MODEL_REVISION]
    factor_condition = ""
    if requested_factor_model:
        args.append(requested_factor_model)
        factor_condition = (
            "AND COALESCE(NULLIF(e.factor_model_revision, ''), "
            "c.snapshot->>'factorModelRevision', '')=$2"
        )

    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        raw_open_records = await connection.fetchval(
            f"""
            SELECT COUNT(*)
            FROM signal_execution_performance e
            JOIN radar_candidates c
              ON c.radar_run_id=e.radar_run_id AND c.symbol=e.symbol
            WHERE e.execution_model_revision=$1
              {factor_condition}
              AND e.execution_status IN ('FILLED','FILLED_PENDING_EXIT')
              AND COALESCE(
                NULLIF(e.profit_management->>'remainingShareRatio','')::numeric,
                1
              ) > 0
            """,
            *args,
        )
        rows = await connection.fetch(
            f"""
            WITH dedup AS (
              SELECT DISTINCT ON (e.signal_date, e.symbol)
                e.*, c.total_score, s.name,
                COALESCE(
                  NULLIF(
                    e.profit_management->>'remainingShareRatio', ''
                  )::numeric,
                  CASE
                    WHEN e.execution_status IN (
                      'FILLED','FILLED_PENDING_EXIT'
                    ) THEN 1
                    ELSE 0
                  END
                ) AS remaining_share_ratio
              FROM signal_execution_performance e
              JOIN radar_candidates c
                ON c.radar_run_id=e.radar_run_id AND c.symbol=e.symbol
              JOIN securities s ON s.symbol=e.symbol
              WHERE e.execution_model_revision=$1
                {factor_condition}
              ORDER BY e.signal_date, e.symbol,
                       c.total_score DESC NULLS LAST,
                       e.radar_run_id DESC
            )
            SELECT *
            FROM dedup
            WHERE execution_status IN ('FILLED','FILLED_PENDING_EXIT')
              AND remaining_share_ratio > 0
            ORDER BY symbol, signal_date, entry_date
            """,
            *args,
        )

    grouped: dict[str, dict[str, Any]] = {}
    latest_evaluated: date | None = None
    pending_exit_lots = 0
    for record in rows:
        row = dict(record)
        symbol = str(row.get("symbol") or "")
        remaining_ratio = _as_float(row.get("remaining_share_ratio")) or 0.0
        filled_position = _as_float(row.get("filled_position_percent")) or 0.0
        remaining_position = filled_position * remaining_ratio
        entry_price = _as_float(row.get("weighted_entry_price"))
        entry_cost = entry_price * BUY_COST_FACTOR if entry_price else None
        status = str(row.get("execution_status") or "")
        if status == "FILLED_PENDING_EXIT":
            pending_exit_lots += 1

        evaluated = row.get("evaluated_through")
        if isinstance(evaluated, date):
            latest_evaluated = max(latest_evaluated or evaluated, evaluated)

        profit_management = _mapping(row.get("profit_management"))
        target1 = _mapping(profit_management.get("target1"))
        target2 = _mapping(profit_management.get("target2"))
        trailing = _mapping(profit_management.get("trailingStop"))
        lot = {
            "signalDate": _as_iso_date(row.get("signal_date")),
            "entryDate": _as_iso_date(row.get("entry_date")),
            "status": status,
            "statusReason": row.get("status_reason"),
            "entryPrice": round(entry_price, 4) if entry_price else None,
            "entryCostWithBuyFee": (
                round(entry_cost, 4) if entry_cost else None
            ),
            "filledPositionPercent": round(filled_position, 4),
            "remainingShareRatioPercent": round(remaining_ratio * 100, 2),
            "remainingModelPositionPercent": round(remaining_position, 4),
            "aggressiveFill": {
                "date": _as_iso_date(row.get("aggressive_fill_date")),
                "price": _as_float(row.get("aggressive_fill_price")),
                "positionPercent": _as_float(
                    row.get("aggressive_fill_percent")
                ) or 0.0,
            },
            "confirmationFill": {
                "date": _as_iso_date(row.get("confirmation_fill_date")),
                "price": _as_float(row.get("confirmation_fill_price")),
                "positionPercent": _as_float(
                    row.get("confirmation_fill_percent")
                ) or 0.0,
            },
            "failurePrice": _as_float(profit_management.get("failurePrice")),
            "target1": {
                "price": _as_float(target1.get("price")),
                "hit": bool(target1.get("hit")),
            },
            "target2": {
                "price": _as_float(target2.get("price")),
                "hit": bool(target2.get("hit")),
            },
            "trailingStop": {
                "level": _as_float(trailing.get("lastLevel")),
                "active": bool(trailing.get("active")),
            },
            "evaluatedThrough": _as_iso_date(evaluated),
        }
        stock = grouped.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": str(row.get("name") or ""),
                "lots": [],
                "remainingModelPositionPercent": 0.0,
                "_remainingCapital": 0.0,
                "_remainingShares": 0.0,
            },
        )
        stock["lots"].append(lot)
        stock["remainingModelPositionPercent"] += remaining_position
        if entry_cost and entry_cost > 0:
            stock["_remainingCapital"] += remaining_position
            stock["_remainingShares"] += remaining_position / entry_cost

    positions: list[dict[str, Any]] = []
    for stock in grouped.values():
        remaining_shares = float(stock.pop("_remainingShares"))
        remaining_capital = float(stock.pop("_remainingCapital"))
        stock["lotCount"] = len(stock["lots"])
        stock["remainingModelPositionPercent"] = round(
            float(stock["remainingModelPositionPercent"]), 4
        )
        stock["weightedEntryCostWithBuyFee"] = (
            round(remaining_capital / remaining_shares, 4)
            if remaining_shares > 0
            else None
        )
        positions.append(stock)

    return {
        "ok": True,
        "executionModelRevision": EXECUTION_MODEL_REVISION,
        "factorModelRevision": requested_factor_model or None,
        "evaluatedThrough": _as_iso_date(latest_evaluated),
        "rawOpenRecords": int(raw_open_records or 0),
        "openLots": len(rows),
        "distinctStocks": len(positions),
        "pendingExitLots": pending_exit_lots,
        "positions": positions,
        "notes": [
            "同一訊號日與股票只保留總分最高的一筆，避免重複雷達膨脹庫存。",
            "不同訊號日視為獨立模擬批次；模型未設定固定本金，因此部位以百分比呈現，不換算張數。",
            "成本已另列國泰電子下單買進手續費後成本；尚未完全退出的分批停利批次只計剩餘部位。",
        ],
    }


async def execution_strategy_priors(
    minimum_samples: int = 30,
    full_confidence_samples: int = 120,
    maximum_adjustment: float = 8.0,
    accuracy_engine: str | None = None,
    factor_model_revision: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build small confidence-weighted priors from matured executable trades."""
    minimum_samples = max(10, int(minimum_samples))
    full_confidence_samples = max(minimum_samples, int(full_confidence_samples))
    maximum_adjustment = max(0.0, min(float(maximum_adjustment), 15.0))
    requested_engine = str(accuracy_engine or "").strip()
    requested_factor_model = str(factor_model_revision or "").strip()
    prior_conditions: list[str] = []
    prior_args: list[Any] = []
    if requested_engine:
        prior_args.append(requested_engine)
        prior_conditions.append(
            "COALESCE(NULLIF(e.accuracy_engine, ''), "
            "c.snapshot->>'accuracyEngine', '')=$1"
        )
    if requested_factor_model:
        prior_args.append(requested_factor_model)
        prior_conditions.append(
            "COALESCE(NULLIF(e.factor_model_revision, ''), "
            f"c.snapshot->>'factorModelRevision', '')=${len(prior_args)}"
        )
    engine_where = (
        f"WHERE {' AND '.join(prior_conditions)}" if prior_conditions else ""
    )
    async with stock_database.acquire() as connection:
        await _ensure_execution_schema(connection)
        rows = await connection.fetch(
            f"""
            WITH base AS (
              SELECT e.*,
                     c.total_score,
                     CASE
                       WHEN JSONB_TYPEOF(c.snapshot->'strategies')='array'
                         AND JSONB_ARRAY_LENGTH(c.snapshot->'strategies') > 0
                       THEN c.snapshot->'strategies'
                       ELSE JSONB_BUILD_ARRAY(
                         REGEXP_REPLACE(
                           LOWER(COALESCE(
                             NULLIF(c.snapshot->>'strategy', ''),
                             NULLIF(e.strategy, ''),
                             'combined'
                           )),
                           '^v12_',
                           ''
                         )
                       )
                     END AS matched_strategies
              FROM signal_execution_performance e
              JOIN radar_candidates c
                ON c.radar_run_id=e.radar_run_id AND c.symbol=e.symbol
              {engine_where}
            ), expanded AS (
              SELECT base.*,
                     REGEXP_REPLACE(
                       LOWER(strategy_value.value),
                       '^v12_',
                       ''
                     ) AS strategy_key
              FROM base
              CROSS JOIN LATERAL JSONB_ARRAY_ELEMENTS_TEXT(
                base.matched_strategies
              ) AS strategy_value(value)
            ), dedup AS (
              SELECT DISTINCT ON (signal_date, symbol, strategy_key) expanded.*
              FROM expanded
              WHERE strategy_key <> ''
              ORDER BY signal_date, symbol, strategy_key,
                       total_score DESC NULLS LAST,
                       radar_run_id DESC
            )
            SELECT strategy_key AS strategy,
                   COUNT(return_d5) AS samples_d5,
                   COUNT(return_d10) AS samples_d10,
                   AVG(return_d5) AS avg_d5,
                   AVG(return_d10) AS avg_d10,
                   AVG(CASE WHEN return_d5 IS NULL THEN NULL
                            WHEN return_d5 > 0 THEN 1.0 ELSE 0.0 END)*100
                     AS win_d5
            FROM dedup
            WHERE execution_status IN (
              'FILLED','FILLED_PENDING_EXIT','EXITED'
            )
            GROUP BY strategy_key
            """,
            *prior_args,
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
    requested_version = str(version or "V12").strip().upper()
    normalised_version = _normalise_version(requested_version)
    display_version = (
        requested_version
        if requested_version in _REPORT_VERSION_ALIASES
        else normalised_version
    )
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
                "version": display_version,
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
              c.snapshot,
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
        version=display_version,
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


async def repair_v12_radar_run_dates(
    target_trade_date: str | date,
    apply: bool = False,
) -> dict[str, Any]:
    """Repair delayed V12 radar runs saved under their execution date.

    Only V12 rows whose immutable configuration explicitly names the requested
    ``latestTradeDate`` are eligible.  Derived performance rows are removed in
    the same transaction so the normal updater can rebuild them from the
    corrected signal date.
    """
    parsed_target = _parse_date(target_trade_date, "target_trade_date")
    if parsed_target is None:
        raise ValueError("target_trade_date is required")

    async with stock_database.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                f"""
                SELECT id, run_date, strategy, candidate_count
                FROM radar_runs r
                WHERE {_version_where("V12")}
                  AND r.configuration->>'latestTradeDate'=$1
                  AND r.run_date<>$2
                ORDER BY id
                FOR UPDATE
                """,
                parsed_target.isoformat(),
                parsed_target,
            )
            run_ids = [int(row["id"]) for row in rows]
            deleted_legacy = 0
            deleted_execution = 0
            if apply and run_ids:
                execution_result = await connection.execute(
                    """
                    DELETE FROM signal_execution_performance
                    WHERE radar_run_id=ANY($1::bigint[])
                    """,
                    run_ids,
                )
                legacy_result = await connection.execute(
                    """
                    DELETE FROM signal_performance
                    WHERE radar_run_id=ANY($1::bigint[])
                    """,
                    run_ids,
                )
                await connection.execute(
                    """
                    UPDATE radar_runs
                    SET run_date=$1
                    WHERE id=ANY($2::bigint[])
                    """,
                    parsed_target,
                    run_ids,
                )
                deleted_execution = int(execution_result.rsplit(" ", 1)[-1])
                deleted_legacy = int(legacy_result.rsplit(" ", 1)[-1])

    return {
        "ok": True,
        "applied": bool(apply),
        "targetTradeDate": parsed_target.isoformat(),
        "matchedRunCount": len(run_ids),
        "runIds": run_ids,
        "previousRunDates": sorted({
            _as_iso_date(row["run_date"]) for row in rows
        }),
        "deletedLegacyPerformanceRows": deleted_legacy,
        "deletedExecutionPerformanceRows": deleted_execution,
        "requiresPerformanceRebuild": bool(apply and run_ids),
    }
