# V12 看漲分析雷達——直接覆蓋安裝版

這份專案已依你上傳的 V11 原始專案直接完成接線，不需要再修改 TODO、import 或 MCP 註冊。

## 已新增工具

- `screen_market_v12`
- `run_full_bullish_radar_v12`
- `get_v12_radar_config`
- `validate_v12_release`

原本 V10／V11 工具全部保留。

## V12內容

1. V7 流動性硬過濾
   - 單日成交量至少 2,000 張
   - 20 日均量至少 1,000 張
   - 成交金額至少 1 億元
2. 價格分層
   - 200 元以下股票列入正式主榜
   - 200 元以上股票不占用主榜名額
   - 高價股僅在總分至少 85 分、最大風險不超過 6%、
     操作狀態可執行且沒有過熱／乖離／走弱警示時另列
3. V11 原三策略
   - `early_stage`
   - `breakout`
   - `pullback`
4. 新增 `reversal_reclaim`
   - 用來提早抓亞電 2026-07-15 這類強勢反轉收復
5. ATR14 與分級防守
   - 固定訊號防守價
   - ATR硬停損
   - 小幅跌破先減碼，不立即全砍
   - 收復後允許分批買回
6. 交易決策輸出
   - 理想買進區
   - 最高可買價
   - 禁止追價價
   - 建議初始部位
   - MA20／布林／爆量過熱扣分

## 安裝

1. 先把目前本機 `taiwan-stock-mcp` 資料夾複製一份備份。
2. 解壓縮本套件。
3. 把解壓後資料夾中的所有檔案複製到原本 `taiwan-stock-mcp` 根目錄。
4. Windows詢問是否取代時，選擇「取代目的地中的檔案」。
5. GitHub Desktop確認變更後，提交並推送。

實際有修改／新增的核心檔案只有：

- `server_v10_tools.py`（修改）
- `stock_db/radar.py`（修改）
- `stock_db/v12.py`（新增）
- `v12_config.json`（新增）
- `tests/test_v12_radar.py`（新增）

`server.py`不用修改，因為它原本就會呼叫 `register_v10_tools(mcp)`，新增的V12工具會一起註冊。

## 部署後先測

依序執行：

```text
get_v12_radar_config
```

```text
screen_market_v12
strategy=reversal_reclaim
limit=10
minimum_score=0
save_result=false
```

```text
run_full_bullish_radar_v12
limit_each=10
minimum_score=45
save_result=true
```

最後執行：

```text
validate_v12_release
limit_each=5
minimum_score=0
```

## 亞電驗證

內建測試以亞電 2026-07-15 的已知K線與指標驗證，目標是在7/15收盤後產生：

- 策略：`reversal_reclaim`
- 訊號價：60.8
- 7/16理想買進區約落在60～61元附近
- 66.8元已超過合理追價區，不應把它當成第一筆進場價

## 調整設定

所有參數都在根目錄：

```text
v12_config.json
```

第一次部署先不要更改。累積至少50～100筆訊號績效後，再調ATR倍數、流動性門檻或反轉條件。
