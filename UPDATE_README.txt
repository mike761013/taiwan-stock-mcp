台股 V12.2 策略修正更新包
日期：2026-08-10

安裝方式
========
1. 解壓縮本更新包。
2. 將解壓後的三個項目放到 taiwan-stock-mcp GitHub 專案根目錄：
   - stock_db/v12.py
   - stock_db/radar.py
   - v12_config.json
3. 選擇覆蓋同名檔案後 Commit。

建議 Commit summary
===================
Fix V12 bottom reversal and pullback rules

本次修正
========
1. reversal_reclaim 改為「底部止跌」策略：
   - 距20日低點不超過15%。
   - 距20日低點不超過2.5 ATR。
   - 位於20日價格區間下方45%以內。
   - 較20日高點至少回落10%。
   - 至少出現兩項止跌訊號。
   - 不再以單日大漲作為必要條件。
   - 單日漲幅達8.5%以上強制顯示「不追價」。
   - 進場風險超過8%只列「底部止跌觀察」。

2. pullback 改為「完整多頭排列守支撐」策略：
   - 必須 MA5 > MA10 > MA20 > MA60。
   - 收盤必須守住MA5。
   - 收盤必須守住滾動大量低點。
   - 收盤距MA5不超過3%。
   - 收跌仍可入選；收跌且量縮會提高分數。
   - 不再強制要求收紅K、突破前高或單日上漲。

3. radar.py 新增20／60日高低區間及前兩日資料，供底部位置判斷。

回歸驗證
========
- 世界（5347）2026-08-10：排除，已離20日低點約20%。
- 光頡（3624）2026-08-10：排除，已離20日低點約47%。
- 亞電（4939）2026-07-15：保留為底部反轉訊號；因單日漲幅過大，標示不追價。
- 完整多頭排列、收跌但守住MA5及大量低點：pullback可正常入選。

部署後建議依序驗證
==================
1. get_v12_radar_config
2. screen_market_v12 strategy=reversal_reclaim limit=10 minimum_score=0 save_result=true
3. screen_market_v12 strategy=pullback limit=10 minimum_score=0 save_result=true
4. validate_v12_release limit_each=5 minimum_score=0
