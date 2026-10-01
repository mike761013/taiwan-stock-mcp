# V12.4 strength selection, revision 1

Purpose: rank stocks whose strength is forming or accelerating, independently
of whether today's quote is an executable entry. No change to stop-loss,
take-profit, position sizing, database universe, or automatic radar schedule.

## Point-in-time inputs and ranking

All inputs use the existing same-session TWSE/TPEX common-stock snapshot.
The market benchmark is the median 5/10/20-session common-stock return, NOT
TAIEX, TPEx index, or a capitalization-weighted index. Sector means the
existing securities industry classification; it is not a hand-built AI theme
basket. Missing history or insufficient sector coverage cannot qualify as a
strong candidate, and missing values remain explicitly reported.

Strength score (0–100):

- 45% relative strength: cross-sectional return midrank percentiles,
  with 5/10/20-session weights of 40/35/25%.
- 25% sector resonance: sector median return relative to market median,
  proportion above MA20, median volume-MA5/MA20 ratio, and breakout breadth.
- 30% volume/price: close location, volume acceleration, confirmed breakout,
  and low-volume support. Penalize failed breakout, long upper shadow and
  high-volume down days.

Ranking blends 65% strength with 35% the existing rank (complete factor rank
after enrichment). Compute strength BEFORE any strategy/factor shortlist;
reapply the blend after enrichment overwrites the original rank. Prior-20-day
breakout high excludes the current signal day. No forward bars are accessed.

Strong-board eligibility additionally requires all necessary inputs,
positive 5-session return, above-MA20 close, 5/10-session market outperformance,
10-session percentile >=65, sector support and no failed-breakout/long-wick
condition. Threshold is 65 strength points. This threshold and weights are
initial engineering settings, not claimed statistically optimal values.

## Outputs and safeguards

`strengthTop10`, `startupCandidates`, `continuationCandidates` are independent
strength boards. Starting candidates have <=15% 20-session gain and <=8% MA20
extension; other eligible candidates go to continuation. An overextended stock
may be strong but still carry DO_NOT_CHASE. Strength never promotes a watch or
probe into a formal buy, changes a stop, or overrides complete-factor/event
qualification. Existing actionable/probe/watch outputs remain available.

Every saved candidate carries the strength profile, benchmark, scores and model
revision. Factor revision becomes `V12.4-STRENGTH-FACTORS-1` so new execution
priors do not silently reuse the older factor model's simulated returns.
Pre-upgrade signals are not retroactively reclassified.

`get_v12_strength_preview` is a read-only deployment check: reads the snapshot,
applies existing liquidity/pattern/price rules and strength ranking, but does
not save radar records, fetch external factors, or change the automatic schedule.
Its results do not substitute for fully enriched formal radar results.

## Validation and subsequent evaluation

Tests cover leader/flat-stock ordering, missing inputs, neutral ties, failed
breakouts, retained no-chase/stop rules, separate lifecycle boards, and ranking
before shortlist and after factor enrichment. No current-data replay is used
to claim historical or out-of-sample improvement.

Track the next fixed-revision signals separately. To assess selection skill,
evaluate top5/top10 5/10/20-session returns, outperformance against corresponding
future market/sector benchmarks, and adverse excursion. Those forward benchmark
statistics are a subsequent reporting task, not implemented by this change.
Do not equate a stock's interim highest price with a successful selection.

Rollback: revert this commit to restore code, config and prior-model revision.
`strength_ranking_enabled=false` disables the rank blend for diagnostic purposes.
