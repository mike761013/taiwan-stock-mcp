V12.1 後勢看漲準確度更新包
版本基線：GitHub main 43a3f9c（已包含雙買點劇本）

【這次真正修改什麼】

1. 正式雷達新增「後勢看漲分數」
   - 不再只因股價已上漲、站上均線就得到高分。
   - 加入 MA5／MA20 斜率、收盤位置、上影線、五日漲幅、
     大量低點、量能是否健康及距離 MA20 是否過遠。

2. Breakout 假突破與末端追價防護
   - 未收過前一日高點、收盤遠離日高、量能過熱、
     距 MA20 過遠者會被排除或大幅扣分。

3. Pullback 必須真的出現止穩
   - 除了完整多頭排列、守 MA5 與大量低點，
     還必須達到設定數量的止穩訊號。

4. 正式雷達分成兩個榜
   - top10／top5：只有目前可執行的候選股。
   - bullishTop10：後勢看漲觀察榜。
   - watchlistCandidates：等待拉回、不追價、底部觀察等標的。
   - DO_NOT_CHASE 與 WAIT_PULLBACK 不再混進可買前十名。

5. 新增 V12.1 執行型回測
   - 訊號隔天才開始判斷成交，避免使用未來資料。
   - 模擬激進低接、確認買點、分批比例及加權成本。
   - 未觸及買點記為未成交，不再當成虧損。
   - 收盤確認失敗後，使用下一交易日開盤價退出。
   - 同日同股重複執行會在績效摘要中去重。

6. 歷史績效回饋
   - 只使用「真的有成交」且已成熟的執行型回測。
   - 每種策略至少 30 筆樣本才啟用。
   - 權重採信心折減且最多只調整正負 8 分，避免過度擬合。

【安裝方式】

1. 解壓縮 V12_Bullish_Accuracy_Update.zip。
2. 打開解壓縮後的 V12_Bullish_Accuracy_Update 資料夾。
3. 全選「資料夾裡面的內容」，複製到 GitHub Desktop 使用的
   taiwan-stock-mcp 專案根目錄。
4. Windows 詢問時選擇「取代目的地中的檔案」。
5. 不要把最外層 V12_Bullish_Accuracy_Update 資料夾整個塞進專案。
6. 可以雙擊 VERIFY_V12_BULLISH_ACCURACY.bat。
   最後看到 TEST PASSED 即代表本機純規則測試成功。
7. 在 GitHub Desktop 輸入摘要：
   Upgrade V12.1 forward bullish accuracy
8. 按 Commit to main，再按 Push origin。
9. 等 Render 顯示 Deploy live。

【部署後怎麼驗證】

對 ChatGPT 下：

驗證V12

驗證成功後先執行：

收盤作業

收盤作業會自動建立新的執行型績效表並更新可用歷史訊號，
不需要自行進資料庫輸入 SQL。

接著下：

正式雷達

新的正式雷達輸出會包含：
- accuracyEngine = V12.1_FORWARD_BULLISH
- actionableCandidateCount
- watchCandidateCount
- top10（目前可執行）
- bullishTop10（後勢看漲觀察）
- watchlistCandidates（等待拉回／不追價）
- executionPriors（成熟後才會啟用）

查真正可執行績效可下：

V12執行績效

剛部署時樣本可能尚未成熟，顯示空值是正常現象。

【注意】

- 這次更新是提高篩選與驗證品質，不保證每一檔都上漲。
- 舊的 signal_performance 不會刪除，仍保留作為「訊號日收盤買進」基準。
- 新績效只計算符合劇本並真的觸價成交的股票。
- ETF 仍不會進入正式雷達；範圍維持上市櫃普通股。
