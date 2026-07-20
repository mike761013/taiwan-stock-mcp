# 台股 MCP V10 完整安裝說明

## 目的

V10 新增 PostgreSQL 歷史行情、技術指標、雷達執行紀錄及成效追蹤基礎，
同時保留現有 Fugle、FinMind、TWSE、TPEx、Redis 與 Telegram 流程。

## 安裝

1. 確認 GitHub Desktop 目前分支為 `feature/v10-postgres-foundation`。
2. 將本套件全部內容複製到 repository 根目錄。
3. 雙擊 `install_v10.bat`。
4. 回 GitHub Desktop 檢查變更。
5. Commit 訊息：`Add complete V10 PostgreSQL subsystem`
6. Push origin。

安裝器會備份原本 `server.py` 與 `requirements.txt` 到 `.v10_backup/`。

## Render 第一階段安全設定

```text
DATABASE_URL=<Render Internal Database URL>
STOCK_DB_ENABLED=false
STOCK_DB_READ_PREFERRED=false
STOCK_DB_DAILY_UPDATE=false
STOCK_DB_FALLBACK_ENABLED=true
STOCK_DB_HISTORY_YEARS=3
STOCK_DB_POOL_MIN=1
STOCK_DB_POOL_MAX=3
STOCK_DB_STATEMENT_TIMEOUT_SECONDS=30
```

先部署並確認原有功能正常，再把 `STOCK_DB_ENABLED` 改成 `true`。

## 初始化

啟用後可透過 MCP 執行：

- `initialize_stock_database`
- `get_stock_database_health`
- `get_stock_database_statistics`

或在 Render Shell 執行：

```bash
python scripts/init_stock_database.py
```

## 匯入日K

支援 CSV 或 JSON。必要欄位：

```text
symbol,date,open,high,low,close,volume
```

執行：

```bash
python scripts/import_daily_bars.py data.csv --market TWSE
python scripts/calculate_indicators.py 2330
```

## 回滾

執行：

```bash
python uninstall_v10.py
```

或把 Render 的 `STOCK_DB_ENABLED` 改回 `false`。即使 PostgreSQL 無法連線，
現有 MCP 仍會啟動，因為 V10 工具註冊已包在安全的 try/except 中。

## 尚未自動化的部分

本包已完成資料庫、匯入器、指標、雷達保存與 MCP 管理工具。
全市場三年歷史資料來源牽涉 Fugle 方案與官方端點限制，因此沒有在未知權限下
強行自動大量抓取。可先用現有資料匯出成 CSV/JSON 匯入；後續再依你實際可用
的 Fugle API 權限加上自動回補器。
