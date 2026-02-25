"""
Run locally:  python3 check_today.py
Requires:     Python 3 only — no pip installs needed

v3 fixes:
  - Volume uses trimmed mean (drop bottom 10% dust + top 1% whales)
  - Reports percentile breakdown so you can see payment size distribution
  - Raw sum shown for reference but trimmed mean used for load calc
"""

import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta

SGT          = timezone(timedelta(hours=8))
UTC          = timezone.utc
RIPPLE_EPOCH = 946684800


def xrpl_rpc(method, params=None):
    nodes = ["https://xrplcluster.com", "https://xrpl.ws", "https://s2.ripple.com"]
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in nodes:
        try:
            req = urllib.request.Request(node, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "XRPCheck/3.0"})
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
    diff_s  = (target_utc - current_time_utc).total_seconds()
    est_seq = current_seq + int(diff_s / 3.5)
    for attempt in range(5):
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


def trimmed_mean(values, drop_bottom_pct=0.10, drop_top_pct=0.01):
    """Mean after removing bottom X% (dust) and top Y% (whales)."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    lo = int(n * drop_bottom_pct)
    hi = int(n * (1 - drop_top_pct))
    trimmed = s[lo:hi]
    return sum(trimmed) / len(trimmed) if trimmed else 0.0


def percentile(values, p):
    if not values:
        return 0
    s = sorted(values)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s)-1)]


def main():
    now_sgt      = datetime.now(SGT)
    now_utc      = now_sgt.astimezone(UTC)
    midnight_sgt = datetime(now_sgt.year, now_sgt.month, now_sgt.day, 0, 0, 0, tzinfo=SGT)
    midnight_utc = midnight_sgt.astimezone(UTC)
    hours_elapsed = (now_utc - midnight_utc).total_seconds() / 3600

    print(f"\n{'='*60}")
    print(f"  XRPBurn — live snapshot")
    print(f"  Window: 00:00 SGT {midnight_sgt.strftime('%d %b %Y')} "
          f"→ {now_sgt.strftime('%H:%M SGT')}  ({hours_elapsed:.2f}h)")
    print(f"{'='*60}\n")

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
    print(f"  → #{start['seq']:,}  "
          f"{start['time'].astimezone(SGT).strftime('%H:%M:%S SGT')}  "
          f"coins={start['coins']:,.4f} XRP")

    burned         = start["coins"] - current["coins"]
    ledgers_in_win = current["seq"] - start["seq"]
    burn_per_hour  = burned / max(hours_elapsed, 0.01)

    # ── Sample ledgers ─────────────────────────────────────────
    SAMPLE = 60
    print(f"\n--- Sampling {SAMPLE} ledgers ---")
    tx_counts       = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    all_payments    = []   # all XRP payment amounts (raw, for analysis)
    fee_drops       = 0
    ledgers_ok      = 0
    tx_per_ledger   = []

    for i in range(SAMPLE):
        res = xrpl_rpc("ledger", {"ledger_index": current["seq"] - i,
                                   "transactions": True, "expand": True})
        if not res:
            continue
        txs = res.get("ledger", {}).get("transactions", [])
        if not txs:
            continue
        ledgers_ok += 1
        ltx = 0
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            if isinstance(meta, dict):
                if meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
                    continue
            cat = classify(tx.get("TransactionType", ""))
            tx_counts[cat] += 1
            ltx += 1
            fee_drops += int(tx.get("Fee", 0))
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):
                    all_payments.append(int(amt) / 1e6)
        tx_per_ledger.append(ltx)
        time.sleep(0.03)

    total_sampled = sum(tx_counts.values())
    scale         = ledgers_in_win / max(ledgers_ok, 1)
    avg_tx_ledger = sum(tx_per_ledger) / max(len(tx_per_ledger), 1)

    # Payment volume — trimmed mean × scaled count
    tmean            = trimmed_mean(all_payments, drop_bottom_pct=0.10, drop_top_pct=0.01)
    payment_count_w  = int(tx_counts["settlement"] * scale)
    volume_trimmed   = tmean * payment_count_w

    # Raw sum for reference (scaled)
    raw_sum_xrp      = sum(all_payments) * scale
    fee_burn_est     = fee_drops * scale / 1e6
    total_tx_win     = int(total_sampled * scale)
    tx_cats_win      = {k: int(v * scale) for k, v in tx_counts.items()}

    # Payment size distribution
    p10 = percentile(all_payments, 10)
    p50 = percentile(all_payments, 50)
    p90 = percentile(all_payments, 90)
    p99 = percentile(all_payments, 99)
    nonzero = [x for x in all_payments if x > 0.001]
    nonzero_pct = len(nonzero) / max(len(all_payments), 1) * 100

    # XRP price
    print("\n--- XRP price ---")
    try:
        pd = json.loads(urllib.request.urlopen(
            urllib.request.Request(
                "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd",
                headers={"User-Agent": "XRPCheck/3.0"}), timeout=15).read())
        xrp_price = float(pd["ripple"]["usd"])
    except Exception as e:
        xrp_price = 2.30
        print(f"  [WARN] Using fallback ${xrp_price}")
    print(f"  XRP = ${xrp_price:.4f}")

    load_trimmed_m = volume_trimmed   * xrp_price / 1e6
    load_raw_m     = raw_sum_xrp     * xrp_price / 1e6

    # ── Report ─────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULTS  |  00:00 → {now_sgt.strftime('%H:%M SGT')}  ({hours_elapsed:.1f}h)")
    print(f"{'='*60}")
    print(f"  XRP price:              ${xrp_price:.4f}")
    print(f"  Ledgers in window:      {ledgers_in_win:,}  (~{avg_tx_ledger:.0f} tx/ledger avg)")
    print()
    print(f"  🔥 XRP BURNED")
    print(f"     Coin delta:          {burned:.6f} XRP   ← authoritative")
    print(f"     Fee estimate:        {fee_burn_est:.6f} XRP")
    print(f"     Rate:                {burn_per_hour:.4f} XRP/hr"
          f"  →  ~{burn_per_hour*24:.1f}/day"
          f"  →  ~{burn_per_hour*24*365:,.0f}/year")
    print()
    print(f"  ⚡ TRANSACTIONS          {total_tx_win:,}")
    labels = {"settlement":"🟢 Settlement","defi":"🟠 DeFi",
              "identity":"🟣 Identity","acct_mgmt":"⚪ Acct Mgmt"}
    for cat, count in tx_cats_win.items():
        pct = count / total_tx_win * 100 if total_tx_win else 0
        print(f"     {labels[cat]:<20} {count:>10,}  ({pct:.1f}%)")
    print()
    print(f"  💰 PAYMENT SIZE DISTRIBUTION (sampled {len(all_payments):,} payments)")
    print(f"     Non-dust (>0.001 XRP): {nonzero_pct:.1f}% of payments")
    print(f"     p10:  {p10:>15,.4f} XRP")
    print(f"     p50:  {p50:>15,.4f} XRP  (median)")
    print(f"     p90:  {p90:>15,.4f} XRP")
    print(f"     p99:  {p99:>15,.4f} XRP")
    print(f"     Trimmed mean (10%-99%): {tmean:,.4f} XRP  ← used for load calc")
    print()
    print(f"  📊 LOAD (USD) — trimmed mean method")
    print(f"     Volume:   {volume_trimmed:>18,.0f} XRP")
    print(f"     Load:     ${load_trimmed_m:>17,.2f}M  ← use this")
    print()
    print(f"  📊 LOAD (USD) — raw sum (whale-skewed, for reference only)")
    print(f"     Volume:   {raw_sum_xrp:>18,.0f} XRP")
    print(f"     Load:     ${load_raw_m:>17,.2f}M")
    print()
    tx_total = max(total_sampled, 1)
    print(f"  📊 LOAD BY CATEGORY (trimmed method):")
    for cat in ["settlement", "defi", "identity", "acct_mgmt"]:
        share = tx_counts[cat] / tx_total
        print(f"     {labels[cat]:<20} ${load_trimmed_m * share:>10,.2f}M  ({share*100:.1f}%)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
