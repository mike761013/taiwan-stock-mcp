V12.3 七因子正式雷達更新包
版本：V12.3_SEVEN_FACTOR

【安裝方式】

1. 解壓縮 V12_3_Seven_Factor_Update.zip。
2. 打開解壓縮後的 V12_3_Seven_Factor_Update 資料夾。
3. 全選「裡面的檔案與資料夾」，複製到 GitHub Desktop 使用的
   taiwan-stock-mcp 專案根目錄。
4. Windows 詢問時選「取代目的地中的檔案」。
5. 不要刪除專案內其他檔案，也不要複製 .git 資料夾。
6. GitHub Desktop 的 Summary 輸入：
   Upgrade V12.3 seven-factor radar
7. 按 Commit to main，再按 Push origin。
8. 等 Render 顯示 Deploy live。

【部署後只做一次】

依序對台股工具下：

初始化資料庫
同步股票清單
更新V12基本面

對應工具名稱：

initialize_stock_database
sync_stock_security_master
refresh_v12_fundamentals

【平常使用】

收盤作業
正式雷達
深查

收盤作業完成最後一批時，會自動：

- 更新上市櫃普通股日K與最新指標
- 更新官方月營收與營收加速度
- 執行 V12.3 正式雷達
- 更新新訊號優先的執行回測

【V12.3 正式雷達七因子】

1. 技術結構 30%
2. 法人／融資籌碼 20%
3. 月營收成長與加速度 15%
4. 題材熱度與族群強度 15%
5. 盤中成交結構 10%
6. 大盤環境 5%
7. 同版真實成交績效 5%

缺少某個外部資料時不會當成 0 分；系統會重新正規化可用因子，
並輸出 dataConfidence 與 missingFactors。正式環境完整度低於 60%
會只列觀察，不列正式可操作前十。

籌碼需要 Render 環境中的 FINMIND_TOKEN。
盤中成交結構需要 Render 環境中的 FUGLE_API_KEY。
題材標籤不會由系統亂猜，可用下列工具建立：

set_v12_theme_tags
symbol=股票代號
themes=AI伺服器,散熱

【擴充歷史樣本】

預設保留與回補期間改為 5 年。第一次擴充請分批使用：

backfill_all_stock_history
years=5
batch_size=20

每次把回傳的 nextStartAfter 放入下一次的 start_after，直到
hasMore=false。這項工作只需完成一次，不要每天重跑。

【本機驗證】

在專案根目錄雙擊 VERIFY_V12_3.cmd，看到
V12.3 VERIFY PASSED 才表示檔案完整。

重要：此更新增加資料與篩選品質，但不可能保證股票 100% 上漲。
