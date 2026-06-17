# 台股 MCP V6：免費全市場初篩＋快取

V6 不再使用 Fugle `Snapshot Quotes`，因此不需要為了全市場選股升級
Fugle 開發者／進階方案。

## 資料流程

1. 上市候選池：證交所 OpenAPI 最新全市場日行情
2. 上櫃候選池：櫃買中心 OpenAPI 最新全市場日行情
3. 依成交值、價格與當日漲跌預篩
4. 候選股的 180 日 K 線：沿用 Fugle 個股歷史行情
5. `include_chip=true`：入選股再用 FinMind 補法人與融資券

官方全市場資料不是盤中逐筆即時 Snapshot。收盤後執行最完整；
盤中執行時，結果依官方端點當下最新公布批次。

## 原有功能保留

- 即時報價
- 歷史 K 線
- 均線、布林通道、量比
- 三大法人、融資融券、外資持股、借券
- 股權分散
- 單檔完整分析
- 指定清單排名
- Redis／記憶體快取
- 快取狀態與清除

## Render 環境變數

保留：

- `FUGLE_API_KEY`
- `FINMIND_TOKEN`
- `REDIS_URL`（已設定 Redis 才需要）

不需要新增證交所或櫃買中心 API Key。

## 更新方式

1. 解壓縮 ZIP。
2. 到原本 GitHub Repository。
3. 覆蓋 `server.py`、`requirements.txt`、`README.md`、`.gitignore`。
4. Commit changes。
5. 等 Render 顯示 `Deploy live`。
6. ChatGPT → Apps → 台股 App → Refresh。
7. 建議開新對話重新選取 App。

## 測試

```text
使用 screen_market，
strategy=early_stage，
markets=BOTH，
top_n=10，
candidate_limit=40，
include_chip=true，
force_refresh=false
```

再執行：

```text
使用 get_cache_status 查看快取狀態
```

## 清除全市場快取

```text
使用 clear_cache，scope=market
```

舊的 `scope=snapshot` 仍保留相容性，也會清除官方全市場資料快取。
