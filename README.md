# 台股 MCP v3：即時報價、技術面與籌碼面

## 原有工具

- `ping`
- `get_realtime_quote`
- `get_historical_candles`
- `get_technical_summary`

## 新增籌碼工具

- `get_institutional_trades`
  - 外資、投信、自營商買賣超
  - 近 5／10／20 個交易日累計

- `get_margin_short`
  - 融資融券買賣與餘額
  - 每日增減與期間變化

- `get_foreign_shareholding`
  - 外資持股股數與比例
  - 查詢期間比例變化

- `get_securities_lending`
  - 借券成交量
  - 加權平均借券費率
  - 注意：不是借券賣出餘額

- `get_shareholding_distribution`
  - 股權持股分級
  - 百張以下持股比例
  - 需要 FinMind backer 或 sponsor 方案

- `get_chip_summary`
  - 一次取得法人、融資券、外資持股、借券成交摘要

## Render 環境變數

原本的：

`FUGLE_API_KEY=你的 Fugle API Key`

新增：

`FINMIND_TOKEN=你的 FinMind Token`

## 更新方式

1. 解壓縮本 ZIP。
2. 到原本的 GitHub Repository。
3. 上傳並覆蓋 `server.py`、`requirements.txt`、`README.md`、`.gitignore`。
4. Commit changes。
5. 等 Render 顯示 Deploy live。
6. 在 Render 的 Environment 新增 `FINMIND_TOKEN`。
7. 回 ChatGPT App 開發者模式重新整理工具；若舊對話未更新，開新對話並重新選取 App。
