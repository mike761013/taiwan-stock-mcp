V11.1 current-repository update

This package is based on the CURRENT files you uploaded:
- stock_db/radar.py (609 lines)
- stock_db/v12.py (788 lines)

Replace exactly:
- stock_db/radar.py  <- radar.py
- stock_db/v12.py    <- v12.py

Changes:
1. Pullback V2
   - requires established bullish structure
   - recognizes MA10 / MA20 support zones
   - treats contracted pullback volume as positive
   - rejects broken MA60 / rolling massive-volume-low structure
   - requires at least 2 stabilization signals before passing
2. reversal_reclaim
   - preserves existing V12 reversal logic
   - adds MA5 >= MA10 early-structure bonus / warning
3. Existing V12 liquidity gates, price tiers, ATR trading plan,
   semantic validation, and full-radar interfaces are preserved.
4. Legacy V11 screen can route reversal_reclaim to the richer V12 engine.

Validation:
- AST syntax validation passed for both files.
- Synthetic healthy pullback functional test passed.
