# XRPBurn Dashboard — User Manual

## What is this dashboard?

XRPBurn tracks three daily metrics on the XRP Ledger (XRPL) and refreshes every hour automatically via GitHub Actions. Each metric has a different data source, a different level of accuracy, and different risks of distortion. This manual explains all three so you know exactly how much to trust what you see.

---

## Chart 1 — XRP Burned per Day

### What it shows
The total XRP permanently destroyed each Singapore calendar day (00:00 → 23:59 SGT). Every XRPL transaction pays a small fee — typically 0.000012 XRP — which is not paid to anyone but is removed from the total supply forever.

### How the data is obtained
The script reads the `total_coins` field from two XRPL ledger headers and subtracts:

```
Burn = coins at 00:00 SGT  −  coins at time of run
```

The 00:00 SGT baseline ledger is found by **binary search** — the script queries XRPL nodes to home in on the ledger that closed closest to midnight Singapore time, typically within 30 seconds. This baseline is cached after the first successful find each day and reused for all subsequent hourly runs, so the midnight anchor never drifts or gets overwritten by a manual run.

### Accuracy — ⭐⭐⭐⭐⭐ Exact

This is the most trustworthy metric on the dashboard. `total_coins` is an exact integer in every ledger header. The measurement is:

- **Not an estimate** — it is a direct on-chain subtraction
- **Immune to wash trades** — a 500M XRP internal transfer burns exactly the same tiny fee as a retail payment
- **Immune to price fluctuations** — denominated in XRP, not USD

For partial days (before 23:30 SGT) the bar shows burn accumulated **since midnight so far today** and grows with each hourly run.

### What could distort it

| Risk | Effect | Likelihood |
|---|---|---|
| Binary search fails (all nodes unreachable at midnight) | Bar shows **null** — no fake value is written | Rare |
| Fallback to approximate baseline (~24h ago ledger) | Burn may be slightly over/understated by a few minutes of activity | Occasional |
| XRPL nodes unreachable mid-run | Run skipped, last good value preserved | Rare |

The script **never fills a null burn bar with an estimate**. If the baseline cannot be found, the bar stays empty rather than showing a wrong number.

### Does it self-correct?
**Within the same day: Yes.** Every hourly run uses the same preserved midnight baseline and the latest current coins. The burn value grows correctly through the day.

**Across days: Yes, via backfill (v12+).** If a day ends with `is_partial=True` (because no run occurred after 23:30 SGT), the **next day's first run** retroactively corrects it. The logic is:

```
exact_full_day_burn = yesterday.open_coins_xrp − today.midnight_coins
```

Both values are midnight-boundary ledger readings, so the result is an **exact** full-day coin delta — not an estimate. The backfilled entry is marked `"backfilled": true` and shows a 🔄 icon on the charts. Transaction counts are also promoted from the partial actual to the projected full-day figure, and category breakdowns are rescaled accordingly.

If the midnight baseline was a fallback approximation rather than the true midnight ledger, that small error in `open_coins_xrp` persists — the backfill corrects the end-of-day boundary but cannot fix a flawed start-of-day baseline.

---

## Chart 2 — Network Load (Categorised USD Millions)

### What it shows
The estimated total economic value flowing through XRPL each day, split into four activity categories:

| Colour | Category | Includes |
|---|---|---|
| 🟢 Green | Settlement | Payment, CheckCreate, CheckCash, CheckCancel |
| 🟠 Orange | DeFi | OfferCreate, OfferCancel, AMMCreate/Deposit/Withdraw/Bid/Vote/Delete |
| 🟣 Purple | Identity | DIDSet, DIDDelete, CredentialCreate/Accept/Delete, DepositPreauth |
| ⚫ Grey | Acct Mgmt | AccountSet, AccountDelete, EscrowCreate/Finish, NFT ops, everything else |
| 🔵 Blue | In Progress | Day has total load but category breakdown not yet available |

### How the data is obtained

**Bar height (total load):** Fetched from CoinGecko's `total_volume` field — the rolling 24-hour XRP trading volume across all major centralised exchanges (Binance, Coinbase, Kraken, etc.), converted to USD millions.

**Bar colours (category split):** The script samples 40 recent XRPL ledgers, classifies every successful transaction by type, computes the proportion of each category, then applies those proportions to the total load figure.

### Accuracy — ⭐⭐⭐ Estimate (±20–30%)

**Total load** is the weakest metric on the dashboard because:
- It measures XRP traded on **centralised exchanges**, not purely on-chain XRPL activity
- It is a **rolling 24-hour window** from the moment of fetch — not a fixed midnight-to-midnight day
- Exchange volumes include legitimate arbitrage and market-making that can inflate the raw number

**Category proportions** are more reliable — ratios are stable across the day and a 40-ledger sample is statistically sufficient. The absolute dollar split per category inherits the ±20–30% uncertainty of the total.

### What could distort it

| Risk | Effect | Self-corrects? |
|---|---|---|
| Exchange volume spike (market event, liquidation cascade) | Bar height sharply elevated for that rolling 24h window | Partially — next day's bar returns to normal |
| Sampling window catches a DEX arbitrage burst | DeFi proportion overstated for that run | Yes — next hourly run re-samples |
| CoinGecko API unavailable | Total load stored as null, bar stays empty | Yes — next successful run fills it |
| New XRPL transaction types added by protocol upgrade | Falls into Acct Mgmt by default | No — requires a code update to classify |

### Does it self-correct?
**Category split: Yes, every hourly run.** The 40-ledger sample refreshes with each run. By 23:30 SGT the proportions reflect the most recent network conditions.

**Total load: No.** Each run overwrites today's load with the current CoinGecko rolling 24h figure. Once a day is marked complete, the load value is frozen. There is no retroactive correction from a "true" daily total.

---

## Chart 3 — Transaction Breakdown (Millions)

### What it shows
The number of XRPL transactions processed, split by the same four categories. **Partial days show actual transactions since midnight SGT.** Complete days show the projected full-day total.

### How the data is obtained

The script already has two ledger sequence numbers from the burn calculation — the midnight ledger and the current ledger. These are reused to derive the tx count at zero extra API cost:

**For partial days (actual count since midnight):**
```
ledgers_since_midnight = current_ledger_seq − midnight_ledger_seq
avg_tx_per_ledger      = total txs in 40-ledger sample ÷ ledgers sampled
                         (capped at 120 tx/ledger to prevent burst inflation)

Actual txs so far = ledgers_since_midnight × avg_tx_per_ledger
```

**For complete days (full-day projection):**
```
real_ledgers_per_day = ledgers_since_midnight × (24 ÷ hours_elapsed)
Projected daily txs  = avg_tx_per_ledger × real_ledgers_per_day
```

The `real_ledgers_per_day` figure is **computed fresh each day** from the actual measured window — not a hardcoded assumption. If XRPL runs at 17,200 ledgers today and 18,100 tomorrow, each day uses its own measured rate.

### Accuracy

**Actual count (partial days): ⭐⭐⭐⭐ Good (±5–10%)**

The ledger count since midnight is exact (two known integers). The only approximation is the per-ledger transaction average from 40 sampled ledgers. Because transaction counts per ledger follow a fairly narrow distribution (typically 60–120), a 40-ledger sample gives a reliable average.

**Projected full-day count (complete days): ⭐⭐⭐ Moderate (±10–15%)**

Projection assumes the current rate holds for the rest of the day. Evening activity patterns, end-of-day settlement bursts, or overnight quietness all introduce error. The 120 tx/ledger cap prevents a burst sample from producing an absurd projection.

### What could distort it

| Risk | Effect | Self-corrects? |
|---|---|---|
| Spam attack or bot burst during 40-ledger sample | avg_tx_per_ledger elevated; capped at 120 limits damage | Yes — next hourly run re-samples |
| Very quiet overnight sample captured at 23:30 SGT | Final complete-day figure understated | No — day is then frozen |
| Midnight ledger binary search failed | `ledgers_since_midnight` unavailable, falls back to projected mode | Partially — projection still runs |
| High-activity event concentrated in SGT evening | Early-day projections understate final total | Yes — each run updates until 23:30 |

### Does it self-correct?
**Yes, every hourly run for the current day.** Actual count grows naturally as ledgers accumulate. The projection recalibrates with each new sample. By 23:30 SGT the final value is written and the day is frozen.

**No for past days.** Historical entries do not update once complete.

---

## Bar Visual Guide

| Appearance | Meaning |
|---|---|
| **Solid, full colour** | Complete day — data is final (after 23:30 SGT) |
| **Semi-transparent + diagonal hatch** | Partial day — data is live and updates every hour |
| **Blue "In Progress"** | Total exists but no category breakdown available yet |
| **Empty (no bar)** | No data — XRPL unreachable or day has not started |
| **🎈 Balloon icon** | Simulated fallback — all XRPL nodes were unreachable |
| **🔄 Recycle icon** | Backfilled — partial day retroactively completed using next day's midnight baseline |

Hover over any bar for exact values. Partial-day bars also show the projected full-day figure in the tooltip.

---

## Hourly Update Schedule

| Event | Time (SGT) | What happens |
|---|---|---|
| Scheduled run | Every hour `:00` | Fetches live data, overwrites today's entry |
| First run of the day | ~00:00 SGT | Binary search finds midnight ledger, caches baseline |
| Manual "Update Now" | Any time | Identical to scheduled run — burn baseline is protected |
| Day marked complete | 23:30 SGT | `is_partial` flips false, bars become solid, entry frozen |
| Backfill check | Each run | If yesterday is still `is_partial=True`, backfills with exact burn and projected tx |

---

## Metric Accuracy Summary

| Metric | Source | Actual or Estimate | Accuracy | Self-corrects today? |
|---|---|---|---|---|
| XRP Burned | XRPL `total_coins` delta | **Exact** | ±0.000001 XRP | ✅ Every hour + next-day backfill |
| Load USD (total) | CoinGecko 24h exchange volume | Estimate | ±20–30% | ⚠️ Overwrites each run, no true correction |
| Load (categories) | XRPL 40-ledger sample proportions | Estimate | ±5% on ratios | ✅ Every hour |
| Tx count (partial day) | Ledger delta × sampled average | Near-actual | ±5–10% | ✅ Every hour |
| Tx count (complete day) | Projected from sampled rate | Estimate | ±10–15% | ✅ Until 23:30 SGT |
| Tx categories | XRPL 40-ledger sample proportions | Estimate | ±5% on ratios | ✅ Every hour |

**Bottom line:** Burn is the ground truth — read it with full confidence. Load and transaction counts are reliable trend indicators but carry estimation uncertainty. Use them to spot patterns and shifts over days and weeks, not as precise absolute figures.
