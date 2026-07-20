# V10.5 無 Background Worker 版本

此版本不使用、不修改 `monitor_worker.py`。

## 每日更新方式

### 手動 MCP

執行：

```text
run_v10_daily_maintenance
```

它會依序：

1. 更新 TWSE／TPEx 當日日 K
2. 計算技術指標
3. 執行三種雷達並保存結果
4. 更新 1／3／5／10／20 日績效

### Render Shell

```bash
python scripts/daily_maintenance.py
```

### 未來需要全自動

可建立 Render Cron Job，命令同樣是：

```bash
python scripts/daily_maintenance.py
```

目前 `STOCK_DB_DAILY_UPDATE` 不需要啟用，建議維持 `false`。

## Render 環境變數

```text
DATABASE_URL=<Internal Database URL>
STOCK_DB_ENABLED=true
STOCK_DB_READ_PREFERRED=true
STOCK_DB_DAILY_UPDATE=false
STOCK_DB_FALLBACK_ENABLED=true
STOCK_DB_HISTORY_YEARS=3
STOCK_DB_POOL_MIN=1
STOCK_DB_POOL_MAX=3
FINMIND_TOKEN=<既有 Token>
```
