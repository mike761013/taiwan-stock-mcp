import asyncio
from datetime import datetime, timedelta

from stock_db import factors, maintenance


TZ = factors.TAIPEI_TZ


def _status(*, now, age_days=3, tags=1900):
    last = now - timedelta(days=age_days)
    return factors._build_fundamental_refresh_status(
        now=now,
        interval_days=7,
        twse_last_updated_at=last,
        tpex_last_updated_at=last,
        revenue_rows=1900,
        official_theme_tags=tags,
    )


def test_fundamentals_are_skipped_inside_seven_day_interval():
    now = datetime(2026, 8, 27, 18, 0, tzinfo=TZ)
    status = _status(now=now, age_days=3)

    assert status["due"] is False
    assert status["intervalDays"] == 7
    assert "未滿7天" in status["reason"]


def test_fundamentals_become_due_after_seven_days():
    now = datetime(2026, 8, 31, 18, 0, tzinfo=TZ)
    status = _status(now=now, age_days=7)

    assert status["due"] is True
    assert "已達7天" in status["reason"]


def test_missing_official_theme_tags_force_self_repair():
    now = datetime(2026, 8, 27, 18, 0, tzinfo=TZ)
    status = _status(now=now, age_days=1, tags=0)

    assert status["due"] is True
    assert "題材標籤尚未初始化" in status["reason"]


def test_close_job_uses_scheduled_fundamental_refresh(monkeypatch):
    received = {}

    async def market_update(**kwargs):
        return {
            "ok": True,
            "hasMore": False,
            "nextStartAfter": None,
            "remainingSymbols": 0,
        }

    async def scheduled_refresh(*, interval_days, force):
        received.update({"interval_days": interval_days, "force": force})
        return {
            "ok": True,
            "skipped": True,
            "intervalDays": interval_days,
            "reason": "距上次完整更新未滿7天",
        }

    monkeypatch.setattr(maintenance, "update_official_daily", market_update)
    monkeypatch.setattr(
        maintenance,
        "refresh_monthly_revenue_if_due",
        scheduled_refresh,
    )

    result = asyncio.run(maintenance.run_daily_maintenance(
        run_radar=False,
        update_performance=False,
    ))

    assert result["completed"] is True
    assert result["fundamentalUpdate"]["skipped"] is True
    assert received == {"interval_days": 7, "force": False}


def test_close_job_can_force_fundamental_refresh(monkeypatch):
    received = {}

    async def market_update(**kwargs):
        return {
            "ok": True,
            "hasMore": False,
            "nextStartAfter": None,
            "remainingSymbols": 0,
        }

    async def scheduled_refresh(*, interval_days, force):
        received.update({"interval_days": interval_days, "force": force})
        return {"ok": True, "skipped": False}

    monkeypatch.setattr(maintenance, "update_official_daily", market_update)
    monkeypatch.setattr(
        maintenance,
        "refresh_monthly_revenue_if_due",
        scheduled_refresh,
    )

    asyncio.run(maintenance.run_daily_maintenance(
        run_radar=False,
        update_performance=False,
        fundamental_refresh_interval_days=14,
        force_fundamental_refresh=True,
    ))

    assert received == {"interval_days": 14, "force": True}
