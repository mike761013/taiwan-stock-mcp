import asyncio
from datetime import date, datetime
from decimal import Decimal
import json

from stock_db.repository import StockRepository


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class Connection:
    def transaction(self, **kwargs):
        assert kwargs == {"readonly": True}
        return Context(self)

    async def fetchrow(self, sql, *args):
        assert "status='completed'" in sql
        assert args == ("v12_combined", 419, date(2026, 10, 2))
        return self.run

    async def fetch(self, sql, run_id):
        assert run_id == 419
        return [{"snapshot": self.snapshot}]


class Database:
    def acquire(self):
        return Context(self.connection)


def test_saved_run_restores_exact_snapshot_in_readonly_transaction():
    connection = Connection()
    connection.run = dict(id=419, candidate_count=1, run_date=date(2026, 10, 2),
                          started_at=datetime(2026, 10, 3), configuration='{"limitEach":10}')
    connection.snapshot = json.dumps(dict(symbol="2302", tradingPlan={"maximumBuyPrice": 46.55}))
    database = Database()
    database.connection = connection
    result = asyncio.run(StockRepository(database).get_saved_radar_run(
        run_id=419, run_date=date(2026, 10, 2)))
    assert result["ok"] and result["complete"] and result["rerun"] is False
    assert result["candidates"][0]["tradingPlan"]["maximumBuyPrice"] == 46.55
    assert result["run"]["run_date"] == "2026-10-02"
    assert result["run"]["configuration"] == {"limitEach": 10}


def test_missing_saved_run_does_not_attempt_candidate_query():
    connection = Connection()
    connection.run = None
    database = Database()
    database.connection = connection
    result = asyncio.run(StockRepository(database).get_saved_radar_run(
        run_id=419, run_date=date(2026, 10, 2)))
    assert result["ok"] is False
