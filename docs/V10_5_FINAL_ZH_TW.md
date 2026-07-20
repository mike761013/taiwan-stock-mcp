# 台股 MCP V10.5 完整測試版

本版直接覆蓋 V10 Foundation，完成：

- PostgreSQL schema、連線池與批次 upsert
- TWSE/TPEx 股票清單同步
- FinMind 三年歷史日K回補
- 官方 TWSE/TPEx 每日增量更新
- MA、布林、量比、波動度、大量低點、技術分數
- PostgreSQL 全市場 early_stage / breakout / pullback 雷達
- 三策略合併的全方位看漲雷達
- 雷達歷史、候選快照與 1/3/5/10/20 日績效
- Background Worker 15:30 後每日自動更新
- MCP 初始化、回補、更新、雷達、績效工具
- 資料庫故障不阻止舊 MCP 啟動

## 套用

把套件全部覆蓋到 repository 根目錄，雙擊 `install_v10_5.bat`，
回 GitHub Desktop 做第二次 Commit 並 Push。第一次 Foundation Commit 不必撤銷。

## Render 測試環境

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

先保持 `STOCK_DB_DAILY_UPDATE=false`，依序測試：

1. `initialize_stock_database`
2. `sync_stock_security_master`
3. `backfill_stock_history("2330,2313,4977", years=3)`
4. `get_stock_database_statistics`
5. `screen_market_v10`
6. `run_full_bullish_radar_v10`
7. `update_radar_signal_performance`

小範圍測試正常後，再執行 `backfill_all_stock_history`。
全市場完成後才把 `STOCK_DB_DAILY_UPDATE=true`。

## 重要限制

全市場三年回補速度與成功率受 FinMind 會員方案、速率限制和 Render
免費資料庫容量影響。程式支援分批與 `start_after` 續傳，但無法在尚未部署前
保證免費 1 GB 一定能容納完整三年全市場資料。
