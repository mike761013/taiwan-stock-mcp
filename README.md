# 台股 MCP V7.1：免費官方雙來源同日篩選

V7.1 保留 V7 的免費全市場掃描，並新增 TWSE／TPEx 第二官方盤後端點。
主要 OpenAPI 日期不同或落後時，程式會自動嘗試指定日期備援，不需要任何新 API Key。

## 行情流程

1. 主要上市來源：TWSE OpenAPI `STOCK_DAY_ALL`
2. 主要上櫃來源：TPEx OpenAPI `tpex_mainboard_daily_close_quotes`
3. 若主要來源日期不同，或落後 Fugle 參考交易日：
   - 上市備援：TWSE `MI_INDEX` 指定日期盤後行情
   - 上櫃備援：TPEx `dailyQuotes` 指定日期盤後行情
4. 備援資料必須通過：
   - 回傳日期等於目標交易日
   - 普通股家數高於安全門檻
   - 股票代號不重複
   - OHLC 完整率至少 80%
   - 成交量／成交值完整率至少 75%
5. 全市場日期一致後，才進行多通道初篩。
6. 候選股以 Fugle 歷史 K 線做深度技術分析，並併入當日官方 K 棒。
7. `include_chip=true` 時，只對技術前 `top_n` 檔補 FinMind 籌碼。

若主要與備援都無法對齊日期，仍會拒絕排名，不會混用不同交易日資料。

## 版本辨識

`ping` 應回傳：

```text
Taiwan Stock MCP v7.1-free-official-fallback
version=7.1.0-free
```

成功篩選時新增：

- `primaryMarketDates`
- `finalMarketDates`
- `fallbackUsed`
- `fallbackMarkets`
- `fallbackAttempts`
- `asOf[].primarySource`
- `asOf[].source`
- `asOf[].validation`

## Render 環境變數

沿用 V7 原設定即可，不需新增官方 API Key：

```text
FUGLE_API_KEY
FINMIND_TOKEN
REDIS_URL（如有）
PYTHON_VERSION=3.12.13
V7_HISTORY_CALLS_PER_MINUTE=55
V7_HISTORY_CONCURRENCY=3
V7_CHIP_CONCURRENCY=2
V7_MIN_UNIVERSE_COUNT=1800
V7_MIN_TSE_COUNT=950
V7_MIN_OTC_COUNT=750
V7_REFERENCE_SYMBOL=2330
```

## 更新既有 V7

1. 解壓縮 ZIP。
2. 到目前 Render 服務所使用的 GitHub 分支 `v7-free-official-same-day`。
3. 上傳並覆蓋根目錄檔案。
4. Commit changes。
5. Render 通常會自動重新部署；沒有自動部署時手動執行 Deploy latest commit。
6. MCP URL 不變，ChatGPT App 不必重建。
7. 先執行 `PING`，確認版本為 `7.1.0-free`。

## 測試指令

```text
使用 screen_market，
strategy=early_stage，
markets=BOTH，
top_n=10，
candidate_limit=40，
include_chip=true，
force_refresh=true
```

若備援成功，結果應顯示：

```text
ok=true
fallbackUsed=true
fallbackMarkets=["TSE"] 或 ["OTC"]
primaryMarketDates 與 finalMarketDates 不同
allUniverseUsingSameDate=true
```

若備援端點也尚未發布完整資料，會保留 `MARKET_DATE_MISMATCH` 或
`OFFICIAL_DATA_NOT_LATEST` 防呆。

## 離線測試

```bash
python test_v71_free.py
```
