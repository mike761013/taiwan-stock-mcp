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

## V12.4 永久持股帳本

帳本寫入既有 PostgreSQL，交易與原始進場策略分開保存。原始策略採
append-only；新的收盤技術位階只能拿來比較，不能覆寫原計畫。

- `record_portfolio_trade`：新增買賣，現股／融資、一般／定期定額分開。
- `record_position_plan`：保存訊號來源、買點、上限、部位與明確停損。
- `get_portfolio_positions`：查 FIFO 批次、最新損益及原始計畫稽核。
- `get_portfolio_history`：查交易、配對與策略歷史。
- `void_latest_portfolio_trade`：保留稽核軌跡地更正最新交易。

成本預設採國泰電子下單 28 折：手續費 0.0399%；普通股票賣出稅
0.3%、股票當沖 0.15%、ETF 0.1%。同日交易先採最有利已實現損益
配對，剩餘庫存再依 FIFO；融資利率預設年息 6.45%。

## V12.4 績效與弱勢盤修正

- 每週主績效只計「正式進場」，「小部位試單」與「等待觀察」另列；策略名稱
  對外顯示中文，但保留穩定英文代碼供程式查詢。
- 同日同股同時存在單策略初篩與完整因子合併快照時，以後者的最終操作分層
  為準，避免初篩「可買」覆蓋完整檢查後的「觀察」。
- 執行績效可依候選快照內所有命中策略分組，不再只依主策略統計。
- 市場寬度偏弱時，放量突破、反轉與多頭初升段須同時通過較高品質及強產業
  門檻，否則降為觀察，不會從候選清單消失。
- `V12.4-NET-EXECUTION-2` 於進場期結束後採 1R 停利 50%、2R 停利
  25%，其餘以 1R 移動停損；仍保留收盤失敗後隔日開盤退出及完整交易成本。
- 週報參數可使用對外版本名 `V12.4`（亦相容 `V12`）；同一服務程序內的資料表
  遷移會序列化且只執行一次，避免同時查詢造成鎖衝突。
