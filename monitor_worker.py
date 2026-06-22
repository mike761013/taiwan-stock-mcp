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


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "漲停"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


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


def find_bool(data, preferred_keys):
    preferred = {k.lower() for k in preferred_keys}
    for k, v in walk_values(data):
        key = k.lower()
        if key in preferred:
            return to_bool(v)
    return False


def extract_quote_numbers(data: dict) -> tuple[float | None, float | None, float | None, float | None, bool]:
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
    reference_price = find_number(data, [
        "referencePrice", "reference_price", "previousClose", "previous_close", "昨收", "參考價", "基準價"
    ])
    is_limit_up = find_bool(data, [
        "isLimitUpPrice", "is_limit_up", "limitUp", "漲停"
    ])
    return price, open_price, high_price, reference_price, is_limit_up


async def safe_quote(symbol: str) -> dict:
    try:
        return await get_realtime_quote(symbol=symbol)
    except TypeError:
        try:
            return await get_realtime_quote(stock_id=symbol)
        except TypeError:
            return await get_realtime_quote(symbol)


def _pct(base: float | None, value: float | None) -> float | None:
    if value is None or base in (None, 0):
        return None
    return (value - base) / base * 100


def _pct_from_open(price: float | None, open_price: float | None) -> float | None:
    return _pct(open_price, price)


def _format_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _round_price(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _is_limit_up_state(
    *,
    price: float | None,
    reference_price: float | None,
    is_limit_up_flag: bool,
    rules: dict[str, Any],
) -> tuple[bool, str]:
    if is_limit_up_flag:
        return True, "報價欄位顯示漲停"

    limit_up_near_percent = float(rules.get("limit_up_near_percent", 9.5))
    pct_ref = _pct(reference_price, price)
    if pct_ref is not None and pct_ref >= limit_up_near_percent:
        return True, f"距基準價漲幅 {pct_ref:.2f}%，已接近漲停區"

    return False, ""


def _calc_trade_plan(price: float | None, open_price: float | None, rules: dict[str, Any]) -> dict[str, Any]:
    if price is None:
        return {"entry": None, "stop_loss": None, "take_profit": None, "risk_per_share": None, "risk_reward": None}

    stop_pct = float(rules.get("entry_stop_loss_percent", 2.0))
    take_r = float(rules.get("take_profit_r_multiple", 1.8))

    pct_stop = price * (1 - stop_pct / 100)
    if open_price and open_price > 0 and price > open_price:
        open_guard_stop = open_price * 0.995
        stop_loss = max(pct_stop, open_guard_stop)
    else:
        stop_loss = pct_stop

    risk = max(price - stop_loss, 0.01)
    take_profit = price + risk * take_r

    return {
        "entry": _round_price(price),
        "stop_loss": _round_price(stop_loss),
        "take_profit": _round_price(take_profit),
        "risk_per_share": _round_price(risk),
        "risk_reward": round(take_r, 2),
    }



def _gap_recovery_signal(
    *,
    price: float | None,
    open_price: float | None,
    reference_price: float | None,
    rules: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    """偵測開低走高：開低後收復跌幅，接近昨收/參考價。"""
    if not bool(rules.get("gap_recovery_enabled", True)):
        return False, "未啟用開低走高模式", {}

    if price is None or open_price is None or reference_price is None:
        return False, "缺少現價、開盤價或參考價", {}

    if not (open_price < reference_price):
        return False, "不是開低盤", {}

    gap = reference_price - open_price
    if gap <= 0:
        return False, "開低缺口不成立", {}

    from_open = (price - open_price) / open_price * 100
    recovered = (price - open_price) / gap * 100
    distance_to_reference = (price - reference_price) / reference_price * 100

    min_from_open = float(rules.get("gap_recovery_min_from_open_percent", 2.0))
    min_recovered = float(rules.get("gap_recovery_min_recovered_percent", 60.0))
    near_reference = float(rules.get("gap_recovery_near_reference_percent", 1.0))
    max_above_reference = float(rules.get("gap_recovery_max_above_reference_percent", 2.0))

    if from_open < min_from_open:
        return False, f"開低後反彈 {from_open:.2f}% 未達 {min_from_open:.2f}%", {}

    if recovered < min_recovered:
        return False, f"開低缺口只收復 {recovered:.1f}% 未達 {min_recovered:.1f}%", {}

    if distance_to_reference < -near_reference:
        return False, f"距參考價仍有 {distance_to_reference:.2f}%，尚未接近翻紅區", {}

    if distance_to_reference > max_above_reference:
        return False, f"已高於參考價 {distance_to_reference:.2f}%，可能偏追高", {}

    reason = (
        f"開低走高，開低後拉升 {from_open:.2f}%，"
        f"收復缺口 {recovered:.1f}%，"
        f"距參考價 {distance_to_reference:.2f}%"
    )
    meta = {
        "from_open_percent": from_open,
        "gap_recovered_percent": recovered,
        "distance_to_reference_percent": distance_to_reference,
    }
    return True, reason, meta


def _entry_filter(
    *,
    signal: dict[str, Any],
    price: float | None,
    open_price: float | None,
    reference_price: float | None,
    is_limit_up: bool,
    rules: dict[str, Any],
) -> tuple[bool, str]:
    if not bool(rules.get("entry_filter_enabled", True)):
        return True, "未啟用進場過濾"

    if price is None:
        return False, "沒有現價"

    kind = str(signal.get("kind"))
    if kind not in {"up_from_open", "new_high_extension", "custom_breakout", "gap_recovery"}:
        return False, "不是偏多進場訊號"

    limit_state, limit_reason = _is_limit_up_state(
        price=price,
        reference_price=reference_price,
        is_limit_up_flag=is_limit_up,
        rules=rules,
    )
    if limit_state and bool(rules.get("suppress_limit_up_repeats", True)):
        return False, f"{limit_reason}，不發進場候選通知"

    if kind == "gap_recovery":
        from_open = signal.get("from_open_percent")
        recovered = signal.get("gap_recovered_percent")
        dist_ref = signal.get("distance_to_reference_percent")
        return True, f"開低走高符合條件：開低後拉升 {float(from_open):.2f}%，收復缺口 {float(recovered):.1f}%，距參考價 {float(dist_ref):.2f}%"

    pct_open = _pct_from_open(price, open_price)
    min_pct = float(rules.get("entry_min_from_open_percent", 1.2))
    max_pct = float(rules.get("entry_max_from_open_percent", 5.5))

    if pct_open is None:
        return False, "沒有開盤價，無法判斷是否追高"
    if pct_open < min_pct:
        return False, f"漲幅 {pct_open:.2f}% 尚未達進場候選下限 {min_pct:.2f}%"
    if pct_open > max_pct:
        return False, f"漲幅 {pct_open:.2f}% 超過追價上限 {max_pct:.2f}%"

    return True, f"漲幅 {pct_open:.2f}% 位於進場候選區間 {min_pct:.2f}%～{max_pct:.2f}%"


def _signal_still_valid(
    signal: dict[str, Any],
    price: float | None,
    open_price: float | None,
    high_price: float | None,
    reference_price: float | None,
    is_limit_up: bool,
    rules: dict[str, Any],
) -> tuple[bool, str]:
    if price is None:
        return False, "沒有現價"

    kind = signal.get("kind")
    pct = _pct_from_open(price, open_price)

    breakout_from_open_percent = float(rules.get("breakout_from_open_percent", 2.0))
    drop_from_open_percent = float(rules.get("drop_from_open_percent", -2.0))

    if kind == "up_from_open":
        return (pct is not None and pct >= breakout_from_open_percent), "漲幅仍維持在門檻上方"

    if kind == "down_from_open":
        return (pct is not None and pct <= drop_from_open_percent), "跌幅仍維持在門檻下方"

    if kind == "gap_recovery":
        ok, note, _ = _gap_recovery_signal(
            price=price,
            open_price=open_price,
            reference_price=reference_price,
            rules=rules,
        )
        return ok, note

    if kind == "new_high_extension":
        trigger_price = to_float(signal.get("trigger_price"))
        if trigger_price is None:
            return False, "沒有觸發價"
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
    extra_block = f"\n{extra}" if extra else ""

    await send_telegram_message(
        f"【台股 MCP V9 監控】\n"
        f"{title}\n"
        f"{symbol} {name}\n"
        f"訊號：{reason}\n"
        f"現價：{_format_price(price)}\n"
        f"開盤：{_format_price(open_price)}"
        f"{extra_block}\n"
        f"時間：{now.strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def main():
    last_alert_at: dict[str, datetime] = {}
    seen_high: dict[str, float] = {}
    pending_signals: dict[str, dict[str, Any]] = {}
    limit_up_suppressed_date: set[str] = set()

    config = await get_effective_config()
    rules = config.get("rules", {})
    await send_telegram_message(
        "【台股 MCP V9 監控啟動】\n"
        f"監控檔數：{len(config.get('watchlist', []))}\n"
        f"輪詢秒數：{rules.get('poll_seconds', 15)}\n"
        f"訊號模式：{rules.get('signal_mode', 'entry')}\n"
        f"二次確認：{rules.get('confirm_seconds', 45)} 秒\n"
        f"進場區間：{rules.get('entry_min_from_open_percent', 1.2)}%～{rules.get('entry_max_from_open_percent', 5.5)}%\n"
        f"漲停抑制：{rules.get('suppress_limit_up_repeats', True)}\n"
        "通知會附上預估停利/停損。提醒仍是紙上監控，不會下單。"
    )

    print("[monitor] started with entry signal + limit-up suppression + TP/SL", flush=True)

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

            signal_mode = str(rules.get("signal_mode", "entry")).strip().lower()
            if signal_mode not in {"alert", "confirmed", "both", "entry"}:
                signal_mode = "entry"
            confirm_seconds = int(rules.get("confirm_seconds", 45))
            entry_signal_only = bool(rules.get("entry_signal_only", True))

            if not enabled:
                print("[monitor] disabled; sleeping", flush=True)
                await asyncio.sleep(max(poll_seconds, 5))
                continue

            if market_only and not is_tw_market_time():
                now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[monitor] outside market hours {now}; sleeping", flush=True)
                await asyncio.sleep(60)
                continue

            now_date = datetime.now(TAIPEI).strftime("%Y-%m-%d")
            limit_up_suppressed_date = {key for key in limit_up_suppressed_date if key.startswith(now_date + ":")}
            active_pending_keys: set[str] = set()

            for item in watchlist:
                symbol = str(item.get("symbol", "")).strip()
                name = str(item.get("name") or symbol).strip()

                if not symbol:
                    continue

                quote = await safe_quote(symbol)
                price, open_price, high_price, reference_price, is_limit_up = extract_quote_numbers(quote)

                now = datetime.now(TAIPEI)
                print(
                    f"[monitor] {symbol} {name}: price={price}, open={open_price}, high={high_price}, ref={reference_price}, limitUp={is_limit_up}",
                    flush=True,
                )

                limit_state, limit_reason = _is_limit_up_state(
                    price=price,
                    reference_price=reference_price,
                    is_limit_up_flag=is_limit_up,
                    rules=rules,
                )
                if limit_state and bool(rules.get("suppress_limit_up_repeats", True)):
                    for key in list(pending_signals.keys()):
                        if key.startswith(symbol + ":"):
                            pending_signals.pop(key, None)

                    suppress_key = f"{now_date}:{symbol}:limit_up"
                    if bool(rules.get("send_limit_up_notice_once", False)) and suppress_key not in limit_up_suppressed_date:
                        limit_up_suppressed_date.add(suppress_key)
                        await _send_alert(
                            symbol=symbol,
                            name=name,
                            title="接近/達漲停，停止重複進場通知",
                            reason=limit_reason,
                            price=price,
                            open_price=open_price,
                            now=now,
                            extra="提醒：已接近漲停區，系統不再重複發進場候選通知，避免追高。",
                        )
                    await asyncio.sleep(0.2)
                    continue

                signals: list[dict[str, Any]] = []
                pct_from_open = _pct_from_open(price, open_price)

                if pct_from_open is not None:
                    if pct_from_open >= breakout_from_open_percent:
                        signals.append({
                            "kind": "up_from_open",
                            "reason": f"開盤漲幅達 {pct_from_open:.2f}%",
                            "trigger_price": price,
                        })

                    if not entry_signal_only and pct_from_open <= drop_from_open_percent:
                        signals.append({
                            "kind": "down_from_open",
                            "reason": f"開盤跌幅達 {pct_from_open:.2f}%",
                            "trigger_price": price,
                        })

                if price is not None and open_price is not None and reference_price is not None:
                    ok_gap, gap_reason, gap_meta = _gap_recovery_signal(
                        price=price,
                        open_price=open_price,
                        reference_price=reference_price,
                        rules=rules,
                    )
                    if ok_gap:
                        signals.append({
                            "kind": "gap_recovery",
                            "reason": gap_reason,
                            "trigger_price": price,
                            **gap_meta,
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

                if not entry_signal_only and price is not None and stop_loss_price:
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

                    if signal_mode in {"confirmed", "both", "entry"}:
                        if signal_mode == "entry":
                            ok_entry, entry_note = _entry_filter(
                                signal=signal,
                                price=price,
                                open_price=open_price,
                                reference_price=reference_price,
                                is_limit_up=is_limit_up,
                                rules=rules,
                            )
                            if not ok_entry:
                                print(f"[monitor] entry filter rejected {signal_key}: {entry_note}", flush=True)
                                pending_signals.pop(signal_key, None)
                                continue

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

                        valid, note = _signal_still_valid(
                            pending,
                            price,
                            open_price,
                            high_price,
                            reference_price,
                            is_limit_up,
                            rules,
                        )
                        if not valid:
                            print(f"[monitor] pending rejected {signal_key}: {note}", flush=True)
                            pending_signals.pop(signal_key, None)
                            continue

                        title = "二次確認成立，請人工判斷是否可進場"
                        extra = f"確認時間：{confirm_seconds} 秒\n確認說明：{note}\n提醒：這不是自動下單建議，仍需看量價、大盤與停損位置。"

                        if signal_mode == "entry":
                            ok_entry, entry_note = _entry_filter(
                                signal=pending,
                                price=price,
                                open_price=open_price,
                                reference_price=reference_price,
                                is_limit_up=is_limit_up,
                                rules=rules,
                            )
                            if not ok_entry:
                                print(f"[monitor] entry confirmation rejected {signal_key}: {entry_note}", flush=True)
                                pending_signals.pop(signal_key, None)
                                continue

                            plan = _calc_trade_plan(price, open_price, rules)
                            title = "開低走高進場候選通知，請人工確認" if str(pending.get("kind")) == "gap_recovery" else "進場候選通知，請人工確認"
                            extra = (
                                f"確認時間：{confirm_seconds} 秒\n"
                                f"進場條件：{entry_note}\n"
                                f"預估進場參考：{_format_price(plan.get('entry'))}\n"
                                f"預估停損：{_format_price(plan.get('stop_loss'))}\n"
                                f"預估停利：{_format_price(plan.get('take_profit'))}\n"
                                f"單股風險：約 {_format_price(plan.get('risk_per_share'))}\n"
                                f"風報比：約 1 : {plan.get('risk_reward')}\n"
                                "提醒：這是紙上監控候選點，不會下單；請再看大盤、量價與券商 App 報價。"
                            )

                        alert_key = f"{signal_key}:{signal_mode}"
                        last_at = last_alert_at.get(alert_key)
                        if last_at and (now - last_at).total_seconds() < cooldown_seconds:
                            continue

                        last_alert_at[alert_key] = now
                        pending_signals.pop(signal_key, None)

                        await _send_alert(
                            symbol=symbol,
                            name=name,
                            title=title,
                            reason=str(pending.get("reason")),
                            price=price,
                            open_price=open_price,
                            now=now,
                            extra=extra,
                        )

                await asyncio.sleep(0.2)

            current_symbols = {str(item.get("symbol", "")).strip() for item in watchlist}
            for key in list(pending_signals.keys()):
                symbol = key.split(":", 1)[0]
                if symbol in current_symbols and key not in active_pending_keys:
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
