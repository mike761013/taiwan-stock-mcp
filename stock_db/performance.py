"""Radar-signal performance updater and date-scoped weekly reports."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
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
DEFAULT_PERFORMANCE_UPDATE_LIMIT = 5000
MAX_PERFORMANCE_UPDATE_LIMIT = 20000


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


async def performance_summary(strategy: str | None = None) -> dict[str, Any]:
    """Return cumulative performance without counting pending D5 rows as losses."""
    where = "WHERE r.strategy=$1" if strategy else ""
    args = [strategy] if strategy else []
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
    return {"ok": True, "strategy": strategy, "summary": dict(row)}


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
