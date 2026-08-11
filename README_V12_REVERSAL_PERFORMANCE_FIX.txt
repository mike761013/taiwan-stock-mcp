V12 reversal_reclaim 績效摘要查詢修正
版本日期：2026-08-11

【修正原因】

V12 雷達在資料庫中保存的名稱是：
v12_reversal_reclaim

但原本查詢工具收到：
reversal_reclaim

時會直接做完全相等比對，因此顯示 0 筆。
實際訊號沒有遺失，目前以完整名稱查詢可找到 45 筆。

【怎麼安裝】

1. 解壓縮本更新包。
2. 打開解壓縮後的 V12_Reversal_Performance_Alias_Fix 資料夾。
3. 全選資料夾裡面的所有內容。
4. 複製到 GitHub Desktop 使用的 taiwan-stock-mcp 專案根目錄。
5. Windows 詢問時選擇「取代目的地中的檔案」。
6. 不要把最外層 V12_Reversal_Performance_Alias_Fix 資料夾整個塞進專案。
7. GitHub Desktop Summary 輸入：
   Fix V12 reversal performance lookup
8. 按 Commit to main，再按 Push origin。
9. 等 Render 部署完成。

【部署後驗證】

執行：

get_radar_performance_summary
strategy=reversal_reclaim

正確結果應出現：

resolvedStrategy: v12_reversal_reclaim
samples: 大於 0

樣本數會隨後續正式雷達與收盤回測增加。

【這次不會改動】

- 不改正式雷達選股規則。
- 不改 reversal_reclaim 判定條件。
- 不刪除既有訊號。
- 不必重新跑正式雷達。
- 不必重建歷史資料。
- early_stage、breakout、pullback 的舊 V11 查詢行為保持不變。
