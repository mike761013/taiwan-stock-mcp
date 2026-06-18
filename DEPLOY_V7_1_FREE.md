# 從目前 V7 升級至 V7.1

你不需要建立新的 Render 服務，也不需要更改 MCP 網址。

## 1. 解壓縮 V7.1 ZIP

裡面主要有：

```text
server.py
requirements.txt
README.md
DEPLOY_V7_1_FREE.md
test_v71_free.py
.gitignore
.python-version
```

## 2. 覆蓋目前 GitHub 分支

進入目前 V7 Render 服務連接的 GitHub Repository，切換至：

```text
v7-free-official-same-day
```

點：

```text
Add file → Upload files
```

把解壓縮後的所有檔案拖入，讓 `server.py`、`README.md` 等檔案被覆蓋。

Commit message 建議：

```text
Upgrade V7 to V7.1 official fallback
```

確認提交目標仍是 `v7-free-official-same-day`。

## 3. 等 Render 自動部署

回到：

```text
Render → taiwan-stock-mcp-v7-free → Events / Logs
```

GitHub 提交後通常會自動部署。若沒有，點：

```text
Manual Deploy → Deploy latest commit
```

這次沒有新增環境變數，因此原本 Environment 不用改。

## 4. 確認部署成功

Logs 最後應看到：

```text
Uvicorn running on http://0.0.0.0:10000
Your service is live
```

MCP 網址保持：

```text
https://taiwan-stock-mcp-v7-free.onrender.com/mcp
```

ChatGPT 裡原本的「台股即時報價 V7」App 不必刪除或重建。

## 5. 測試 PING

執行：

```text
PING
```

應回傳：

```text
Taiwan Stock MCP v7.1-free-official-fallback
version=7.1.0-free
```

如果仍顯示 `7.0.0-free`，代表 Render 還沒部署到最新 GitHub commit。

## 6. 測試官方備援

執行：

```text
使用 screen_market，
strategy=early_stage，
markets=BOTH，
top_n=10，
candidate_limit=40，
include_chip=true，
force_refresh=true
```

這次若主要 TWSE 仍是前一日、TPEx 已是當日，V7.1 會自動查 TWSE 指定日期備援。

成功時重點欄位：

```text
serverVersion=v7.1-free-official-fallback
fallbackUsed=true
fallbackMarkets=["TSE"]
primaryMarketDates.TSE=前一交易日
finalMarketDates.TSE=當日
allUniverseUsingSameDate=true
```

備援仍失敗時，查看 `fallbackAttempts` 的 `error`，程式會繼續拒絕混日排名。
