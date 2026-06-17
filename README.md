# 台股 MCP v2

新增功能：

- `ping`
- `get_realtime_quote`
- `get_historical_candles`
- `get_technical_summary`

技術摘要包含：

- 5、10、20、60 日均線
- 均線排列
- 20 日布林通道
- 20 日量比
- 20／60 日區間高低

## 更新方式

1. 把本 ZIP 解壓縮。
2. 到原本的 GitHub Repository。
3. 用本版 `server.py`、`requirements.txt`、`README.md` 覆蓋舊檔。
4. Commit changes。
5. 等 Render 自動部署完成。
6. ChatGPT → 設定 → Apps & Connectors → 你的台股 App → Refresh。

Render 環境變數仍只需要：

`FUGLE_API_KEY=你的 Fugle API Key`
