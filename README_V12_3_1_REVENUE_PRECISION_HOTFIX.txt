V12.3.1 月營收極端值欄位修正包
資料庫版本：1232

【修正原因】

官方資料存在超過 1,000,000% 的年增率，舊 NUMERIC(10,4) 欄位不足，
造成更新V12基本面出現 NumericValueOutOfRangeError。

【修正內容】

- 月增率、年增率、年增加速度欄位擴大為 NUMERIC(22,4)。
- 保留官方原始數值，不截斷資料。
- 雷達評分仍維持原本的合理封頂，極端基期不會無限放大分數。
- schemaVersion 升為 1232。

【安裝】

1. 解壓縮更新包。
2. 把解壓縮後資料夾「裡面的全部內容」複製到 taiwan-stock-mcp 根目錄。
3. Windows 詢問時選擇取代目的地中的檔案。
4. GitHub Desktop Summary 輸入：
   Fix V12 revenue precision overflow
5. Commit to main，再 Push origin。
6. 等 Render 顯示 Deploy live。

部署完成後告訴我「部署完成」，我會重新執行：

初始化資料庫
更新V12基本面
正式雷達
