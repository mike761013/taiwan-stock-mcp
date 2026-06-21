# Taiwan Stock MCP V9 - Monitor Alerts

V9 在 V8 勝率追蹤器上新增：

- Telegram 手機通知測試
- 盤中監測 Background Worker
- 監測設定檔 monitor_rules.json
- 下單預覽 preview_order（只計算，不送單）

## 主要檔案

- `server.py`：ChatGPT MCP 主服務，新增 `send_test_notification`、`get_telegram_setup_status`、`get_monitor_config`、`preview_order`
- `notifications.py`：Telegram Bot 發送通知
- `monitor_worker.py`：Render Background Worker 使用，盤中監測最多 5 檔
- `monitor_rules.json`：監測清單與條件
- `order_preview.py`：下單預覽成本與風險試算

## Web Service Start Command

```bash
python server.py
```

## Background Worker Start Command

```bash
python monitor_worker.py
```

## 必要環境變數

原本 V8 已有：

```text
FUGLE_API_KEY
FINMIND_TOKEN
REDIS_URL
TZ=Asia/Taipei
```

V9 手機通知新增：

```text
TELEGRAM_BOT_TOKEN=你的 BotFather token
TELEGRAM_CHAT_ID=你的 chat id
```

V9 盤中監測 Background Worker 建議：

```text
MONITOR_ENABLED=true
MONITOR_MODE=paper
MONITOR_WATCHLIST=2313,4977,6213,3583,2408
MONITOR_POLL_SECONDS=15
ALERT_COOLDOWN_SECONDS=300
MONITOR_MARKET_ONLY=true
SEND_STARTUP_NOTIFICATION=true
```

## 注意

免費 Fugle 方案建議最多監測 5 檔。V9 預設只發通知，不會也不能送出券商委託。
