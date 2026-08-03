V12 收盤回測漏算修正版
版本日期：2026-08-03

【修正內容】

原本收盤作業只取最舊的500筆未滿20日訊號。
由於舊訊號的 return_d20 在20個交易日以前都會保持空值，
它們每天都會重複占滿500筆上限，導致新訊號可能一直沒有被回測。

本次修正：

1. 從未計算過的新訊號優先。
2. 已計算過的訊號依 calculated_at 由舊到新更新。
3. 每日預設上限由500提高為5000筆。
4. 收盤作業明確使用5000筆上限。
5. 回傳 selected、processed、limit，方便確認是否完整。

這項修改只影響雷達績效回測的更新順序與數量，
不會改變日K、技術指標、V12分數、候選股或買賣防守價。

【安裝方式】

1. 解壓縮 V12_Performance_Backtest_Fix.zip。
2. 開啟解壓縮後的 V12_Performance_Backtest_Fix 資料夾。
3. 全選資料夾裡面的所有檔案與資料夾。
4. 複製到 GitHub Desktop 使用的 taiwan-stock-mcp 專案根目錄。
5. Windows 詢問時選擇「取代目的地中的檔案」。
6. 在 GitHub Desktop 提交並 Push origin。
7. 等 Render 部署完成。

建議 Commit 訊息：
Fix V12 backtest update starvation

【部署後使用】

直接對 ChatGPT 輸入：

收盤更新與回測

正常完成時，績效區會顯示：

- limit：5000
- selected：本次選取筆數
- processed：成功計算筆數
- newSignalsPrioritised：true

【選擇性本機驗證】

在專案根目錄雙擊：

VERIFY_V12_PERFORMANCE_FIX.bat

看到 TEST PASSED 才代表測試通過。
