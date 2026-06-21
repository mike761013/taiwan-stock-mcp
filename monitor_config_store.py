import json
import os
from pathlib import Path
from typing import Any

import redis.asyncio as redis


RULES_FILE = os.environ.get("MONITOR_RULES_FILE", "monitor_rules.json")
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
REDIS_KEY = os.environ.get("MONITOR_CONFIG_REDIS_KEY", "taiwan_stock_monitor_config_v9")


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "watchlist": [
        {"symbol": "2313", "name": "華通", "breakout_price": None, "stop_loss_price": None},
        {"symbol": "4977", "name": "眾達-KY", "breakout_price": None, "stop_loss_price": None},
    ],
    "rules": {
        "breakout_from_open_percent": 2.0,
        "drop_from_open_percent": -2.0,
        "new_high_extension_percent": 0.8,
        "cooldown_seconds": 300,
        "poll_seconds": 15,
        "market_only": True,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base, ensure_ascii=False))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def parse_watchlist(value: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = []
        for item in value:
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            items.append({
                "symbol": symbol,
                "name": str(item.get("name") or symbol).strip(),
                "breakout_price": item.get("breakout_price"),
                "stop_loss_price": item.get("stop_loss_price"),
            })
        return items

    items = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" in raw:
            symbol, name = raw.split(":", 1)
        else:
            symbol, name = raw, raw
        symbol = symbol.strip()
        name = name.strip() or symbol
        if symbol:
            items.append({
                "symbol": symbol,
                "name": name,
                "breakout_price": None,
                "stop_loss_price": None,
            })
    return items


def load_file_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))

    path = Path(RULES_FILE)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = _deep_merge(config, loaded)
        except Exception as exc:
            print(f"[monitor-config] failed to read {RULES_FILE}: {exc}", flush=True)

    # Environment overrides, useful for Render one-off settings.
    if os.environ.get("MONITOR_ENABLED"):
        config["enabled"] = os.environ["MONITOR_ENABLED"].strip().lower() in {"1", "true", "yes", "on"}

    if os.environ.get("MONITOR_WATCHLIST"):
        parsed = parse_watchlist(os.environ["MONITOR_WATCHLIST"])
        if parsed:
            config["watchlist"] = parsed

    if os.environ.get("MONITOR_POLL_SECONDS"):
        config.setdefault("rules", {})["poll_seconds"] = int(os.environ["MONITOR_POLL_SECONDS"])

    if os.environ.get("ALERT_COOLDOWN_SECONDS"):
        config.setdefault("rules", {})["cooldown_seconds"] = int(os.environ["ALERT_COOLDOWN_SECONDS"])

    return config


def redis_configured() -> bool:
    return bool(REDIS_URL)


async def _redis_client():
    if not REDIS_URL:
        return None
    return redis.from_url(REDIS_URL, decode_responses=True)


async def get_effective_config() -> dict[str, Any]:
    base_config = load_file_config()

    client = await _redis_client()
    if client is None:
        return base_config

    try:
        raw = await client.get(REDIS_KEY)
        await client.aclose()
    except Exception as exc:
        print(f"[monitor-config] redis read failed: {exc}", flush=True)
        return base_config

    if not raw:
        return base_config

    try:
        dynamic_config = json.loads(raw)
    except Exception as exc:
        print(f"[monitor-config] redis json parse failed: {exc}", flush=True)
        return base_config

    if not isinstance(dynamic_config, dict):
        return base_config

    return _deep_merge(base_config, dynamic_config)


async def save_dynamic_config(config: dict[str, Any]) -> dict[str, Any]:
    client = await _redis_client()
    if client is None:
        raise RuntimeError("REDIS_URL 尚未設定。請先在 Web Service 與 Background Worker 都設定同一組 REDIS_URL。")

    await client.set(REDIS_KEY, json.dumps(config, ensure_ascii=False))
    await client.aclose()
    return config


async def update_dynamic_config(
    *,
    watchlist: str | list[dict[str, Any]] | None = None,
    poll_seconds: int | None = None,
    cooldown_seconds: int | None = None,
    enabled: bool | None = None,
    market_only: bool | None = None,
    breakout_from_open_percent: float | None = None,
    drop_from_open_percent: float | None = None,
    new_high_extension_percent: float | None = None,
) -> dict[str, Any]:
    current = await get_effective_config()

    if watchlist is not None:
        parsed = parse_watchlist(watchlist)
        if not parsed:
            raise ValueError("watchlist 解析後是空的。格式範例：2313:華通,4977:眾達-KY")
        current["watchlist"] = parsed

    if enabled is not None:
        current["enabled"] = bool(enabled)

    rules = current.setdefault("rules", {})

    if poll_seconds is not None:
        poll_seconds = int(poll_seconds)
        if poll_seconds < 1:
            raise ValueError("poll_seconds 不能小於 1")
        rules["poll_seconds"] = poll_seconds

    if cooldown_seconds is not None:
        cooldown_seconds = int(cooldown_seconds)
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能小於 0")
        rules["cooldown_seconds"] = cooldown_seconds

    if market_only is not None:
        rules["market_only"] = bool(market_only)

    if breakout_from_open_percent is not None:
        rules["breakout_from_open_percent"] = float(breakout_from_open_percent)

    if drop_from_open_percent is not None:
        rules["drop_from_open_percent"] = float(drop_from_open_percent)

    if new_high_extension_percent is not None:
        rules["new_high_extension_percent"] = float(new_high_extension_percent)

    return await save_dynamic_config(current)
