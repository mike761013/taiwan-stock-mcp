# V7 免費版部署步驟（依你的原始專案客製）

你的專案只有主要檔案：`server.py`、`requirements.txt`、`README.md`。V7 已直接整合進 `server.py`，不需要額外 adapter。

## 1. GitHub 建立分支

在 GitHub 專案頁：

1. 點左上方目前分支 `main`。
2. 輸入 `v7-free-official-same-day`。
3. 點 `Create branch: v7-free-official-same-day from main`。

## 2. 上傳 V7 檔案

解壓縮本 ZIP，將以下檔案上傳並覆蓋到新分支根目錄：

- `server.py`
- `requirements.txt`
- `README.md`
- `.gitignore`
- `.python-version`
- `test_v7_free.py`（可留著作測試）

Commit message：

```text
Upgrade to v7 free same-day screener
```

## 3. 建立 Render 測試服務

不要先覆蓋目前 V6。

1. Render Dashboard → `New` → `Web Service`。
2. 選同一個 GitHub repository。
3. Name：`taiwan-stock-mcp-v7-free`。
4. Branch：`v7-free-official-same-day`。
5. Instance Type：`Free`。
6. Build Command：複製 V6；本專案通常是 `pip install -r requirements.txt`。
7. Start Command：複製 V6；本專案程式可用 `python server.py`。

## 4. 複製 Render Environment

從 V6 複製：

```text
FUGLE_API_KEY
FINMIND_TOKEN
REDIS_URL（如有）
```

再新增：

```text
PYTHON_VERSION=3.12.13
V7_HISTORY_CALLS_PER_MINUTE=55
V7_HISTORY_CONCURRENCY=3
V7_CHIP_CONCURRENCY=2
V7_MIN_UNIVERSE_COUNT=1800
V7_MIN_TSE_COUNT=950
V7_MIN_OTC_COUNT=750
V7_REFERENCE_SYMBOL=2330
```

儲存時選 `Save, rebuild, and deploy`。

## 5. 檢查 Render Logs

成功時應看到服務啟動，且沒有：

- `ModuleNotFoundError`
- `SyntaxError`
- `FUGLE_API_KEY` 缺少
- `FINMIND_TOKEN` 缺少

## 6. ChatGPT 新增 V7 MCP

Render 網址假設為：

```text
https://taiwan-stock-mcp-v7-free.onrender.com
```

MCP URL：

```text
https://taiwan-stock-mcp-v7-free.onrender.com/mcp
```

先建立新的 App／MCP，不要刪除 V6。

## 7. 測試 ping

執行 `ping`，應回傳：

```text
Taiwan Stock MCP v7-free-official-same-day
version=7.0.0-free
```

## 8. 第一次篩選

```text
使用 screen_market，
strategy=early_stage，
markets=BOTH，
top_n=10，
candidate_limit=40，
include_chip=true，
force_refresh=true
```

成功時確認：

- `ok=true`
- `allUniverseUsingSameDate=true`
- `scoringDate=referenceDate`
- `deepAnalyzedCount` 接近 40
- 所有結果 `quoteDate=scoringDate`
- 所有結果 `technical.latestDate=scoringDate`

## 9. 若被拒絕排名

`MARKET_DATE_MISMATCH` 或 `OFFICIAL_DATA_NOT_LATEST` 代表官方資料還沒更新完整。等數分鐘後使用 `force_refresh=true` 再試。V7 在收盤後 18:30 前把官方行情快取縮短為 5 分鐘，避免舊資料被鎖到隔天。

## 10. 切換正式服務

V7 連續測試正常後：

1. ChatGPT 停用 V6 App。
2. Render 暫停 V6 服務，保留 GitHub `main` 分支作為回復版本。
3. 日後只需說「執行股票篩選」。
