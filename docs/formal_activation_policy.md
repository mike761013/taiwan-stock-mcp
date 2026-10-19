# Formal activation policy

Revision: `V12.4-FORMAL-ACTIVATION-2`, introduced 2026-10-19.

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
and data dates. Scores do not override failed gates. Support setups are no
longer permanently vetoed by their strategy name: a technically qualified
`PROBE_ENTRY` can graduate only after full-factor qualification and all the
same formal gates. It then becomes `PRICE_CONFIRMATION_REQUIRED`, with no
aggressive entry and 40% confirmation allocation. Nonqualified candidates
uniformly become `WAIT_ACTIVATION`, with zero allocation and disabled orders;
their displayed prices are reference levels. The independent prelaunch lane retains
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

## Post-close watch activation

After a successful complete market update, close maintenance with
`run_radar=false` rechecks the last saved combined radar's nonformal candidates.
This is not a scheduled background job or a full-universe radar rerun. A newly
requested full radar evaluates its own current candidates instead.

- Watch validity is five **market trading sessions** after the original signal.
  Missing sessions fail closed. Any intervening close below the original
  failure price invalidates that origin, even after a recovery.
- Prices must stay within the original maximum buy and below the original
  no-chase level. New plans may tighten, never widen, those bounds or stops.
- Rebuild every technical strategy from current bars and current market-wide
  strength, then fully enrich at most ten technically ready names. Remaining
  names explicitly wait for complete factors; a score is not copied forward.
  Existing provider caches and shared request budgets remain in force.
- Save reviews separately as `v12_activation`, linked to the immutable source
  run. Promotions have the **review date**, never the discovery date, and are
  eligible only for subsequent-session execution evaluation. Watch returns
  remain separate from actual/model entries.
- Same-date repeated closes reuse the saved review; database advisory locking
  prevents duplicate cohorts from concurrent saves. An already-promoted
  origin is not reissued daily. Failed reviews surface as maintenance errors.
- No old snapshots or historical returns are relabelled. This fixes missing
  activation plumbing, not a demonstrated improvement in returns. Liquidity,
  the 65-point score floor, evidence freshness and chase controls are unchanged.
