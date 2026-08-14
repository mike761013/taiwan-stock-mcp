V12.1 交易劇本語意修正包
版本日期：2026-08-14

【驗證結果】

上一個逾時修正已成功：四策略、合併雷達、單次快照與資料寫入均正常。
完整發布驗證剩下兩類價格標籤不一致：

1. 顯示後的訊號價等於不追價線，但狀態仍為等待拉回。
2. Pullback 訊號價高於理想買進區、但未超過最高買價，狀態仍為買進區。

【本次修正】

- 所有狀態改用使用者實際看到的台股跳動單位價格比較。
- 訊號價等於不追價線時，狀態固定為「不追價」。
- Pullback 高於理想買進區、但仍低於最高買價時，改為「等待價格確認」，
  不再誤標成「買進區」。
- 確認買點是否低於不追價線，也改用顯示後價格判斷。

本修正不改選股條件、策略門檻、預測分數、排序權重、ATR、防守價或停損規則。

【安裝】

1. 解壓縮 V12_Bullish_Accuracy_Semantic_Hotfix.zip。
2. 打開解壓後的 V12_Bullish_Accuracy_Semantic_Hotfix 資料夾。
3. 全選裡面的所有內容，複製到 taiwan-stock-mcp 專案根目錄。
4. Windows 詢問時選擇「取代目的地中的檔案」。
5. GitHub Desktop 提交：Fix V12.1 trading-plan semantics
6. 按 Push origin，等待 Render 顯示 Deploy live。
7. 回到 ChatGPT 輸入：部署完成

【本機驗證（可選）】

雙擊 VERIFY_V12_BULLISH_ACCURACY.bat。
看到 TEST PASSED 即可。

