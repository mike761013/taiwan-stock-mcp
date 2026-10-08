# Formal activation policy

Revision: `V12.4-FORMAL-ACTIVATION-1`, introduced 2026-10-08.

The new policy is a prospective experiment, not a demonstrated increase in
win rate. Historical signals retain their original snapshots and rules.

## Formal entry

All existing technical, full-factor, event and execution checks still apply.
An independent gate additionally requires:

- Ordinary nonfinancial stocks priced at or below NT$200.
- At least 3,000 lots today, 1,500 lots on the 20-session average, and the
  existing turnover floor. Monetary turnover cannot compensate for low volume.
- Qualified relative-strength profile, positive five-session excess return
  versus the common-stock market median, nonnegative ten-session excess
  return and five-session sector excess, sector breadth at least 50%.
- Volume ratio at least 1.3, rising close reclaiming the previous high,
  closing position at least 70%, and limited extension from MA20.
- In weak breadth regimes: volume ratio at least 1.5, sector breadth at least
  60%, and five-session market excess at least three percentage points.
- Five dated institutional sessions through the signal date, at least three
  positive days, positive five-day and final-two-day totals, and signal-day
  buying. Missing/stale data fail rather than receiving neutral credit.
- Fresh published monthly revenue with nonnegative year-over-year growth.
- No known hard event risk or combined rapid margin/price overheating.
- An executable confirmation price below the existing chase cap, with
  0.8–6% distance to the original failure level and at least half an ATR.

Failed formal candidates become `WAIT_ACTIVATION`, with individual reasons
and data dates. Scores do not override failed gates. Earlier support probes
are observations, not formal entries. The independent prelaunch lane retains
its three-month acceleration and persistent-accumulation requirements.

## Execution and evaluation

Formal candidates have no aggressive support-zone entry. Confirmation uses
40% of the model allocation and requires the entry session's volume ratio
and close position to pass again. OHLCV confirmation remains a daily-bar
simulation, not evidence of an actual executable intraday fill.

New-model watch snapshots are `NO_TRADE` in execution evaluation. Their price
performance may still be observed separately. Technical strategy prefilters
are explicitly diagnostic and cannot count as new formal signals.

`get_v12_formal_performance_summary` and the default execution-summary tool
filter by this revision and `formalQualification.qualified=true`, excluding
old versions and probes. Passing `formal_model_revision=null` to the general
execution-summary tool retrieves the broader historical cohort.

`get_v12_formal_quality_preview` is read-only and uses cached evidence. It
does not run full remote-factor enrichment or create performance samples.

Official institutional history that failed before publication can retry once
after a 30-minute cooldown. Atomic reservations retain the shared 40-request
daily cap, and successful or running tasks are not duplicated.
