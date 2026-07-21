# V11 一鍵全市場歷史回補

這個補丁不修改既有 V11 核心程式，只新增：

- `.github/workflows/backfill-all-market.yml`
- `scripts/backfill_all_market_once.py`
- `scripts/check_stock_coverage.py`

用途：

1. 在 GitHub Actions 內執行長時間回補，不經過 MCP HTTP。
2. 避免 Render 免費版的 504 Gateway Timeout。
3. 每批都寫出 `backfill_checkpoint.json`。
4. 工作失敗或超時時，可從 `nextStartAfter` 續跑。
5. 執行前後自動輸出資料覆蓋率。

## GitHub Secrets

Repository → Settings → Secrets and variables → Actions → New repository secret

必要：

- `DATABASE_URL`：複製 Render 的 PostgreSQL External Database URL

視你的專案設定：

- `FINMIND_TOKEN`：若 Render 有設定，就一併新增；沒有可先不加

## 執行

GitHub repository → Actions → Backfill all Taiwan stock history → Run workflow

第一次建議：

- years: `1`
- batch_size: `20`
- concurrency: `6`
- start_after: 留空
- max_symbols: `0`

如果工作因 GitHub 執行時間或外部 API 中斷：

1. 打開該次 Workflow 的 Artifacts。
2. 下載 `stock-backfill-report`。
3. 查看 `backfill_checkpoint.json` 的 `nextStartAfter`。
4. 再次 Run workflow，將該值填到 `start_after`。

## 完成標準

執行 `scripts/check_stock_coverage.py` 後，重點看：

- `symbols_without_bars`
- `symbols_with_bars`
- `recently_updated_symbols`
- `symbols_with_200_bars`

新上市股票可能本來就不足 200 筆，因此不能只用 `symbols_with_200_bars` 判斷是否完整。
