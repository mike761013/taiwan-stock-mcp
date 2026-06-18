# 台股 MCP V7 免費版：官方同日全市場篩選

版本：`Taiwan Stock MCP v7-free-official-same-day`（`7.0.0-free`）

這個版本不使用 Fugle 付費的全市場 Snapshot Quotes，適合 Fugle 免費基本方案與 Render 免費 Web Service。

## V7 解決的問題

V6 可能發生：上市資料仍是前一交易日、上櫃資料已更新到最新交易日，卻仍混合產生排名。這會讓前一日漲幅被誤認成當日漲幅，也可能漏掉當日才發動的股票。

V7 會：

1. 取得 TWSE 與 TPEx 官方全市場普通股日行情。
2. 將民國日期統一轉為 ISO 日期（例如 `1150618` → `2026-06-18`）。
3. 上市、上櫃日期不同時拒絕排名。
4. 用 Fugle 2330 報價日期確認官方資料是否已更新到最新交易日。
5. 全市場先使用同日行情做多通道初篩，而不是只按漲幅或成交值排序。
6. 對 `candidate_limit` 檔（預設 40）呼叫 Fugle 免費歷史 K 線。
7. 把官方當日 K 棒併入歷史資料後，重新計算均線、布林、量比與前高。
8. 只對技術初評前 `top_n` 檔補法人與融資籌碼。
9. 回傳 `scoringDate`、`referenceDate`、`allUniverseUsingSameDate` 與市場家數供驗證。

## 免費版資料流程

```text
TWSE + TPEx 官方同日全市場行情
→ 全市場價格／成交值過濾
→ 多通道候選池（預設 40 檔）
→ Fugle 免費歷史 K 線
→ 合併官方當日 K 棒並重算技術指標
→ 前 10 檔補 FinMind 籌碼
→ 輸出前 10 名
```

## Render 必要環境變數

保留原本：

- `FUGLE_API_KEY`
- `FINMIND_TOKEN`
- `REDIS_URL`（沒有 Redis 可以不設）

新增／確認：

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

## Render 指令

沿用原本 V6：

```text
Build Command: pip install -r requirements.txt
Start Command: python server.py
```

若你目前 Render 的 Start Command 不同，請以原本能運作的 V6 設定為準。

## 使用方式

```text
使用 screen_market，
strategy=early_stage，
markets=BOTH，
top_n=10，
candidate_limit=40，
include_chip=true，
force_refresh=false
```

第一次部署後建議先用：

```text
force_refresh=true
```

## 成功回傳應包含

```json
{
  "ok": true,
  "serverVersion": "v7-free-official-same-day",
  "scoringDate": "2026-06-18",
  "referenceDate": "2026-06-18",
  "allUniverseUsingSameDate": true,
  "marketUniverseCount": 1900,
  "technicalCandidateLimit": 40,
  "deepAnalyzedCount": 40,
  "chipAnalyzedCount": 10
}
```

## 正常的拒絕情況

### 上市與上櫃日期不同

```text
errorCode=MARKET_DATE_MISMATCH
```

### 官方資料尚未追上 Fugle 最新交易日

```text
errorCode=OFFICIAL_DATA_NOT_LATEST
```

### 官方普通股家數異常過少

```text
errorCode=MARKET_UNIVERSE_INCOMPLETE
```

這些不是程式故障，而是 V7 的資料防呆。等官方資料更新後再執行即可。

## 測試

```bash
python test_v7_free.py
```

應看到 5 個 `PASS`。
