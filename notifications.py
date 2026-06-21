import os
from datetime import datetime, timezone
from typing import Any

import httpx


TELEGRAM_API_BASE = "https://api.telegram.org"


def _read_secret_file_value(key: str) -> str:
    """Read KEY=value from Render Secret File fallback, if available."""
    candidates = [
        "/etc/secrets/telegram.env",
        "/etc/secrets/telegram_env",
        "telegram.env",
        "telegram_env",
    ]

    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
        except FileNotFoundError:
            continue
        except Exception:
            continue

    return ""


def _get_secret_value(key: str) -> str:
    """Prefer Render env var, fallback to Render Secret File."""
    return os.environ.get(key, "").strip() or _read_secret_file_value(key)


def _telegram_token() -> str:
    token = _get_secret_value("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("尚未設定 TELEGRAM_BOT_TOKEN。請把 Token 放到 Render Environment。")
    return token


def _telegram_chat_id() -> str:
    chat_id = _get_secret_value("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("尚未設定 TELEGRAM_CHAT_ID。請把 chat id 放到 Render Environment。")
    return chat_id


def build_test_message() -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"【台股 MCP V9 測試】\nTelegram 手機通知已連線成功。\n時間：{now}"


build_telegram_test_message = build_test_message
make_test_message = build_test_message


async def send_telegram_message(
    text: str,
    *,
    disable_web_page_preview: bool = True,
) -> dict[str, Any]:
    """Send a plain-text Telegram message using Bot API."""
    token = _telegram_token()
    chat_id = _telegram_chat_id()

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json=payload)

    try:
        data = response.json()
    except Exception:
        data = {"ok": False, "raw": response.text}

    if response.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Telegram 發送失敗：HTTP {response.status_code} {data}")

    return data


async def get_telegram_updates(limit: int = 5) -> dict[str, Any]:
    token = _telegram_token()
    url = f"{TELEGRAM_API_BASE}/bot{token}/getUpdates"

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params={"limit": limit, "timeout": 0})

    try:
        data = response.json()
    except Exception:
        data = {"ok": False, "raw": response.text}

    if response.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates 失敗：HTTP {response.status_code} {data}")

    return data
