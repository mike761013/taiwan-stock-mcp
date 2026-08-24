V12.3.1 三項資料修正更新包
版本：V12.3.1_SEVEN_FACTOR_FIX

【本次修正】

1. 題材標籤
   - 更新V12基本面時，自動以證交所／櫃買中心的「產業別」建立基礎標籤。
   - 題材熱度改用同標籤股票的全市場當日廣度、漲跌、量能與技術分數。
   - 手動加入 AI伺服器、散熱等細分題材時，不再刪掉官方產業標籤。

2. 月營收基本面
   - 正確讀取「營業收入-當月營收」。
   - 正確讀取「營業收入-上月比較增減(%)」。
   - 正確讀取「營業收入-去年同月增減(%)」。
   - 不再把缺失欄位顯示為 0 或固定 50 分。
   - YoY 與 MoM 第一次更新後即可使用；加速度需資料庫累積至少兩個月份。

3. 盤中成交結構
   - 現價、開盤、最高、最低任一缺失時，盤中因子標示 missing，不再給中性 50 分。
   - 報價日期與雷達交易日不同時不計分，避免混用不同日期。
   - 依 Fugle 定義修正內外盤方向：外盤較強才增加分數。

4. 額外安全修正
   - 七因子最終分數低於 65，即使前段規則通過，也只能進觀察區。

【安裝方式】

1. 解壓縮 V12_3_1_Three_Fixes_Update_READY.zip。
2. 打開解壓縮後的 V12_3_1_Three_Fixes_Update 資料夾。
3. 全選資料夾「裡面的檔案與資料夾」。
4. 複製到 GitHub Desktop 使用的 taiwan-stock-mcp 專案根目錄。
5. Windows 詢問時選「取代目的地中的檔案」。
6. 不要刪除專案其他檔案，也不要動 .git 資料夾。
7. 可雙擊 VERIFY_V12_3_1.cmd，看到 V12.3.1 VERIFY PASSED。
8. GitHub Desktop Summary 輸入：
   Fix V12.3 factor data quality
9. 按 Commit to main，再按 Push origin。
10. 等 Render 顯示 Deploy live。

【部署完成後依序對我說】

初始化資料庫
同步股票清單
更新V12基本面
正式雷達

初始化成功時 schemaVersion 應為 1231。
更新V12基本面結果應看到：
- updated 大於 0
- autoThemeTags 大於 0

正式雷達個股應看到：
- accuracyEngine = V12.3.1_SEVEN_FACTOR_FIX
- 月營收有資料時，revenueYoYPercent／revenueMoMPercent 不再固定為 0
- 報價不完整時，intraday 出現在 missingFactors，而不是 intradayScore=50
- finalScore 低於 65 的股票不會進 actionableCandidates

重要：本次修正讓資料與分數語意正確，不代表任何股票保證上漲。
