import inspect
import unittest
from unittest.mock import patch

from stock_db.performance import (
    DEFAULT_PERFORMANCE_UPDATE_LIMIT,
    MAX_PERFORMANCE_UPDATE_LIMIT,
    update_signal_performance,
)


class _FakeConnection:
    def __init__(self):
        self.fetch_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []


class _FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeDatabase:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _FakeAcquire(self.connection)


class PerformanceUpdatePriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_updates_large_active_window_and_prioritises_new(self):
        connection = _FakeConnection()
        database = _FakeDatabase(connection)

        with patch("stock_db.performance.stock_database", database):
            result = await update_signal_performance()

        self.assertEqual(DEFAULT_PERFORMANCE_UPDATE_LIMIT, 5000)
        self.assertEqual(
            inspect.signature(update_signal_performance)
            .parameters["limit"]
            .default,
            DEFAULT_PERFORMANCE_UPDATE_LIMIT,
        )
        self.assertEqual(result["limit"], DEFAULT_PERFORMANCE_UPDATE_LIMIT)
        self.assertEqual(result["selected"], 0)
        self.assertTrue(result["newSignalsPrioritised"])

        query, args = connection.fetch_calls[0]
        self.assertEqual(args, (DEFAULT_PERFORMANCE_UPDATE_LIMIT,))
        self.assertIn(
            "CASE WHEN p.radar_run_id IS NULL THEN 0 ELSE 1 END",
            query,
        )
        self.assertIn("p.calculated_at ASC NULLS FIRST", query)
        self.assertIn("r.run_date DESC", query)

    async def test_manual_limit_is_safely_capped(self):
        connection = _FakeConnection()
        database = _FakeDatabase(connection)

        with patch("stock_db.performance.stock_database", database):
            result = await update_signal_performance(999999)

        self.assertEqual(result["limit"], MAX_PERFORMANCE_UPDATE_LIMIT)
        self.assertEqual(
            connection.fetch_calls[0][1],
            (MAX_PERFORMANCE_UPDATE_LIMIT,),
        )


if __name__ == "__main__":
    unittest.main()
