import asyncio
from datetime import date, datetime
from decimal import Decimal

import pytest

from stock_db import advanced_factors
from stock_db import factors
from stock_db import performance
from stock_db.performance import simulate_signal_execution


def test_date_parser_accepts_roc_western_and_timestamp_without_leakage():
    assert advanced_factors.parse_roc_date("115/08/27") == date(2026, 8, 27)
    assert advanced_factors.parse_roc_date("2026-08-27 09:15:00") == date(
        2026, 8, 27
    )
    assert advanced_factors.parse_roc_date("20260827") == date(2026, 8, 27)
    assert advanced_factors.parse_roc_date("not-a-date") is None
    assert advanced_factors.parse_roc_period("1150827-1150902") == (
        date(2026, 8, 27),
        date(2026, 9, 2),
    )


def test_all_official_statement_layouts_are_registered():
    for market in ("twse", "tpex"):
        for statement in ("income", "balance"):
            for layout in advanced_factors._STATEMENT_LAYOUTS:
                assert f"{market}_{statement}_{layout}" in (
                    advanced_factors.OFFICIAL_ENDPOINTS
                )


def test_financial_layout_normalises_bank_without_industrial_margins():
    result = advanced_factors._financial_row(
        {
            "出表日期": "1150814",
            "年度": "115",
            "季別": "2",
            "利息淨收益": "1,000",
            "利息以外淨損益": "500",
            "淨利（損）歸屬於母公司業主": "200",
            "基本每股盈餘（元）": "1.25",
        },
        {
            "資產總額": "10,000",
            "負債總額": "8,000",
            "歸屬於母公司業主之權益合計": "2,000",
        },
        {"Date": "1150827", "PriceBookRatio": "1.4"},
        "basi",
    )

    assert result["isFinancialIndustry"] is True
    assert result["revenue"] == 1500
    assert result["netIncome"] == 200
    assert result["annualizedRoePercent"] == pytest.approx(20)
    assert result["grossMarginPercent"] is None
    assert result["pbRatio"] == 1.4


def test_event_engine_blocks_only_active_or_imminent_disposition():
    trade_day = date(2026, 8, 27)
    official = {
        "events": {},
        "attention": {},
        "disposition": {
            "2330": [{
                "date": "2026-08-26",
                "periodStart": "2026-08-27",
                "periodEnd": "2026-09-02",
            }],
            "2317": [{
                "date": "2026-08-01",
                "periodStart": "2026-08-02",
                "periodEnd": "2026-08-08",
            }],
        },
    }

    active_score, active = advanced_factors.score_event_context(
        "2330", official, trade_day
    )
    ended_score, ended = advanced_factors.score_event_context(
        "2317", official, trade_day
    )
    assert active["executionBlocked"] is True
    assert active_score < ended_score
    assert ended["executionBlocked"] is False


def test_hard_mops_risk_and_capital_dilution_are_detected():
    hard = advanced_factors.classify_event_text("停止買賣並有重大舞弊")
    dilution = advanced_factors.classify_event_text("董事會決議辦理現金增資")
    assert hard["hardRisk"] is True
    assert hard["impact"] <= -35
    assert dilution["eventType"] == "CAPITAL_ACTION"
    assert dilution["impact"] < 0


def test_routine_spokesperson_boilerplate_is_not_misread_as_litigation():
    result = advanced_factors.classify_event_text(
        "公告本公司發言人異動",
        "代理訴訟及非訟事項，並負責財務資訊",
    )
    assert result["impact"] == 0
    assert result["hardRisk"] is False


def test_tdcc_parser_aggregates_small_and_large_holders():
    csv_text = "\n".join([
        "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%",
        "1150821,2330,1,100,1000,5.5",
        "1150821,2330,9,50,100000,8.5",
        "1150821,2330,12,20,500000,21.0",
        "1150821,2330,17,999,0,100",
    ])
    parsed = advanced_factors.parse_tdcc_csv(csv_text)["2330"]
    assert parsed["snapshotDate"] == "2026-08-21"
    assert parsed["under100LotsPercent"] == 14
    assert parsed["over400LotsPercent"] == 21
    assert parsed["holderCount"] == 999


def test_chip_factor_normalises_institutional_flow_and_lending_acceleration(
    monkeypatch,
):
    async def fake_finmind_rows(dataset, symbol, start, end):
        assert symbol == "2330"
        rows = {
            "TaiwanStockInstitutionalInvestorsBuySell": [
                {"buy": 600_000, "sell": 200_000},
                {"buy": 300_000, "sell": 200_000},
            ],
            "TaiwanStockMarginPurchaseShortSale": [
                {"MarginPurchaseTodayBalance": 10_000},
                {"MarginPurchaseTodayBalance": 9_000},
            ],
            "TaiwanStockShareholding": [
                {
                    "ForeignInvestmentSharesRatio": 70.0,
                    "NumberOfSharesIssued": 100_000_000,
                },
                {
                    "ForeignInvestmentSharesRatio": 70.5,
                    "NumberOfSharesIssued": 100_000_000,
                },
            ],
            "TaiwanStockSecuritiesLending": [
                {"date": "2026-08-24", "volume": 100, "fee_rate": 2.0},
                {"date": "2026-08-25", "volume": 100, "fee_rate": 2.0},
                {"date": "2026-08-26", "volume": 400, "fee_rate": 3.0},
                {"date": "2026-08-27", "volume": 400, "fee_rate": 3.0},
            ],
        }
        return rows[dataset]

    monkeypatch.setattr(factors, "_finmind_rows", fake_finmind_rows)
    score, detail = asyncio.run(factors._chip_factor("2330", date(2026, 8, 27)))

    assert score is not None
    assert detail["institutionalNetToIssuedPercent"] == 0.5
    assert detail["securitiesLendingVolumeChangePercent"] == 300
    assert detail["averageSecuritiesLendingFeeRate"] == 2.5
    assert detail["componentScores"]["securitiesLending"] < 50


def test_news_deduplicates_headlines_and_rejects_future_rows():
    score, detail = advanced_factors.score_news_rows(
        [
            {
                "date": "2026-08-27 08:00:00",
                "title": "法人調升目標價",
                "source": "A",
            },
            {
                "date": "2026-08-27 09:00:00",
                "title": "法人調升目標價",
                "source": "B",
            },
            {
                "date": "2026-08-28 08:00:00",
                "title": "未來新聞優於預期",
                "source": "C",
            },
        ],
        date(2026, 8, 27),
    )
    assert score is not None and score > 50
    assert detail["newsCount"] == 1
    assert detail["sourceCount"] == 1
    assert detail["analystRevisionMentions"] == ["法人調升目標價"]


def test_finmind_news_uses_one_day_request_without_end_date(monkeypatch):
    captured = {}

    async def fake_json_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = dict(kwargs["params"])
        return {"status": 200, "data": []}

    monkeypatch.setenv("FINMIND_TOKEN", "test-token")
    monkeypatch.setattr(factors, "_json_get", fake_json_get)
    rows = asyncio.run(
        factors._finmind_rows(
            "TaiwanStockNews",
            "2330",
            date(2026, 8, 27),
            date(2026, 8, 27),
        )
    )

    assert rows == []
    assert captured["params"] == {
        "dataset": "TaiwanStockNews",
        "data_id": "2330",
        "start_date": "2026-08-27",
    }


@pytest.mark.parametrize(
    ("symbol", "industry", "expected_name", "expected_driver"),
    [
        ("6505", "23", "油電燃氣業", "XLE"),
        ("2606", "15", "航運業", "BDRY"),
        ("1326", "03", "塑膠工業", "XLB"),
        ("1513", "05", "電機機械", "XLI"),
        ("2375", "28", "電子零組件業", "QQQ"),
    ],
)
def test_sector_driver_resolves_twse_numeric_industry_codes(
    symbol, industry, expected_name, expected_driver
):
    asset_row = {
        "latestDate": "2026-08-26",
        "change1dPercent": 1.0,
        "change5dPercent": 2.0,
    }
    context = {
        "assets": {
            ticker: dict(asset_row)
            for ticker in (
                "XLE", "BDRY", "JETS", "XLB", "XLI", "QQQ", "SOXX"
            )
        },
        "macro": {
            "wti": dict(asset_row),
            "brent": dict(asset_row),
        },
        "source": "test",
        "taiwanImpactTiming": {"completedUSSession": "complete"},
    }

    score, detail = advanced_factors.score_sector_driver(
        symbol, industry, context
    )

    assert score is not None and score > 50
    assert detail["industryCode"] == industry
    assert expected_name in detail["resolvedIndustry"]
    assert expected_driver in {
        row["ticker"] for row in detail["drivers"]
    }


def test_oil_driver_can_use_macro_series_as_well_as_etf():
    context = {
        "assets": {
            "XLE": {"change1dPercent": 1.0, "change5dPercent": 1.0}
        },
        "macro": {
            "wti": {"change1dPercent": 2.0, "change5dPercent": 3.0},
            "brent": {"change1dPercent": 1.5, "change5dPercent": 2.5},
        },
        "source": "test",
        "taiwanImpactTiming": {},
    }

    score, detail = advanced_factors.score_sector_driver(
        "6505", "23", context
    )

    assert score is not None
    assert {
        (row["datasetGroup"], row["ticker"])
        for row in detail["drivers"]
    } >= {("assets", "XLE"), ("macro", "wti"), ("macro", "brent")}


def test_cross_market_same_date_is_only_a_live_preopen_overlay():
    async def fetcher(dataset, data_id, start, end):
        return [
            {"date": "2026-08-25", "Close": 100},
            {"date": "2026-08-26", "Close": 101},
            {"date": "2026-08-27", "Close": 104},
        ]

    close_context = asyncio.run(
        advanced_factors.build_global_market_context(
            date(2026, 8, 27),
            fetcher,
            current_time=datetime(2026, 8, 27, 14, 0),
        )
    )
    preopen_context = asyncio.run(
        advanced_factors.build_global_market_context(
            date(2026, 8, 27),
            fetcher,
            current_time=datetime(2026, 8, 28, 8, 0),
        )
    )

    assert close_context["nextSessionAssets"] == {}
    assert close_context["assets"]["^GSPC"]["latestDate"] == "2026-08-26"
    assert preopen_context["nextSessionAssets"]["^GSPC"][
        "latestDate"
    ] == "2026-08-27"
    assert preopen_context["taiwanImpactTiming"]["effectivePhase"] == (
        "PREOPEN_NEXT_SESSION_OVERLAY"
    )


def test_derivatives_only_scores_live_snapshot_in_next_session_window():
    async def fetcher(dataset, data_id, start, end):
        if dataset == "taiwan_futures_snapshot":
            return [{
                "date": "2026-08-27 16:00:00",
                "change_rate": 1.2,
                "close": 25000,
            }]
        return []

    context = asyncio.run(
        advanced_factors.build_derivatives_context(
            date(2026, 8, 27), fetcher
        )
    )
    assert context["available"] is True
    assert context["score"] > 50
    assert context["taiwanImpactTiming"]["nightOverlayIncluded"] is True


def test_daily_low_touch_without_support_proxy_is_not_a_fill():
    snapshot = {
        "tradingPlan": {
            "noChasePrice": 106,
            "aggressiveEntry": {
                "entryLow": 98,
                "entryHigh": 100,
                "positionPercent": 40,
            },
            "confirmationEntry": {
                "price": 103,
                "positionPercent": 60,
                "availableBelowNoChase": True,
            },
            "positionPlan": {},
            "failureCondition": {"price": 96},
        },
    }
    bars = [
        {"trade_date": date(2026, 8, 3), "open": 101, "high": 102,
         "low": 99, "close": 99.1},
        {"trade_date": date(2026, 8, 4), "open": 101, "high": 102,
         "low": 100.5, "close": 101},
        {"trade_date": date(2026, 8, 5), "open": 101, "high": 102,
         "low": 100.5, "close": 101},
    ]
    result = simulate_signal_execution(snapshot, bars)
    assert result["execution_status"] == "NO_TRADE"
    assert result["filled_position_percent"] == 0


def test_execution_upsert_persists_cost_fill_and_model_revisions(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.executemany_call = None

        async def execute(self, query, *args):
            return "OK"

        async def fetch(self, query, *args):
            if "FROM radar_candidates c" in query:
                assert args == (
                    performance.DEFAULT_PERFORMANCE_UPDATE_LIMIT,
                    performance.EXECUTION_MODEL_REVISION,
                )
                return [{
                    "radar_run_id": 1,
                    "symbol": "2330",
                    "strategy": "v12_early_stage",
                    "run_date": date(2026, 8, 2),
                    "snapshot": {
                        "accuracyEngine": "V12.4",
                        "factorModelRevision": (
                            "V12.4-COMPLETE-FACTORS-1"
                        ),
                        "tradingPlan": {
                            "noChasePrice": 106,
                            "aggressiveEntry": {
                                "entryLow": 98,
                                "entryHigh": 100,
                                "positionPercent": 40,
                            },
                            "confirmationEntry": {
                                "price": 103,
                                "positionPercent": 60,
                                "availableBelowNoChase": True,
                            },
                            "positionPlan": {},
                            "failureCondition": {"price": 96},
                        },
                    },
                }]
            return [
                {
                    "symbol": "2330",
                    "trade_date": date(2026, 8, 3),
                    "open": 99,
                    "high": 102,
                    "low": 98,
                    "close": 101,
                },
                {
                    "symbol": "2330",
                    "trade_date": date(2026, 8, 4),
                    "open": 102,
                    "high": 104,
                    "low": 101,
                    "close": 103.5,
                },
            ]

        async def executemany(self, query, records):
            self.executemany_call = (query, records)

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeDatabase:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    connection = FakeConnection()
    monkeypatch.setattr(
        performance,
        "stock_database",
        FakeDatabase(connection),
    )
    result = asyncio.run(performance.update_signal_execution_performance())
    assert result["processed"] == 1
    query, records = connection.executemany_call
    assert "$40" in query
    assert len(records[0]) == 40
    assert records[0][2] == "v12_early_stage"
    assert records[0][15] == 100
    assert records[0][16] == 100
    assert records[0][20] == "[]"
    assert '"enabled": true' in records[0][21]
    assert records[0][32] == "V12.4-COMPLETE-FACTORS-1"
    assert records[0][33] == performance.EXECUTION_MODEL_REVISION
    assert '"buyCostFactor": 1.000399' in records[0][34]


def test_execution_summary_matches_every_strategy_in_combined_snapshot(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.fetchrow_call = None

        async def execute(self, query, *args):
            return "OK"

        async def fetchrow(self, query, *args):
            self.fetchrow_call = (query, args)
            return {
                "evaluated_signals": 3,
                "pending": 0,
                "no_trade": 1,
                "cancelled": 1,
                "filled": 1,
                "exited": 0,
                "stopped": 0,
                "trailing_exits": 0,
                "first_profit_taken": 0,
                "second_profit_taken": 0,
                "fully_filled": 1,
                "partially_filled": 0,
            }

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeDatabase:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    connection = FakeConnection()
    monkeypatch.setattr(
        performance,
        "stock_database",
        FakeDatabase(connection),
    )

    result = asyncio.run(
        performance.execution_performance_summary(
            strategy="v12_breakout",
            factor_model_revision="V12.4-COMPLETE-FACTORS-2",
        )
    )

    query, args = connection.fetchrow_call
    assert args == ("breakout", "V12.4-COMPLETE-FACTORS-2")
    assert "JSONB_ARRAY_ELEMENTS_TEXT" in query
    assert "c.snapshot->'strategies'" in query
    assert result["resolvedStrategy"] == "breakout"
    assert result["strategyLabel"] == "放量突破"
    assert result["summary"]["entry_rate_percent"] == 33.33


def test_simulated_open_positions_deduplicates_and_groups_lots(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.fetchval_call = None
            self.fetch_call = None

        async def fetchval(self, query, *args):
            self.fetchval_call = (query, args)
            return 3

        async def fetch(self, query, *args):
            self.fetch_call = (query, args)
            return [
                {
                    "symbol": "2330",
                    "name": "台積電",
                    "signal_date": date(2026, 9, 10),
                    "entry_date": date(2026, 9, 11),
                    "execution_status": "FILLED",
                    "status_reason": "已分批停利",
                    "weighted_entry_price": Decimal("1000"),
                    "filled_position_percent": Decimal("40"),
                    "remaining_share_ratio": Decimal("0.5"),
                    "aggressive_fill_date": date(2026, 9, 11),
                    "aggressive_fill_price": Decimal("1000"),
                    "aggressive_fill_percent": Decimal("40"),
                    "confirmation_fill_date": None,
                    "confirmation_fill_price": None,
                    "confirmation_fill_percent": Decimal("0"),
                    "profit_management": {
                        "failurePrice": 970,
                        "target1": {"price": 1030, "hit": True},
                        "target2": {"price": 1060, "hit": False},
                        "trailingStop": {"lastLevel": 1010, "active": True},
                    },
                    "evaluated_through": date(2026, 9, 16),
                    "latest_symbol_date": date(2026, 9, 16),
                },
                {
                    "symbol": "2330",
                    "name": "台積電",
                    "signal_date": date(2026, 9, 12),
                    "entry_date": date(2026, 9, 14),
                    "execution_status": "FILLED_PENDING_EXIT",
                    "status_reason": "等待隔日開盤退出",
                    "weighted_entry_price": Decimal("1020"),
                    "filled_position_percent": Decimal("60"),
                    "remaining_share_ratio": Decimal("1"),
                    "aggressive_fill_date": None,
                    "aggressive_fill_price": None,
                    "aggressive_fill_percent": Decimal("0"),
                    "confirmation_fill_date": date(2026, 9, 14),
                    "confirmation_fill_price": Decimal("1020"),
                    "confirmation_fill_percent": Decimal("60"),
                    "profit_management": {},
                    "evaluated_through": date(2026, 9, 16),
                    "latest_symbol_date": date(2026, 9, 16),
                },
            ]

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class FakeDatabase:
        def __init__(self, connection):
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    async def schema_ready(connection):
        return None

    connection = FakeConnection()
    monkeypatch.setattr(performance, "stock_database", FakeDatabase(connection))
    monkeypatch.setattr(performance, "_ensure_execution_schema", schema_ready)

    result = asyncio.run(performance.simulated_open_positions())

    assert connection.fetchval_call[1] == (performance.EXECUTION_MODEL_REVISION,)
    assert "DISTINCT ON (e.signal_date, e.symbol)" in connection.fetch_call[0]
    assert result["rawOpenRecords"] == 3
    assert result["deduplicatedStoredOpenLots"] == 2
    assert result["openLots"] == 2
    assert result["distinctStocks"] == 1
    assert result["pendingExitLots"] == 1
    assert result["staleOpenLots"] == 0
    assert result["evaluatedThrough"] == "2026-09-16"
    position = result["positions"][0]
    assert position["lotCount"] == 2
    assert position["remainingModelPositionPercent"] == 80.0
    assert position["lots"][0]["remainingShareRatioPercent"] == 50.0
    assert position["lots"][0]["remainingModelPositionPercent"] == 20.0
    assert position["lots"][0]["entryCostWithBuyFee"] == 1000.399
