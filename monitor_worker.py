import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from notifications import send_telegram_message

TAIPEI_TZ = ZoneInfo("Asia/Taipei")
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
RULES_FILE = Path(os.environ.get("MONITOR_RULES_FILE", "monitor_rules.json"))


def _api_key() -> str:
    key = os.environ.get("FUGLE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("尚未設定 FUGLE_API_KEY。")
    return key


def _load_rules() -> dict[str, Any]:
    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    return {"enabled": True, "watchlist": [], "rules": {}}


def _env_watchlist() -> list[dict[str, Any]]:
    raw = os.environ.get("MONITOR_WATCHLIST", "").strip()
    if not raw:
        return []
    rows = []
    for item in raw.split(","):
        symbol = item.strip().upper()
        if re.fullmatch(r"[0-9A-Z]{4,7}", symbol):
            rows.append({"symbol": symbol, "name": ""})
    return rows


def _watchlist(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _env_watchlist() or config.get("watchlist") or []
    clean = []
    seen = set()
    for row in rows:
        if isinstance(row, str):
            symbol = row.strip().upper()
            row = {"symbol": symbol, "name": ""}
        symbol = str(row.get("symbol", "")).strip().upper()
        if not re.fullmatch(r"[0-9A-Z]{4,7}", symbol):
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        clean.append(row | {"symbol": symbol})
    return clean[:5]


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def _price_from_quote(q: dict[str, Any]) -> float | None:
    last_trade = q.get("lastTrade") if isinstance(q.get("lastTrade"), dict) else {}
    for key in ["lastPrice", "closePrice", "avgPrice", "price"]:
        value = _to_float(q.get(key))
        if value and value > 0:
            return value
    value = _to_float(last_trade.get("price"))
    if value and value > 0:
        return value
    return None


def _volume_from_quote(q: dict[str, Any]) -> int | None:
    total = q.get("total") if isinstance(q.get("total"), dict) else {}
    for source in [total, q]:
        for key in ["tradeVolume", "volume", "totalVolume"]:
            value = _to_float(source.get(key))
            if value is not None:
                return int(value)
    return None


def _base_from_quote(q: dict[str, Any], price: float) -> float:
    for key in ["openPrice", "referencePrice", "previousClose"]:
        value = _to_float(q.get(key))
        if value and value > 0:
            return value
    return price


def _market_is_open() -> bool:
    now = datetime.now(TAIPEI_TZ)
    if now.weekday() >= 5:
        return False
    return dt_time(9, 0) <= now.time() <= dt_time(13, 35)


async def _fugle_quote(symbol: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(
            f"{FUGLE_BASE_URL}/intraday/quote/{symbol}",
            headers={"X-API-KEY": _api_key()},
        )
    if response.status_code == 429:
        raise RuntimeError("Fugle API 達到頻率上限，請調高 MONITOR_POLL_SECONDS。")
    if response.status_code == 403:
        raise RuntimeError("目前 Fugle 方案沒有此資料權限。")
    response.raise_for_status()
    return response.json()


@dataclass
class SymbolState:
    base_price: float | None = None
    high_seen: float | None = None
    low_seen: float | None = None
    last_volume: int | None = None
    last_alert_at: dict[str, float] = field(default_factory=dict)


class Monitor:
    def __init__(self):
        self.config = _load_rules()
        self.rules = self.config.get("rules") or {}
        self.watchlist = _watchlist(self.config)
        self.states: dict[str, SymbolState] = {str(row["symbol"]): SymbolState() for row in self.watchlist}
        self.poll_seconds = int(os.environ.get("MONITOR_POLL_SECONDS") or self.rules.get("poll_seconds") or 15)
        self.cooldown_seconds = int(os.environ.get("ALERT_COOLDOWN_SECONDS") or self.rules.get("cooldown_seconds") or 300)
        self.market_only = str(os.environ.get("MONITOR_MARKET_ONLY", self.rules.get("market_only", True))).lower() not in {"0", "false", "no"}
        self.breakout_pct = float(os.environ.get("MONITOR_BREAKOUT_PCT") or self.rules.get("breakout_from_open_percent") or 2.0)
        self.drop_pct = float(os.environ.get("MONITOR_DROP_PCT") or self.rules.get("drop_from_open_percent") or -2.0)
        self.new_high_extension_pct = float(os.environ.get("MONITOR_NEW_HIGH_EXTENSION_PCT") or self.rules.get("new_high_extension_percent") or 0.8)

    async def send_startup(self):
        if str(os.environ.get("SEND_STARTUP_NOTIFICATION", "true")).lower() in {"0", "false", "no"}:
            return
        symbols = ", ".join(row["symbol"] for row in self.watchlist) or "尚未設定"
        msg = (
            "【台股 MCP V9 監測啟動】\n"
            f"時間：{datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"監測清單：{symbols}\n"
            f"輪詢秒數：{self.poll_seconds}\n"
            "模式：paper，只發通知，不會下單。"
        )
        await send_telegram_message(msg)

    def _cooldown_ok(self, symbol: str, key: str) -> bool:
        now = datetime.now(TAIPEI_TZ).timestamp()
        state = self.states.setdefault(symbol, SymbolState())
        last = state.last_alert_at.get(key, 0)
        if now - last < self.cooldown_seconds:
            return False
        state.last_alert_at[key] = now
        return True

    async def check_symbol(self, row: dict[str, Any]):
        symbol = row["symbol"]
        name = row.get("name") or ""
        q = await _fugle_quote(symbol)
        price = _price_from_quote(q)
        if not price:
            return
        volume = _volume_from_quote(q)
        state = self.states.setdefault(symbol, SymbolState())
        if state.base_price is None:
            state.base_price = _base_from_quote(q, price)
        previous_high = state.high_seen
        if state.high_seen is None or price > state.high_seen:
            state.high_seen = price
        if state.low_seen is None or price < state.low_seen:
            state.low_seen = price

        alerts = []
        base = state.base_price or price
        chg_pct = (price / base - 1) * 100 if base else 0

        breakout_price = _to_float(row.get("breakout_price"))
        stop_loss_price = _to_float(row.get("stop_loss_price"))
        if breakout_price and price >= breakout_price and self._cooldown_ok(symbol, "fixed_breakout"):
            alerts.append(f"突破指定價 {breakout_price}")
        if stop_loss_price and price <= stop_loss_price and self._cooldown_ok(symbol, "fixed_stop"):
            alerts.append(f"跌破指定停損 {stop_loss_price}")
        if chg_pct >= self.breakout_pct and self._cooldown_ok(symbol, "open_breakout"):
            alerts.append(f"較開盤/參考價上漲 {chg_pct:.2f}%")
        if chg_pct <= self.drop_pct and self._cooldown_ok(symbol, "open_drop"):
            alerts.append(f"較開盤/參考價下跌 {chg_pct:.2f}%")
        if previous_high and price >= previous_high * (1 + self.new_high_extension_pct / 100) and self._cooldown_ok(symbol, "new_high_extension"):
            alerts.append(f"盤中新高再延伸 {self.new_high_extension_pct:.2f}%")

        if not alerts:
            state.last_volume = volume
            return
        msg = (
            f"【盤中訊號】{name} {symbol}\n"
            f"時間：{datetime.now(TAIPEI_TZ).strftime('%H:%M:%S')}\n"
            f"現價：{price}\n"
            f"基準價：{base}\n"
            f"漲跌幅：{chg_pct:.2f}%\n"
            f"今日觀察高/低：{state.high_seen} / {state.low_seen}\n"
            f"成交量：{volume if volume is not None else 'N/A'}\n"
            f"觸發：{'; '.join(alerts)}\n\n"
            "提醒：這是紙上監測通知，不會下單。請回 ChatGPT 做下單預覽，或自行到券商 App 確認。"
        )
        await send_telegram_message(msg)
        state.last_volume = volume

    async def run_forever(self):
        if not self.watchlist:
            raise RuntimeError("沒有監測清單。請設定 MONITOR_WATCHLIST 或 monitor_rules.json。")
        await self.send_startup()
        while True:
            try:
                if self.market_only and not _market_is_open():
                    await asyncio.sleep(60)
                    continue
                tasks = [self.check_symbol(row) for row in self.watchlist]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        print(f"[monitor] symbol check error: {result}", flush=True)
                await asyncio.sleep(max(5, self.poll_seconds))
            except Exception as exc:
                print(f"[monitor] loop error: {exc}", flush=True)
                try:
                    await send_telegram_message(f"【台股 MCP V9 監測錯誤】\n{exc}")
                except Exception as notify_exc:
                    print(f"[monitor] notify error: {notify_exc}", flush=True)
                await asyncio.sleep(60)


async def main():
    config = _load_rules()
    if str(os.environ.get("MONITOR_ENABLED", config.get("enabled", True))).lower() in {"0", "false", "no"}:
        print("MONITOR_ENABLED=false，監測未啟動。", flush=True)
        while True:
            await asyncio.sleep(300)
    monitor = Monitor()
    await monitor.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
