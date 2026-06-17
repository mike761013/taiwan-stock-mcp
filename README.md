# 台股 MCP Starter

這是一個最小可用版本，提供：

- `ping`：確認 MCP 是否正常
- `get_realtime_quote`：透過 Fugle MarketData API v1.0 查台股即時報價

## Render 設定

- Build Command：`pip install -r requirements.txt`
- Start Command：`python server.py`
- Environment Variable：
  - Key：`FUGLE_API_KEY`
  - Value：你的 Fugle 行情 API v1.0 金鑰

部署完成後，MCP 網址通常是：

`https://你的服務名稱.onrender.com/mcp`

## 本機測試

```bash
pip install -r requirements.txt
set FUGLE_API_KEY=你的金鑰
python server.py
```

macOS / Linux：

```bash
export FUGLE_API_KEY=你的金鑰
python server.py
```

本機 MCP endpoint：

`http://localhost:8000/mcp`
