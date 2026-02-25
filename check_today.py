"""
Run locally:  python3 check_today.py
Requires:     Python 3 only — no pip installs needed

v2 fixes:
  - Payment volume now uses MEDIAN tx size × count (not sum) to avoid whale skew
  - Caps single payment at 10M XRP (exchange internal shuffles excluded)
  - Load USD = realistic daily volume estimate, not raw sum
  - Added annualised burn rate
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

SGT          = timezone(timedelta(hours=8))
UTC          = timezone.utc
RIPPLE_EPOCH = 946684800
MAX_PAYMENT_XRP = 10_000_000   # cap single payment at 10M XRP — above this = internal shuffle


def xrpl_rpc(method, params=None):
    nodes = ["https://xrplcluster.com", "https://xrpl.ws", "https://s2.ripple.com"]
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in nodes:
        try:
            req = urllib.request.Request(node, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "XRPCheck/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                result = json.loads(r.read().decode()).get("result", {})
            if result.get("status") == "success":
                return result
        except Exception as e:
            print(f"  [ERR] {node}: {e}")
    return None


def parse_ledger(res):
    if not res:
        return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    if not ld:
        return None
    seq      = int(ld.get("ledger_index") or ld.get("seqNum") or res.get("ledger_index", 0))
    coins    = int(ld["total_coins"]) / 1e6 if ld.get("total_coins") else None
    close_dt = (datetime.fromtimestamp(ld["close_time"] + RIPPLE_EPOCH, UTC)
                if ld.get("close_time") is not None else None)
    return {"seq": seq, "coins": coins, "time": close_dt}


def find_ledger_at(target_utc, current_seq, current_time_utc):
    """Binary-search for ledger closest to target_utc."""
    diff_s  = (target_utc - current_time_utc).total_seconds()
    est_seq = current_seq + int(diff_s / 3.5)
    for attempt in range(4):
        res  = xrpl_rpc("ledger", {"ledger_index": est_seq, "transactions": False})
        info = parse_ledger(res)
        if not info:
            break
        delta_s    = (target_utc - info["time"]).total_seconds()
        correction = int(delta_s / 3.5)
        print(f"  Attempt {attempt+1}: #{info['seq']:,}  "
              f"{info['time'].astimezone(SGT).strftime('%H:%M:%S SGT')}  "
              f"(Δ {delta_s/60:+.1f} min)")
        if abs(correction) < 5:
            return info
        est_seq += correction
    return info


def classify(tx_type):
    if tx_type in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):
        return "settlement"
    if tx_type in ("OfferCreate", "OfferCancel", "AMMCreate", "AMMDeposit",
                   "AMMWithdraw", "AMMBid", "AMMVote", "AMMDelete"):
        return "defi"
    if tx_type in ("DIDSet", "DIDDelete", "CredentialCreate",
                   "CredentialAccept", "CredentialDelete", "DepositPreauth"):
        return "identity"
    return "acct_mgmt"


def main():
    now_sgt      = datetime.now(SGT)
    now_utc      = now_sgt.astimezone(UTC)
    midnight_sgt = datetime(now_sgt.year, now_sgt.month, now_sgt.day, 0, 0, 0, tzinfo=SGT)
    midnight_utc = midnight_sgt.astimezone(UTC)
    hours_elapsed = (now_utc - midnight_utc).total_seconds() / 3600

    print(f"\n{'='*58}")
    print(f"  XRPBurn — live snapshot")
    print(f"  Window: 00:00 SGT {midnight_sgt.strftime('%d %b %Y')} "
          f"→ {now_sgt.strftime('%H:%M SGT')}  ({hours_elapsed:.2f}h)")
    print(f"{'='*58}\n")

    # ── Current ledger ─────────────────────────────────────────
    print("--- Current validated ledger ---")
    current = parse_ledger(xrpl_rpc("ledger", {"ledger_index": "validated",
                                                "transactions": False}))
    if not current or not current["coins"]:
        print("[FAIL] Cannot fetch current ledger.")
        return
    print(f"  Seq:   #{current['seq']:,}")
    print(f"  Time:  {current['time'].astimezone(SGT).strftime('%H:%M:%S SGT')}")
    print(f"  Coins: {current['coins']:,.4f} XRP")

    # ── Midnight ledger ────────────────────────────────────────
    print("\n--- Finding midnight SGT ledger ---")
    start = find_ledger_at(midnight_utc, current["seq"], current["time"])
    if not start or not start["coins"]:
        print("[FAIL] Cannot find midnight ledger.")
        return
    print(f"  → #{start['seq']:,}  {start['time'].astimezone(SGT).strftime('%H:%M:%S SGT')}"
          f"  coins={start['coins']:,.4f} XRP")

    burned        = start["coins"] - current["coins"]
    ledgers_in_win = current["seq"] - start["seq"]

    # ── Sample ledgers ─────────────────────────────────────────
    SAMPLE = 60
    print(f"\n--- Sampling {SAMPLE} ledgers ---")
    tx_counts        = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_amounts  = []   # capped individual payment sizes in XRP
    fee_drops        = 0
    ledgers_ok       = 0
    tx_per_ledger    = []

    for i in range(SAMPLE):
        res = xrpl_rpc("ledger", {"ledger_index": current["seq"] - i,
                                   "transactions": True, "expand": True})
        if not res:
            continue
        txs = res.get("ledger", {}).get("transactions", [])
        if not txs:
            continue
        ledgers_ok += 1
        ledger_tx_count = 0
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            if isinstance(meta, dict):
                if meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
                    continue
            cat = classify(tx.get("TransactionType", ""))
            tx_counts[cat] += 1
            ledger_tx_count += 1
            fee_drops += int(tx.get("Fee", 0))
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):  # XRP in drops
                    xrp_amt = int(amt) / 1e6
                    # Cap at MAX_PAYMENT_XRP — above this = exchange internal shuffle, not real load
                    if xrp_amt <= MAX_PAYMENT_XRP:
                        payment_amounts.append(xrp_amt)
        tx_per_ledger.append(ledger_tx_count)
        time.sleep(0.03)

    total_sampled = sum(tx_counts.values())
    scale         = ledgers_in_win / max(ledgers_ok, 1)

    # Median tx size × payment count (robust to whale outliers)
    payment_amounts.sort()
    if payment_amounts:
        median_payment  = payment_amounts[len(payment_amounts) // 2]
        payment_count   = tx_counts["settlement"]
        # Scale payment count to window, multiply by median size
        payment_count_window = int(payment_count * scale)
        payment_vol_window   = median_payment * payment_count_window
    else:
        payment_vol_window   = 0
        payment_count_window = 0
        median_payment       = 0

    # Also compute raw sum for comparison
    raw_sum_xrp = sum(payment_amounts) * scale

    # Fee-based burn estimate for cross-check
    fee_burn_est  = fee_drops * scale / 1e6

    total_tx_win  = int(total_sampled * scale)
    tx_cats_win   = {k: int(v * scale) for k, v in tx_counts.items()}
    avg_tx_ledger = sum(tx_per_ledger) / max(len(tx_per_ledger), 1)

    # Price
    print("\n--- XRP price ---")
    try:
        pd = json.loads(urllib.request.urlopen(
            urllib.request.Request(
                "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd",
                headers={"User-Agent": "XRPCheck/2.0"}), timeout=15).read())
        xrp_price = float(pd["ripple"]["usd"])
    except Exception as e:
        xrp_price = 2.30
        print(f"  [WARN] Using fallback ${xrp_price} ({e})")
    print(f"  XRP = ${xrp_price:.4f}")

    load_usd_m       = payment_vol_window * xrp_price / 1e6
    raw_load_usd_m   = raw_sum_xrp * xrp_price / 1e6

    # Annualised burn rate
    burn_per_hour    = burned / max(hours_elapsed, 0.01)
    burn_per_day_est = burn_per_hour * 24
    burn_per_year_est = burn_per_day_est * 365

    # ── Report ─────────────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  RESULTS  |  00:00 → {now_sgt.strftime('%H:%M SGT')}  ({hours_elapsed:.1f}h)")
    print(f"{'='*58}")
    print(f"  XRP price:             ${xrp_price:.4f}")
    print(f"  Ledgers in window:     {ledgers_in_win:,}  (~{avg_tx_ledger:.0f} tx/ledger)")
    print()
    print(f"  🔥 XRP BURNED")
    print(f"     Coin delta:         {burned:.6f} XRP   ← most accurate")
    print(f"     Fee estimate:       {fee_burn_est:.6f} XRP")
    print(f"     Rate:               {burn_per_hour:.4f} XRP/hr  "
          f"→ ~{burn_per_day_est:.2f}/day  "
          f"→ ~{burn_per_year_est:,.0f}/year")
    print()
    print(f"  ⚡ TRANSACTIONS         {total_tx_win:,}")
    for cat, count in tx_cats_win.items():
        pct   = count / total_tx_win * 100 if total_tx_win else 0
        label = {"settlement": "🟢 Settlement",
                 "defi":       "🟠 DeFi",
                 "identity":   "🟣 Identity",
                 "acct_mgmt":  "⚪ Acct Mgmt"}[cat]
        print(f"     {label:<20} {count:>10,}  ({pct:.1f}%)")
    print()
    print(f"  💰 PAYMENT VOLUME (capped ≤10M XRP/tx)")
    print(f"     Median tx size:     {median_payment:,.2f} XRP")
    print(f"     Payment tx count:   {payment_count_window:,}")
    print(f"     Volume (median×n):  {payment_vol_window:,.0f} XRP")
    print(f"     Raw sum (outliers): {raw_sum_xrp:,.0f} XRP  ← inflated by whales")
    print()
    print(f"  📊 LOAD — median method  ${load_usd_m:,.2f}M USD")
    print(f"  📊 LOAD — raw sum        ${raw_load_usd_m:,.2f}M USD  ← do not use")
    tx_total = max(total_sampled, 1)
    print(f"  📊 LOAD BY CATEGORY (median method):")
    for cat in ["settlement", "defi", "identity", "acct_mgmt"]:
        share = tx_counts[cat] / tx_total
        label = {"settlement": "🟢 Settlement", "defi": "🟠 DeFi",
                 "identity":   "🟣 Identity",   "acct_mgmt": "⚪ Acct Mgmt"}[cat]
        print(f"     {label:<20} ${load_usd_m * share:>10,.2f}M  ({share*100:.1f}%)")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    main()
