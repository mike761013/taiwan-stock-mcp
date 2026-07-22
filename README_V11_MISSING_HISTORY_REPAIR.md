# V11 缺漏歷史資料修復

這個修復流程不再依靠 `start_after`。

它會直接查 PostgreSQL 中 `is_active=true` 且 `daily_bars=0` 的股票，
只補這些缺漏股票。重新執行時會再次查資料庫，所以已修好的股票不會重跑。

## 安裝

覆蓋或新增：

- `.github/workflows/repair-missing-stock-history.yml`
- `scripts/repair_missing_stock_history.py`

Commit 並 Push 到 main。

## 執行參數

- years: `1`
- request_delay_seconds: `10`
- retries: `3`

遇到 402/403 會立刻保存報告並停止。等待額度恢復後，
重新執行相同 Workflow 即可；不需要填續傳代號。
