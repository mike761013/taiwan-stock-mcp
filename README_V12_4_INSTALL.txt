台股雷達 V12.4 更新說明
========================

正式版本固定顯示：V12.4
不使用 V12.4.1、V12.4.2 等細分版本。

本次更新
--------
1. 保留原有七因子評分與四種策略。
2. 新增 reversal_continuation（反轉續強橋接）：
   - 捕捉 V 型反轉離開底部後的第一段健康整理。
   - MA60 改為階段判斷，不再要求完整 MA5>MA10>MA20>MA60 才能辨識。
   - 反轉背景成立時，五日漲幅上限動態放寬至 18%，但漲停與過度乖離仍禁止追價。
3. 新增黃金三角辨識：成立、形成中、近期形成與均線收斂。
4. 新增 nearMissObservations，不把差一項的股票直接消失，也不混入正式買進榜。
5. 新增 MCP 工具 explain_stock_v12(symbol)，逐項顯示單檔通過或淘汰原因。
6. 所有正式輸出的 version 與 accuracyEngine 統一為 V12.4。

安裝方式
--------
1. 關閉正在執行的 MCP Server。
2. 將更新包內 files_to_replace 的內容，依相同路徑覆蓋到專案根目錄。
3. 在專案根目錄執行：

   python scripts/verify_v12_4.py

4. 看到「V12.4 VERIFY PASSED」後，重新部署／啟動服務。
5. 呼叫 get_v12_radar_config，確認：

   version = V12.4
   accuracyEngine = V12.4
   strategies 包含 reversal_continuation

台表科 6278 防追價驗證
---------------------
- 2026-08-11：可由 reversal_continuation 辨識為反轉後第一段健康整理。
- 2026-08-26：漲停且距 MA20 約 18.9%，必須顯示 DO_NOT_CHASE（不追價）。

重要
----
黃金三角只提供結構加分，不會單獨產生買進訊號；仍須同時通過量能、位置、
均線支撐、收盤承接、風險與七因子資料完整度門檻。

