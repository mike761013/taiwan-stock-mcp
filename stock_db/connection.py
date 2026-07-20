"""Safe asyncpg connection-pool management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg

from .config import StockDatabaseConfig, load_config

logger = logging.getLogger(__name__)


class StockDatabase:
    def __init__(self, config: StockDatabaseConfig | None = None) -> None:
        self.config = config or load_config()
        self._pool: asyncpg.Pool | None = None
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._pool is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def connect(self) -> bool:
        if not self.config.enabled:
            self._last_error = "disabled"
            return False
        if not self.config.database_url:
            self._last_error = "DATABASE_URL missing"
            return False
        if self._pool is not None:
            return True

        async with self._lock:
            if self._pool is not None:
                return True
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.config.database_url,
                    min_size=self.config.pool_min,
                    max_size=self.config.pool_max,
                    timeout=15,
                    command_timeout=self.config.statement_timeout_seconds,
                    max_inactive_connection_lifetime=300,
                    server_settings={
                        "application_name": "taiwan-stock-mcp-v10",
                        "timezone": "Asia/Taipei",
                    },
                )
                self._last_error = None
                logger.info("V10 PostgreSQL pool created.")
                return True
            except Exception as exc:
                self._pool = None
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("V10 PostgreSQL connection failed.")
                return False

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        if not await self.connect() or self._pool is None:
            raise RuntimeError(f"Stock database unavailable: {self._last_error}")
        async with self._pool.acquire() as connection:
            yield connection

    async def health(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"enabled": False, "connected": False, "status": "disabled"}
        if not self.config.database_url:
            return {
                "enabled": True,
                "connected": False,
                "status": "database_url_missing",
            }
        if not await self.connect():
            return {
                "enabled": True,
                "connected": False,
                "status": "connection_failed",
                "error": self._last_error,
            }
        try:
            async with self.acquire() as connection:
                value = await connection.fetchval("SELECT 1")
                version = await connection.fetchval("SHOW server_version")
            return {
                "enabled": True,
                "connected": value == 1,
                "status": "healthy",
                "postgresVersion": version,
            }
        except Exception as exc:
            return {
                "enabled": True,
                "connected": False,
                "status": "health_check_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }


stock_database = StockDatabase()
