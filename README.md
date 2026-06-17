# 台股 MCP V5：全市場選股＋智慧快取

保留 V4 的全部功能，新增兩層快取與管理工具。

## 新增功能

- 全市場選股 `screen_market`
- 記憶體 TTL 快取（不需新增設定）
- 可選 Redis／Render Key Value 共用快取
- 同一資料的併發請求只打一次上游 API（防止 cache stampede）
- `force_refresh=true` 可強制刷新全市場選股資料
- `get_cache_status` 查看命中率、實際 API 呼叫數、估計省下的呼叫數
- `clear_cache` 可清除指定快取

## 快取策略

- 即時報價：盤中 10 秒
- 全市場快照：盤中 30 秒
- 歷史日 K：盤中 5 分鐘；盤後快取至下一個工作日 08:30
- FinMind 法人／融資／外資／借券：更新時段 20 分鐘，其餘 2～10 小時
- 股權分散：24 小時

## 環境變數

必要：

- `FUGLE_API_KEY`
- `FINMIND_TOKEN`

選用：

- `REDIS_URL`：Render Key Value 的 Internal URL
- `CACHE_PREFIX`：預設 `twstock:mcp:v5`
- `CACHE_MAX_ITEMS`：記憶體快取上限，預設 2500

沒有 `REDIS_URL` 也能運作，但免費 Render 休眠、重啟或重新部署後，記憶體快取會消失。

## 更新方式

1. 解壓縮 ZIP。
2. 到原 GitHub Repository。
3. 上傳並覆蓋 `server.py`、`requirements.txt`、`README.md`、`.gitignore`。
4. Commit changes。
5. 等 Render 顯示 Deploy live。
6. 回 ChatGPT App 重新整理工具。

## 建議測試

- `使用 get_cache_status 查看快取狀態`
- `使用 screen_market，以 early_stage 策略找前 10 名，candidate_limit=40`
- 立刻重跑同一條件，再查看 `get_cache_status`，應看到 cache hits 增加。
- 需要全新資料時，把 `force_refresh` 設為 `true`。
