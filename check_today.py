"""
Run locally:  python3 check_today.py
Requires:     python3 only — no pip installs needed
Queries XRPL directly for XRP burned, load, and transactions
since 00:00 SGT on Feb 25, 2026.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

SGT          = timezone(timedelta(hours=8))
UTC          = timezone.utc
RIPPLE_EPOCH = 946684800   # Jan 1 2000 00:00 UTC in unix seconds

XRPL_NODES = [
    "https://xrplcluster.com",
    "https://xrpl.ws",
    "https://s2.ripple.com",
]


def xrpl_rpc(method, params=None):
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in XRPL_NODES:
        try:
            req = urllib.request.Request(
                node, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "XRPCheck/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            result = data.get("result", {})
            if result.get("status") == "success":
                return result
        except Exception as e:
            print(f"  [ERR] {node}: {e}")
    return None


def get_ledger(index):
    return xrpl_rpc("ledger", {
        "ledger_index": index,
        "transactions": False,
        "expand":       False,
    })


def get_ledger_with_txs(index):
    return xrpl_rpc("ledger", {
        "ledger_index": index,
        "transactions": True,
        "expand":       True,
    })


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


def parse_ledger(res):
    """Extract seq, total_coins, close_time from a ledger RPC response."""
    if not res:
        return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    if not ld:
        return None
    seq        = int(ld.get("ledger_index") or ld.get("seqNum") or
                     res.get("ledger_index", 0))
    raw_coins  = ld.get("total_coins")
    coins      = int(raw_coins) / 1e6 if raw_coins else None
    close_ts   = ld.get("close_time")
    close_dt   = (datetime.fromtimestamp(close_ts + RIPPLE_EPOCH, UTC)
                  if close_ts is not None else None)
    return {"seq": seq, "coins": coins, "time": close_dt, "raw": ld}


def find_ledger_at(target_utc, current_seq, current_time_utc):
    """Estimate then refine the ledger closest to target_utc."""
    diff_s        = (target_utc - current_time_utc).total_seconds()
    est_seq       = current_seq + int(diff_s / 3.5)

    print(f"  Estimated ledger at target: #{est_seq} "
          f"(offset {diff_s/3600:.2f}h from current)")

    # Two-pass refinement
    for attempt in range(3):
        res = get_ledger(est_seq)
        info = parse_ledger(res)
        if not info:
            print(f"  [WARN] Could not fetch ledger #{est_seq}")
            break
        delta_s = (target_utc - info["time"]).total_seconds()
        correction = int(delta_s / 3.5)
        print(f"  Attempt {attempt+1}: #{info['seq']} closed at "
              f"{info['time'].astimezone(SGT).strftime('%H:%M:%S SGT')} "
              f"(delta {delta_s/60:+.1f} min → adjust {correction})")
        if abs(correction) < 3:
            break
        est_seq += correction

    return info


def main():
    now_sgt = datetime.now(SGT)
    now_utc = now_sgt.astimezone(UTC)

    midnight_sgt = datetime(now_sgt.year, now_sgt.month, now_sgt.day,
                            0, 0, 0, tzinfo=SGT)
    midnight_utc = midnight_sgt.astimezone(UTC)
    hours_elapsed = (now_utc - midnight_utc).total_seconds() / 3600

    print(f"\n{'='*58}")
    print(f"  XRPBurn — live snapshot")
    print(f"  Window: 00:00 SGT {midnight_sgt.strftime('%d %b %Y')} "
          f"→ {now_sgt.strftime('%H:%M SGT')}  ({hours_elapsed:.2f}h)")
    print(f"{'='*58}\n")

    # ── 1. Current ledger ──────────────────────────────────────
    print("--- Step 1: Current validated ledger ---")
    current_res  = get_ledger("validated")
    current_info = parse_ledger(current_res)
    if not current_info or not current_info["coins"]:
        print("[FAIL] Cannot fetch current ledger. Check internet connection.")
        return

    print(f"  Seq:         #{current_info['seq']:,}")
    print(f"  Time:        {current_info['time'].astimezone(SGT).strftime('%H:%M:%S SGT')}")
    print(f"  Total coins: {current_info['coins']:,.4f} XRP")

    # ── 2. Ledger at midnight SGT ──────────────────────────────
    print("\n--- Step 2: Ledger at midnight SGT ---")
    start_info = find_ledger_at(midnight_utc, current_info["seq"],
                                current_info["time"])
    if not start_info or not start_info["coins"]:
        print("[FAIL] Cannot find midnight ledger.")
        return

    print(f"\n  Start ledger: #{start_info['seq']:,}  "
          f"({start_info['time'].astimezone(SGT).strftime('%H:%M:%S SGT')})")
    print(f"  Total coins:  {start_info['coins']:,.4f} XRP")

    # ── 3. XRP burned = coin delta ─────────────────────────────
    burned_delta = start_info["coins"] - current_info["coins"]
    ledgers_in_window = current_info["seq"] - start_info["seq"]

    print(f"\n--- Step 3: Burn ---")
    print(f"  Ledgers in window: {ledgers_in_window:,}")
    print(f"  Coins at start:    {start_info['coins']:,.6f} XRP")
    print(f"  Coins now:         {current_info['coins']:,.6f} XRP")
    print(f"  XRP BURNED:        {burned_delta:.6f} XRP")

    # ── 4. Sample recent ledgers for tx classification ─────────
    SAMPLE = 60
    print(f"\n--- Step 4: Sampling {SAMPLE} ledgers for tx breakdown ---")
    tx_counts       = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_vol_xrp = 0.0
    fee_drops       = 0
    ledgers_ok      = 0

    for i in range(SAMPLE):
        res = get_ledger_with_txs(current_info["seq"] - i)
        if not res:
            continue
        txs = res.get("ledger", {}).get("transactions", [])
        if not txs:
            continue
        ledgers_ok += 1
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            if isinstance(meta, dict):
                if meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
                    continue
            cat = classify(tx.get("TransactionType", ""))
            tx_counts[cat] += 1
            fee_drops += int(tx.get("Fee", 0))
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):
                    payment_vol_xrp += int(amt) / 1e6
        time.sleep(0.03)

    total_sampled = sum(tx_counts.values())
    print(f"  Fetched {ledgers_ok}/{SAMPLE} ledgers | {total_sampled:,} txs sampled")

    # Scale sample to full window
    scale             = ledgers_in_window / max(ledgers_ok, 1)
    total_tx_window   = int(total_sampled * scale)
    payment_vol_window = payment_vol_xrp * scale
    fee_est_window    = fee_drops * scale / 1e6
    tx_cats_window    = {k: int(v * scale) for k, v in tx_counts.items()}

    # ── 5. XRP price + load USD ────────────────────────────────
    print("\n--- Step 5: XRP price ---")
    try:
        pd = json.loads(urllib.request.urlopen(
            urllib.request.Request(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=ripple&vs_currencies=usd",
                headers={"User-Agent": "XRPCheck/1.0"}
            ), timeout=15).read())
        xrp_price = float(pd["ripple"]["usd"])
        print(f"  XRP = ${xrp_price:.4f}")
    except Exception as e:
        xrp_price = 2.30
        print(f"  [WARN] CoinGecko failed ({e}) — using ${xrp_price}")

    load_usd_m = payment_vol_window * xrp_price / 1e6

    # ── Final report ───────────────────────────────────────────
    print(f"\n{'='*58}")
    print(f"  RESULTS  |  00:00 → {now_sgt.strftime('%H:%M SGT')}  "
          f"({hours_elapsed:.1f}h)")
    print(f"{'='*58}")
    print(f"  XRP price (live):      ${xrp_price:.4f}")
    print(f"  Ledgers in window:     {ledgers_in_window:,}")
    print()
    print(f"  🔥 XRP BURNED          {burned_delta:.6f} XRP  (coin delta)")
    print(f"     (fee estimate):     {fee_est_window:.6f} XRP")
    print()
    print(f"  ⚡ TRANSACTIONS        {total_tx_window:,}")
    for cat, count in tx_cats_window.items():
        pct = count / total_tx_window * 100 if total_tx_window else 0
        label = {"settlement":"🟢 Settlement","defi":"🟠 DeFi",
                 "identity":"🟣 Identity","acct_mgmt":"⚪ Acct Mgmt"}[cat]
        print(f"     {label:<20} {count:>10,}  ({pct:.1f}%)")
    print()
    print(f"  💰 PAYMENT VOLUME      {payment_vol_window:,.0f} XRP")
    print(f"  📊 LOAD (USD):         ${load_usd_m:,.2f}M")
    tx_total = max(total_sampled, 1)
    print(f"  📊 LOAD BY CATEGORY:")
    for cat in tx_counts:
        share = tx_counts[cat] / tx_total
        label = {"settlement":"🟢 Settlement","defi":"🟠 DeFi",
                 "identity":"🟣 Identity","acct_mgmt":"⚪ Acct Mgmt"}[cat]
        print(f"     {label:<20} ${load_usd_m * share:>10,.2f}M  ({share*100:.1f}%)")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    main()
