import asyncio
import json
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

from notifications import send_telegram_message
from server import get_realtime_quote


TAIPEI = ZoneInfo("Asia/Taipei")
RULES_FILE = os.environ.get("MONITOR_RULES_FILE", "monitor_rules.json")


DEFAULT_CONFIG = {
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


def load_config() -> dict:
    config = DEFAULT_CONFIG.copy()

    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                config.update(loaded)
        except Exception as exc:
            print(f"[monitor] failed to read {RULES_FILE}: {exc}", flush=True)

    if os.environ.get("MONITOR_POLL_SECONDS"):
        config.setdefault("rules", {})["poll_seconds"] = int(os.environ["MONITOR_POLL_SECONDS"])

    if os.environ.get("ALERT_COOLDOWN_SECONDS"):
        config.setdefault("rules", {})["cooldown_seconds"] = int(os.environ["ALERT_COOLDOWN_SECONDS"])

    if os.environ.get("MONITOR_ENABLED"):
        config["enabled"] = os.environ["MONITOR_ENABLED"].strip().lower() in {"1", "true", "yes", "on"}

    # Example:
    # MONITOR_WATCHLIST=2313:華通,4977:眾達-KY
    if os.environ.get("MONITOR_WATCHLIST"):
        items = []
        for raw in os.environ["MONITOR_WATCHLIST"].split(","):
            raw = raw.strip()
            if not raw:
                continue
            if ":" in raw:
                symbol, name = raw.split(":", 1)
            else:
                symbol, name = raw, raw
            items.append({
                "symbol": symbol.strip(),
                "name": name.strip(),
                "breakout_price": None,
                "stop_loss_price": None,
            })
        if items:
            config["watchlist"] = items

    return config


def is_tw_market_time() -> bool:
    now = datetime.now(TAIPEI)
    if now.weekday() >= 5:
        return False
    return time(9, 0) <= now.time() <= time(13, 30)


def walk_values(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k), v
            yield from walk_values(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_values(item)


def to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("%", "").strip()
        if cleaned in {"", "-", "null", "None"}:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def find_number(data, preferred_keys):
    preferred = {k.lower() for k in preferred_keys}

    for k, v in walk_values(data):
        key = k.lower()
        if key in preferred:
            num = to_float(v)
            if num is not None:
                return num

    for k, v in walk_values(data):
        key = k.lower()
        if any(p in key for p in preferred):
            num = to_float(v)
            if num is not None:
                return num

    return None


def extract_quote_numbers(data: dict) -> tuple[float | None, float | None, float | None]:
    price = find_number(data, [
        "price", "lastPrice", "last_price", "close", "closePrice", "tradePrice",
        "last", "成交價", "現價", "最新價"
    ])
    open_price = find_number(data, [
        "open", "openPrice", "open_price", "開盤價", "開盤"
    ])
    high_price = find_number(data, [
        "high", "highPrice", "high_price", "最高價", "最高"
    ])
    return price, open_price, high_price


async def safe_quote(symbol: str) -> dict:
    try:
        return await get_realtime_quote(symbol=symbol)
    except TypeError:
        try:
            return await get_realtime_quote(stock_id=symbol)
        except TypeError:
            return await get_realtime_quote(symbol)


async def main():
    config = load_config()
    rules = config.get("rules", {})
    watchlist = config.get("watchlist", [])

    poll_seconds = int(rules.get("poll_seconds", 15))
    cooldown_seconds = int(rules.get("cooldown_seconds", 300))
    market_only = bool(rules.get("market_only", True))

    breakout_from_open_percent = float(rules.get("breakout_from_open_percent", 2.0))
    drop_from_open_percent = float(rules.get("drop_from_open_percent", -2.0))
    new_high_extension_percent = float(rules.get("new_high_extension_percent", 0.8))

    enabled = bool(config.get("enabled", True))

    last_alert_at: dict[str, datetime] = {}
    seen_high: dict[str, float] = {}

    await send_telegram_message(
        "【台股 MCP V9 監控啟動】\n"
        f"監控檔數：{len(watchlist)}\n"
        f"輪詢秒數：{poll_seconds}\n"
        f"market_only：{market_only}\n"
        "若非台股盤中，程式會等待盤中再監控。"
    )

    print(f"[monitor] started. enabled={enabled}, watchlist={watchlist}, poll={poll_seconds}", flush=True)

    while True:
        try:
            config = load_config()
            rules = config.get("rules", rules)
            watchlist = config.get("watchlist", watchlist)

            poll_seconds = int(rules.get("poll_seconds", poll_seconds))
            cooldown_seconds = int(rules.get("cooldown_seconds", cooldown_seconds))
            market_only = bool(rules.get("market_only", market_only))
            enabled = bool(config.get("enabled", enabled))

            if not enabled:
                print("[monitor] disabled; sleeping", flush=True)
                await asyncio.sleep(max(poll_seconds, 5))
                continue

            if market_only and not is_tw_market_time():
                now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[monitor] outside market hours {now}; sleeping", flush=True)
                await asyncio.sleep(60)
                continue

            for item in watchlist:
                symbol = str(item.get("symbol", "")).strip()
                name = str(item.get("name") or symbol).strip()

                if not symbol:
                    continue

                quote = await safe_quote(symbol)
                price, open_price, high_price = extract_quote_numbers(quote)

                now = datetime.now(TAIPEI)
                print(f"[monitor] {symbol} {name}: price={price}, open={open_price}, high={high_price}", flush=True)

                messages = []

                if price is not None and open_price not in (None, 0):
                    pct_from_open = (price - open_price) / open_price * 100

                    if pct_from_open >= breakout_from_open_percent:
                        messages.append(f"開盤漲幅達 {pct_from_open:.2f}%")

                    if pct_from_open <= drop_from_open_percent:
                        messages.append(f"開盤跌幅達 {pct_from_open:.2f}%")

                ref_high = high_price or price
                if ref_high is not None:
                    previous_high = seen_high.get(symbol)
                    if previous_high is None:
                        seen_high[symbol] = ref_high
                    elif ref_high >= previous_high * (1 + new_high_extension_percent / 100):
                        messages.append(f"創監控新高，較前高 {previous_high:.2f} 延伸 {new_high_extension_percent:.2f}% 以上")
                        seen_high[symbol] = ref_high
                    elif ref_high > previous_high:
                        seen_high[symbol] = ref_high

                breakout_price = item.get("breakout_price")
                stop_loss_price = item.get("stop_loss_price")

                if price is not None and breakout_price:
                    bp = to_float(breakout_price)
                    if bp is not None and price >= bp:
                        messages.append(f"突破指定價 {bp}")

                if price is not None and stop_loss_price:
                    sp = to_float(stop_loss_price)
                    if sp is not None and price <= sp:
                        messages.append(f"跌破指定停損價 {sp}")

                for reason in messages:
                    alert_key = f"{symbol}:{reason}"
                    last_at = last_alert_at.get(alert_key)
                    if last_at and (now - last_at).total_seconds() < cooldown_seconds:
                        continue

                    last_alert_at[alert_key] = now

                    price_text = "-" if price is None else f"{price:.2f}"
                    open_text = "-" if open_price is None else f"{open_price:.2f}"

                    await send_telegram_message(
                        f"【台股 MCP V9 監控】\n"
                        f"{symbol} {name}\n"
                        f"訊號：{reason}\n"
                        f"現價：{price_text}\n"
                        f"開盤：{open_text}\n"
                        f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                await asyncio.sleep(0.2)

        except Exception as exc:
            print(f"[monitor] error: {exc}", flush=True)
            try:
                await send_telegram_message(f"【台股 MCP V9 監控錯誤】\n{exc}")
            except Exception as notify_exc:
                print(f"[monitor] notify error: {notify_exc}", flush=True)

        await asyncio.sleep(max(poll_seconds, 1))


if __name__ == "__main__":
    asyncio.run(main())
