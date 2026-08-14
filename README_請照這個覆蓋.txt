V12 最終 2851 狀態修正

GitHub main 提交 740d0ad 仍是舊版，缺少兩行狀態保護。
這個新檔名的壓縮包只包含真正需要更新的 stock_db/v12.py。

操作：

1. 關閉或刪除先前解壓縮的 Semantic Hotfix 資料夾，避免拿到舊檔。
2. 解壓縮 V12_Final_2851_Status_Fix.zip。
3. 打開 V12_Final_2851_Status_Fix 資料夾。
4. 把裡面的 stock_db 資料夾複製到 taiwan-stock-mcp 專案根目錄。
5. Windows 詢問時選擇「取代目的地中的檔案」。
6. GitHub Desktop 應顯示 stock_db/v12.py 有修改。
7. Commit 訊息輸入：Fix 2851 no-chase status precedence
8. 按 Push origin，等待 Render 顯示 Deploy live。
9. 回到 ChatGPT 輸入：部署完成

正確檔案在以下程式區段會同時出現：

    "DO_NOT_CHASE",
    "WAIT_PULLBACK",
    "SMALL_POSITION_OR_SKIP",

