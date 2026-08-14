V12.1 正式雷達逾時修正包
版本日期：2026-08-14

【為什麼需要這個修正】

部署 V12_Bullish_Accuracy_Update 後，V12.1 新設定與 breakout 策略已正常載入，
但正式發布驗證發現 early_stage、pullback、reversal_reclaim 的全市場快照查詢
可能超過 PostgreSQL 免費環境的 30 秒 statement timeout。

本修正只改善查詢與驗證流程，不改任何選股門檻、分數、排序權重、買點、
分批比例、防守或失敗條件。

【修正內容】

1. 前一交易日 MA5／MA10／MA20／MA60 改用既有主鍵索引逐股取得，
   不再對 120 天 daily_indicators 做第二次全表視窗排序。
2. validate_v12_release 改為只取得一次市場快照；同一次結果已包含四策略、
   四策略寫入及合併雷達寫入，不再重複掃描五次。
3. 驗證腳本可直接從專案根目錄執行。

【安裝】

1. 解壓縮 V12_Bullish_Accuracy_Timeout_Hotfix.zip。
2. 打開解壓後的 V12_Bullish_Accuracy_Timeout_Hotfix 資料夾。
3. 全選資料夾內的所有檔案與資料夾。
4. 複製到 GitHub Desktop 使用的 taiwan-stock-mcp 專案根目錄。
5. Windows 詢問時選擇「取代目的地中的檔案」。
6. 在 GitHub Desktop 提交：
   Fix V12.1 radar snapshot timeout
7. 按 Push origin，等待 Render 顯示 Deploy live。
8. 回到 ChatGPT 輸入：部署完成

【本機快速驗證（可選）】

在專案根目錄雙擊：
VERIFY_V12_BULLISH_ACCURACY.bat

看到 TEST PASSED 即可。

【應覆蓋的程式檔】

- server_v10_tools.py
- stock_db/radar.py
- scripts/verify_v12_bullish_accuracy.py
- tests/test_v12_bullish_accuracy.py

