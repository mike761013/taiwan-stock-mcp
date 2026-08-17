V12 TDCC 免費股權分散備援更新包
================================

這次修改的目的
--------------
把原本需要 FinMind Backer／Sponsor 的
TaiwanStockHoldingSharesPer 股權分散資料，改成直接讀取：

TDCC 臺灣集中保管結算所 OpenData 1-5（免費、無需 Token）
https://opendata.tdcc.com.tw/getOD.ashx?id=1-5


安裝方式
--------
1. 解壓縮 V12_TDCC_Distribution_Fallback.zip。
2. 打開解壓縮後的 V12_TDCC_Distribution_Fallback 資料夾。
3. 只複製裡面的 server.py。
4. 貼到 GitHub Desktop 的 taiwan-stock-mcp 專案根目錄。
   就是可以看到原本 server.py、server_v10_tools.py、stock_db、tests 的那一層。
5. Windows 問是否取代檔案時，選「取代目的地中的檔案」。
6. GitHub Desktop 左下 Summary 輸入：
   Add free TDCC shareholding distribution
7. 按 Commit to main，再按 Push origin。
8. 等 Render 顯示 Deploy live。

不需要修改 Render Environment，也不需要新增 TDCC Token。
原本 FINMIND_TOKEN 仍要保留，因為法人、融資券、外資持股與借券資料
仍然使用 FinMind；只有股權分散改走 TDCC。


怎麼驗證
--------
部署完成後，執行：

get_shareholding_distribution
symbol=2330
days=120
force_refresh=true

正確結果應該看到：

source = TDCC OpenData 1-5 集保戶股權分散表
access = 免費公開資料，無需 Token
latest.under100LotsPercent 有數值
errors 不再出現 FinMind 400


重要說明
--------
1. TDCC 免費 CSV 每週更新一次，不是每日資料。
2. 免費 CSV 只提供最新一期，所以第一次部署時：
   previousUnder100LotsPercent 與 under100LotsPercentChange 可能是 null，這是正常的。
3. 系統收集到第二個不同週期後，才會開始顯示前一期變化。
4. 如果 Render 已設定 REDIS_URL，歷史週資料可跨休眠與重新部署保留；
   沒有 REDIS_URL 時，服務重啟後仍能查最新比例，但前一期比較可能重新累積。
5. 100 張以下的算法固定使用 TDCC 第 1～9 級；
   第 16 級「差異數調整」與第 17 級「合計」不會重複計入。


本機測試（非必要）
------------------
若電腦已安裝本專案 Python 套件，可雙擊：
VERIFY_V12_TDCC_FALLBACK.bat

看到 ALL TDCC FALLBACK TESTS PASSED 與 TEST PASSED 才代表測試通過。
