V12 週報更新包
版本：2026-08-03 v1

【這次新增】

1. 新工具：get_radar_weekly_report
2. 可指定 start_date、end_date、version、top_n。
3. version=V12 時只讀取真正的 v12_ 策略與 postgres-v12 記錄。
4. 同一天、同一股票重複出現在多個策略或多次雷達時，整體統計只算一次。
5. 尚未滿 1／3／5／10／20 個交易日的空值，不再被算成失敗。
6. 分開顯示每個報酬期間的成熟樣本數、待成熟數、平均報酬與勝率。
7. 顯示各策略、每日、最佳股票、最差股票、MFE 與 MAE。
8. 原本 get_radar_performance_summary 的 5 日勝率空值問題也一併修正。

【安裝方式】

1. 解壓縮 V12_Weekly_Report_Update.zip。
2. 打開解壓縮後的 V12_Weekly_Report_Update_v1 資料夾。
3. 全選裡面的所有檔案與資料夾。
4. 複製到 GitHub Desktop 使用的 taiwan-stock-mcp 專案根目錄。
   就是可以看到 server.py、server_v10_tools.py、stock_db、tests 的那一層。
5. Windows 詢問時選擇「取代目的地中的檔案」。
6. 不要刪除 .git，也不要把最外層 V12_Weekly_Report_Update_v1 資料夾整個再套一層。
7. 打開 GitHub Desktop，確認應出現以下 3 個程式變更：
   - server_v10_tools.py
   - stock_db/performance.py
   - tests/test_weekly_performance_report.py
8. Summary 可填：Add V12 weekly radar report
9. 按 Commit to main，再按 Push origin。
10. 等 Render 顯示 Deploy live。

【快速確認】

複製完成後，可直接雙擊專案根目錄裡的：

VERIFY_V12_WEEKLY_REPORT.bat

看到下面這行代表檔案位置正確：

ALL V12 WEEKLY REPORT FILES ARE PRESENT.

這只驗證檔案有放對位置；完整測試會由 Python／GitHub Actions 或部署環境執行。

【部署後使用】

最簡短：

V12週報

指定日期時：

get_radar_weekly_report
version=V12
start_date=2026-07-27
end_date=2026-07-31
top_n=10

日期全部省略時，系統會自動使用最新已保存雷達日期所在的星期一到最新雷達日。

【報表定義】

- 報酬基準是雷達當日收盤價，不等於使用者實際成交損益。
- 1／3／5／10／20 日是後續第 1／3／5／10／20 個交易日。
- 勝率分母只使用已成熟、已有該期間報酬的樣本。
- 同日同股跨策略只在整體統計算一次，各策略區仍各自保留一次。
- MFE／MAE 使用目前資料庫可取得的後續期間，近期訊號可能尚未滿20日。

【不需要做的事】

- 不需要修改 PostgreSQL 資料表。
- 不需要重新回補全部歷史K線。
- 不需要重新執行正式雷達。
- 不會刪除既有雷達與績效紀錄。
