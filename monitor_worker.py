import asyncio
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from notifications import send_telegram_message
from monitor_config_store import get_effective_config
from server import get_realtime_quote


TAIPEI = ZoneInfo("Asia/Taipei")


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


def _pct_from_open(price: float | None, open_price: float | None) -> float | None:
    if price is None or open_price in (None, 0):
        return None
    return (price - open_price) / open_price * 100


def _signal_still_valid(
    signal: dict[str, Any],
    price: float | None,
    open_price: float | None,
    high_price: float | None,
    rules: dict[str, Any],
) -> tuple[bool, str]:
    if price is None:
        return False, "沒有現價"

    kind = signal.get("kind")
    pct = _pct_from_open(price, open_price)

    breakout_from_open_percent = float(rules.get("breakout_from_open_percent", 2.0))
    drop_from_open_percent = float(rules.get("drop_from_open_percent", -2.0))
    max_confirm_from_open_percent = float(rules.get("max_confirm_from_open_percent", 6.0))

    # 向上訊號：避免太晚追高，超過 max_confirm_from_open_percent 就不發二次確認。
    if kind in {"up_from_open", "new_high_extension", "custom_breakout"}:
        if pct is not None and pct > max_confirm_from_open_percent:
            return False, f"漲幅已達 {pct:.2f}%，超過二次確認追價上限 {max_confirm_from_open_percent:.2f}%"

    if kind == "up_from_open":
        return (pct is not None and pct >= breakout_from_open_percent), "漲幅仍維持在門檻上方"

    if kind == "down_from_open":
        return (pct is not None and pct <= drop_from_open_percent), "跌幅仍維持在門檻下方"

    if kind == "new_high_extension":
        trigger_price = to_float(signal.get("trigger_price"))
        if trigger_price is None:
            return False, "沒有觸發價"
        # 用現價確認，不只用日高，避免碰一下高點後回落仍通知。
        return price >= trigger_price, "現價仍站在創高觸發價上方"

    if kind == "custom_breakout":
        bp = to_float(signal.get("breakout_price"))
        return (bp is not None and price >= bp), "現價仍站在指定突破價上方"

    if kind == "custom_stop_loss":
        sp = to_float(signal.get("stop_loss_price"))
        return (sp is not None and price <= sp), "現價仍跌破指定停損價"

    return True, "訊號仍成立"


async def _send_alert(
    *,
    symbol: str,
    name: str,
    title: str,
    reason: str,
    price: float | None,
    open_price: float | None,
    now: datetime,
    extra: str = "",
) -> None:
    price_text = "-" if price is None else f"{price:.2f}"
    open_text = "-" if open_price is None else f"{open_price:.2f}"
    extra_block = f"\n{extra}" if extra else ""

    await send_telegram_message(
        f"【台股 MCP V9 監控】\n"
        f"{title}\n"
        f"{symbol} {name}\n"
        f"訊號：{reason}\n"
        f"現價：{price_text}\n"
        f"開盤：{open_text}"
        f"{extra_block}\n"
        f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def main():
    last_alert_at: dict[str, datetime] = {}
    seen_high: dict[str, float] = {}
    pending_signals: dict[str, dict[str, Any]] = {}

    config = await get_effective_config()
    rules = config.get("rules", {})
    await send_telegram_message(
        "【台股 MCP V9 監控啟動】\n"
        f"監控檔數：{len(config.get('watchlist', []))}\n"
        f"輪詢秒數：{rules.get('poll_seconds', 15)}\n"
        f"訊號模式：{rules.get('signal_mode', 'confirmed')}\n"
        f"二次確認：{rules.get('confirm_seconds', 45)} 秒\n"
        "現在已支援二次確認，降低把異動誤當進場點的機率。"
    )

    print("[monitor] started with dynamic config + confirmed signal support", flush=True)

    while True:
        try:
            config = await get_effective_config()
            rules = config.get("rules", {})
            watchlist = config.get("watchlist", [])

            poll_seconds = int(rules.get("poll_seconds", 15))
            cooldown_seconds = int(rules.get("cooldown_seconds", 300))
            market_only = bool(rules.get("market_only", True))
            enabled = bool(config.get("enabled", True))

            breakout_from_open_percent = float(rules.get("breakout_from_open_percent", 2.0))
            drop_from_open_percent = float(rules.get("drop_from_open_percent", -2.0))
            new_high_extension_percent = float(rules.get("new_high_extension_percent", 0.8))

            signal_mode = str(rules.get("signal_mode", "confirmed")).strip().lower()
            if signal_mode not in {"alert", "confirmed", "both"}:
                signal_mode = "confirmed"
            confirm_seconds = int(rules.get("confirm_seconds", 45))

            if not enabled:
                print("[monitor] disabled; sleeping", flush=True)
                await asyncio.sleep(max(poll_seconds, 5))
                continue

            if market_only and not is_tw_market_time():
                now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[monitor] outside market hours {now}; sleeping", flush=True)
                await asyncio.sleep(60)
                continue

            active_pending_keys: set[str] = set()

            for item in watchlist:
                symbol = str(item.get("symbol", "")).strip()
                name = str(item.get("name") or symbol).strip()

                if not symbol:
                    continue

                quote = await safe_quote(symbol)
                price, open_price, high_price = extract_quote_numbers(quote)

                now = datetime.now(TAIPEI)
                print(f"[monitor] {symbol} {name}: price={price}, open={open_price}, high={high_price}", flush=True)

                signals: list[dict[str, Any]] = []
                pct_from_open = _pct_from_open(price, open_price)

                if pct_from_open is not None:
                    if pct_from_open >= breakout_from_open_percent:
                        signals.append({
                            "kind": "up_from_open",
                            "reason": f"開盤漲幅達 {pct_from_open:.2f}%",
                            "trigger_price": price,
                        })

                    if pct_from_open <= drop_from_open_percent:
                        signals.append({
                            "kind": "down_from_open",
                            "reason": f"開盤跌幅達 {pct_from_open:.2f}%",
                            "trigger_price": price,
                        })

                ref_high = high_price or price
                if ref_high is not None:
                    previous_high = seen_high.get(symbol)
                    if previous_high is None:
                        seen_high[symbol] = ref_high
                    elif ref_high >= previous_high * (1 + new_high_extension_percent / 100):
                        signals.append({
                            "kind": "new_high_extension",
                            "reason": f"創監控新高，較前高 {previous_high:.2f} 延伸 {new_high_extension_percent:.2f}% 以上",
                            "trigger_price": price,
                        })
                        seen_high[symbol] = ref_high
                    elif ref_high > previous_high:
                        seen_high[symbol] = ref_high

                breakout_price = item.get("breakout_price")
                stop_loss_price = item.get("stop_loss_price")

                if price is not None and breakout_price:
                    bp = to_float(breakout_price)
                    if bp is not None and price >= bp:
                        signals.append({
                            "kind": "custom_breakout",
                            "reason": f"突破指定價 {bp}",
                            "trigger_price": price,
                            "breakout_price": bp,
                        })

                if price is not None and stop_loss_price:
                    sp = to_float(stop_loss_price)
                    if sp is not None and price <= sp:
                        signals.append({
                            "kind": "custom_stop_loss",
                            "reason": f"跌破指定停損價 {sp}",
                            "trigger_price": price,
                            "stop_loss_price": sp,
                        })

                for signal in signals:
                    kind = str(signal.get("kind"))
                    signal_key = f"{symbol}:{kind}"
                    active_pending_keys.add(signal_key)

                    # 舊版模式或 both：條件剛觸發就提醒「異動」，但仍不是進場建議。
                    if signal_mode in {"alert", "both"}:
                        alert_key = f"{signal_key}:raw"
                        last_at = last_alert_at.get(alert_key)
                        if not last_at or (now - last_at).total_seconds() >= cooldown_seconds:
                            last_alert_at[alert_key] = now
                            await _send_alert(
                                symbol=symbol,
                                name=name,
                                title="異動提醒，非下單建議",
                                reason=str(signal.get("reason")),
                                price=price,
                                open_price=open_price,
                                now=now,
                            )

                    # 新版二次確認：條件要持續成立 confirm_seconds 才提醒。
                    if signal_mode in {"confirmed", "both"}:
                        pending = pending_signals.get(signal_key)
                        if pending is None:
                            signal["first_seen_at"] = now.isoformat()
                            pending_signals[signal_key] = signal
                            print(f"[monitor] pending {signal_key}: {signal.get('reason')}", flush=True)
                            continue

                        first_seen = datetime.fromisoformat(str(pending.get("first_seen_at")))
                        elapsed = (now - first_seen).total_seconds()
                        pending["reason"] = signal.get("reason")
                        pending["trigger_price"] = signal.get("trigger_price")

                        if elapsed < confirm_seconds:
                            continue

                        valid, note = _signal_still_valid(pending, price, open_price, high_price, rules)
                        if not valid:
                            print(f"[monitor] pending rejected {signal_key}: {note}", flush=True)
                            pending_signals.pop(signal_key, None)
                            continue

                        alert_key = f"{signal_key}:confirmed"
                        last_at = last_alert_at.get(alert_key)
                        if last_at and (now - last_at).total_seconds() < cooldown_seconds:
                            continue

                        last_alert_at[alert_key] = now
                        pending_signals.pop(signal_key, None)

                        await _send_alert(
                            symbol=symbol,
                            name=name,
                            title="二次確認成立，請人工判斷是否可進場",
                            reason=str(pending.get("reason")),
                            price=price,
                            open_price=open_price,
                            now=now,
                            extra=f"確認時間：{confirm_seconds} 秒\n確認說明：{note}\n提醒：這不是自動下單建議，仍需看量價、大盤與停損位置。",
                        )

                await asyncio.sleep(0.2)

            # 清掉已經不再成立的 pending signal。
            for key in list(pending_signals.keys()):
                symbol = key.split(":", 1)[0]
                if symbol in {str(item.get("symbol", "")).strip() for item in watchlist} and key not in active_pending_keys:
                    pending_signals.pop(key, None)

        except Exception as exc:
            print(f"[monitor] error: {exc}", flush=True)
            try:
                await send_telegram_message(f"【台股 MCP V9 監控錯誤】\n{exc}")
            except Exception as notify_exc:
                print(f"[monitor] notify error: {notify_exc}", flush=True)

        await asyncio.sleep(max(int((await get_effective_config()).get("rules", {}).get("poll_seconds", 15)), 1))


if __name__ == "__main__":
    asyncio.run(main())
