"""Prospective early watch lane. Cached evidence only; never an order signal.

Revenue/ownership timestamps are exposed. These are current-cache screens,
not historical point-in-time backtests. Only saved prospective observations
are used for evaluation, separately from executed trade performance.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date
from math import isfinite
from typing import Any, Mapping, Sequence

from .connection import stock_database
from .v12 import round_tw_price

PRELAUNCH_MODEL = 'V12.4-PRELAUNCH-1'


def next_tick(price: float) -> float:
    tick = .01 if price < 10 else .05 if price < 50 else .1 if price < 100 else .5 if price < 500 else 1 if price < 1000 else 5
    return round_tw_price(price + tick)


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def mapping(value):
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


async def cached_evidence(symbols: list[str], as_of: date) -> dict[str, dict]:
    # Bulk reads reuse weekly fundamental/ownership data, without FinMind calls.
    result = {symbol: {'revenues': [], 'ownership': [], 'features': {}} for symbol in symbols}
    if not symbols or as_of is None:
        return result
    async with stock_database.acquire() as connection:
        revenues = await connection.fetch('''
            SELECT * FROM monthly_revenue WHERE symbol=ANY($1::varchar[])
              AND revenue_month <= $2 AND revenue_month >= $2 - INTERVAL '6 months'
            ORDER BY symbol,revenue_month DESC
        ''', symbols, as_of)
        ownership = await connection.fetch('''
            SELECT * FROM tdcc_distribution_snapshots WHERE symbol=ANY($1::varchar[])
              AND snapshot_date <= $2 AND snapshot_date >= $2 - INTERVAL '35 days'
            ORDER BY symbol,snapshot_date DESC
        ''', symbols, as_of)
        factors = await connection.fetch('''
            SELECT DISTINCT ON(symbol) symbol,trade_date,features FROM daily_factor_snapshots
            WHERE symbol=ANY($1::varchar[]) AND trade_date <= $2
              AND trade_date >= $2 - INTERVAL '7 days'
            ORDER BY symbol,trade_date DESC
        ''', symbols, as_of)
        has_history = await connection.fetchval("SELECT to_regclass('institutional_daily_history')")
        institutional = await connection.fetch('''
            SELECT symbol,trade_date,net_shares,source FROM institutional_daily_history
            WHERE symbol=ANY($1::varchar[]) AND trade_date <= $2
              AND trade_date >= $2 - INTERVAL '14 days'
            ORDER BY symbol,trade_date
        ''', symbols, as_of) if has_history else []
    for record in revenues:
        result[str(record['symbol'])]['revenues'].append(dict(record))
    for record in ownership:
        result[str(record['symbol'])]['ownership'].append(dict(record))
    for record in factors:
        target = result[str(record['symbol'])]
        target['features'] = mapping(record['features'])
        target['factorDate'] = str(record['trade_date'])
    for record in institutional:
        target = result[str(record['symbol'])]
        chip = target['features'].setdefault('chip', {})
        existing = {str(r['date']):r for r in chip.get('institutionalDailyNetShares', [])}
        existing[str(record['trade_date'])] = {'date':str(record['trade_date']), 'netShares':record['net_shares'], 'source':record['source']}
        chip['institutionalDailyNetShares'] = [existing[d] for d in sorted(existing)]
        chip['institutionalAsOfDate'] = max(existing)
        chip['institutionalHistorySources'] = sorted({r.get('source') for r in existing.values() if r.get('source')})
        chip['institutionalHistorySource'] = 'Market-wide history; per-row sources disclosed'
    return result


def evaluate_prelaunch(row: Mapping, evidence: Mapping, profile: Mapping, config: Any) -> dict:
    as_of = day(row.get('trade_date'))
    missing, rejected, reasons = [], [], []
    revenues = list(evidence.get('revenues') or [])[:3]
    ownership = list(evidence.get('ownership') or [])[:3]
    features = evidence.get('features') or {}
    months = [day(r.get('revenue_month')) for r in revenues]
    yoy = [number(r.get('yearly_change_percent')) for r in revenues]
    consecutive = len(months) == 3 and all(months) and all(
        (months[i].year * 12 + months[i].month) -
        (months[i+1].year * 12 + months[i+1].month) == 1 for i in range(2))
    revenue_fresh = bool(months and months[0] and as_of and 0 <= (as_of-months[0]).days <= 80)
    if not consecutive or any(y is None for y in yoy) or not revenue_fresh:
        missing.append('recentThreeConsecutiveRevenueMonths')
    fundamental_ok = (consecutive and revenue_fresh and all(y is not None for y in yoy)
                      and yoy[0] >= 10 and yoy[0] > yoy[1] > yoy[2])
    if fundamental_ok:
        reasons.append('連續三個月營收年增率加速，最新年增至少10%')
    else:
        rejected.append('fundamentalAcceleration')

    weeks = [day(r.get('snapshot_date')) for r in ownership]
    large = [number(r.get('over_400_lots_percent')) for r in ownership]
    small = [number(r.get('under_100_lots_percent')) for r in ownership]
    ownership_valid = (len(weeks) == 3 and all(weeks) and as_of
                       and 0 <= (as_of-weeks[0]).days <= 10
                       and all(5 <= (weeks[i]-weeks[i+1]).days <= 9 for i in range(2))
                       and all(v is not None for v in large+small))
    ownership_ok = bool(ownership_valid and large[0] > large[1] > large[2]
                        and small[0] < small[1] < small[2])
    if not ownership_valid:
        missing.append('threeWeeklyOwnershipSnapshots')
    if ownership_ok:
        reasons.append('連續兩週400張以上持股增加、100張以下持股減少')

    chip = features.get('chip') or {}
    daily = sorted((r for r in chip.get('institutionalDailyNetShares') or []
                    if day(r.get('date')) and as_of and day(r['date']) <= as_of),
                   key=lambda r: str(r['date']))[-5:]
    nets = [number(r.get('netShares')) for r in daily]
    inst_valid = (len(daily) == 5 and all(n is not None for n in nets)
                  and as_of and 0 <= (as_of-day(daily[-1]['date'])).days <= 4
                  and (day(daily[-1]['date'])-day(daily[0]['date'])).days <= 10)
    inst_ok = bool(inst_valid and sum(n > 0 for n in nets) >= 3
                   and sum(nets) > 0 and sum(nets[-2:]) > 0)
    if not inst_valid:
        missing.append('fiveRecentInstitutionalSessions')
    if inst_ok:
        reasons.append('近五個已公布交易日法人至少三日買超，合計與最近兩日均買超')
    if not (ownership_ok or inst_ok):
        rejected.append('persistentAccumulation')

    close, ma20, prev_ma20, prior = [number(row.get(k)) for k in ('close','ma20','prev_ma20','prior_high20')]
    r10 = number((profile.get('returns') or {}).get('10'))
    r20 = number((profile.get('returns') or {}).get('20'))
    vr = number(row.get('volume_ratio'))
    if any(v is None or v <= 0 for v in (close,ma20,prev_ma20,prior,vr)) or r10 is None or r20 is None:
        missing.append('basePriceVolumeHistory')
        setup_ok = False
    else:
        setup_ok = (ma20 >= prev_ma20 and ma20 <= close <= ma20 * 1.06
                    and -5 <= r10 <= 8 and -8 <= r20 <= 15
                    and close <= prior and 0.35 <= vr <= 1.5)
    if not setup_ok:
        rejected.append('notAnUnextendedBase')
    else:
        reasons.append('整理守住上彎MA20，尚未突破前20日高點且未過度延伸')
    sector = profile.get('sectorContext') or {}
    sector_breadth = number(sector.get('aboveMA20Percent'))
    sector_ok = sector_breadth is not None and sector_breadth >= 50
    if sector_breadth is None:
        missing.append('sectorBreadth')
    if not sector_ok:
        rejected.append('sectorSupport')
    # Quiet bases need liquidity on average, not a fresh volume explosion.
    daily_volume = number(row.get('volume')) or 0
    average_volume = number(row.get('volume_ma20')) or 0
    liquid = (daily_volume >= config.min_daily_volume_lots * 1000 * .25
              and average_volume >= config.min_average_volume20_lots * 1000
              and average_volume * (close or 0) >= config.effective_min_trade_value)
    if not liquid:
        rejected.append('liquidity')
    if close is None or close > config.primary_max_price:
        rejected.append('priceCap')
    if (features.get('event') or {}).get('hardRisk'):
        rejected.append('knownHardRisk')
    score = 30 * bool(fundamental_ok) + 30 * bool(ownership_ok or inst_ok) + 25 * bool(setup_ok) + 15 * bool(sector_ok)
    return dict(modelRevision=PRELAUNCH_MODEL, eligible=not rejected, score=score,
                reasons=reasons, failedRules=rejected, missingInputs=missing,
                revenueMonths=[str(m) for m in months], revenueYoYPercent=yoy,
                ownershipDates=[str(w) for w in weeks], over400LotsPercent=large,
                under100LotsPercent=small, ownershipAccumulation=ownership_ok,
                institutionalAccumulation=inst_ok, institutionalDailyNetShares=daily,
                cachedFactorDate=evidence.get('factorDate'), sectorAboveMA20Percent=sector_breadth,
                observationOnly=True, historicalPointInTimeBacktest=False)


def screen_prelaunch(rows: Sequence[Mapping], evidence: Mapping, profiles: Mapping, config: Any, limit=10) -> dict:
    candidates, missing, rejected = [], Counter(), Counter()
    for row in rows:
        symbol = str(row.get('symbol'))
        p = evaluate_prelaunch(row, evidence.get(symbol, {}), profiles.get(symbol, {}), config)
        missing.update(p['missingInputs'])
        rejected.update(p['failedRules'])
        if not p['eligible']:
            continue
        prior = float(row['prior_high20'])
        trigger = next_tick(prior)
        candidates.append(dict(symbol=symbol, name=row.get('name'), market=row.get('market'),
                               strategy='prelaunch_watch', strategies=['prelaunch_watch'],
                               close=float(row['close']), trade_date=str(row['trade_date']),
                               total_score=p['score'], ranking_score=p['score'],
                               actionCode='EARLY_WATCH_ONLY', forwardQualified=False,
                               reasons=p['reasons'], prelaunchProfile=p,
                               confirmationTrigger=trigger,
                               confirmationConditions=['後續收盤突破觀察日前20日高點',
                                                       '當日量比至少1.2', '重新產生有效買點與風險計畫後才可進場'],
                               observationReferencePrice=float(row['close'])))
    candidates.sort(key=lambda r: (r['total_score'], (profiles[r['symbol']].get('sectorResonanceScore') or 0)), reverse=True)
    return dict(modelRevision=PRELAUNCH_MODEL, candidateCount=len(candidates),
                candidates=candidates[:limit], missingInputCounts=dict(missing),
                rejectionCounts=dict(rejected), scope='FULL_COMMON_STOCK_UNIVERSE_INDEPENDENT_OF_STRENGTH_SHORTLIST',
                note='提前觀察不等於買進；資料缺漏不補分。不使用單日買超或單月成長冒充持續累積。')


def observation_metrics(snapshot: Mapping, bars: Sequence[Mapping]) -> dict:
    reference = number(snapshot.get('observationReferencePrice'))
    trigger = number(snapshot.get('confirmationTrigger'))
    ordered = sorted((b for b in bars if str(b['trade_date'])[:10] > str(snapshot['trade_date'])[:10]),
                     key=lambda b: str(b['trade_date']))[:20]
    if not reference or not trigger:
        return {'status': 'INVALID_REFERENCE'}
    breakout = next((i+1 for i,b in enumerate(ordered) if float(b['close']) >= trigger
                     and (number(b.get('volume_ratio')) or 0) >= 1.2), None)
    return dict(status='MATURED' if len(ordered) >= 20 else 'PENDING', observedSessions=len(ordered),
                sessionsToConfirmedBreakout=breakout,
                noBreakoutAfter20Sessions=(breakout is None) if len(ordered) >= 20 else None,
                returns={str(h): round((float(ordered[h-1]['close'])/reference-1)*100,4)
                         if len(ordered)>=h else None for h in (5,10,20)},
                maximumFavorablePercent=round(max((float(b['high'])/reference-1)*100 for b in ordered),4) if ordered else None,
                maximumAdversePercent=round(min((float(b['low'])/reference-1)*100 for b in ordered),4) if ordered else None,
                basis='FIRST_OBSERVATION_CLOSE_NOT_EXECUTED_TRADE_RETURN')


async def prelaunch_performance(as_of: date) -> dict:
    if as_of is None:
        return {'modelRevision': PRELAUNCH_MODEL, 'samples': 0, 'maturedSamples': 0,
                'confirmedBreakoutRate20': None, 'observations': []}
    async with stock_database.acquire() as connection:
        records = await connection.fetch('''
            WITH first_observation AS (
                SELECT DISTINCT ON(c.symbol) c.symbol,r.run_date,c.snapshot
                FROM radar_runs r JOIN radar_candidates c ON c.radar_run_id=r.id
                WHERE r.strategy='prelaunch_watch' AND r.status='completed' AND r.run_date <= $1
                  AND c.snapshot->'prelaunchProfile'->>'modelRevision'=$2
                ORDER BY c.symbol,r.run_date,r.id
            )
            SELECT f.*,COALESCE(b.bars,'[]'::jsonb) AS bars FROM first_observation f
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(x ORDER BY x.trade_date) AS bars FROM (
                    SELECT d.trade_date,d.close,d.high,d.low,i.volume_ratio
                    FROM daily_bars d LEFT JOIN daily_indicators i
                      ON i.symbol=d.symbol AND i.trade_date=d.trade_date
                    WHERE d.symbol=f.symbol AND d.trade_date>f.run_date AND d.trade_date <= $1
                    ORDER BY d.trade_date LIMIT 20
                ) x
            ) b ON TRUE
        ''', as_of, PRELAUNCH_MODEL)
    samples = []
    for r in records:
        bars = json.loads(r['bars']) if isinstance(r['bars'], str) else r['bars']
        samples.append(dict(symbol=r['symbol'],firstObservedDate=str(r['run_date']),
                            **observation_metrics(mapping(r['snapshot']),bars)))
    mature = [s for s in samples if s['status']=='MATURED']
    return dict(modelRevision=PRELAUNCH_MODEL, samples=len(samples), maturedSamples=len(mature),
                confirmedBreakoutRate20=round(sum(not s['noBreakoutAfter20Sessions'] for s in mature)/len(mature)*100,2) if mature else None,
                observations=samples, note='每檔本版本首次入選；20交易日完成才計入發動率。觀察價報酬不等於成交績效。')
