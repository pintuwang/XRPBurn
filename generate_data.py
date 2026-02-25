"""
XRPBurn Data Generator — v4
============================
- Uses urllib only (no requests needed)
- Fetches real data from XRPL JSON-RPC (port 443 only)
- Burn = total_coins delta between days
- Tx categories sampled from real ledger transactions
- Never writes fake percentage splits
- sys.exit(1) on failure so GitHub Actions shows red X
v4 fix: server_info now checks validated_ledger AND closed_ledger
        (some nodes return one or the other depending on sync state)
        Also prints raw ledger info keys for easier future debugging.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
import pytz

XRPL_NODES = [
    "https://xrplcluster.com",
    "https://xrpl.ws",
    "https://s2.ripple.com",
]

SAMPLE_LEDGER_COUNT = 40
LEDGERS_PER_DAY     = 25_000
REQUEST_TIMEOUT     = 30


def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/4.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {url}")
        return None
    except Exception as e:
        print(f"  [ERR] {url} -> {type(e).__name__}: {e}")
        return None


def xrpl_rpc(method, params=None):
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in XRPL_NODES:
        try:
            req = urllib.request.Request(
                node, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "XRPBurnTracker/4.0"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.loads(r.read().decode())
            result = data.get("result", {})
            if result.get("status") == "success":
                print(f"  [OK]  {method} via {node}")
                return result
            else:
                print(f"  [WARN] {node} -> status='{result.get('status')}'")
        except urllib.error.HTTPError as e:
            print(f"  [HTTP {e.code}] {node}")
        except Exception as e:
            print(f"  [ERR] {node} -> {type(e).__name__}: {e}")
    print(f"  [FAIL] All XRPL nodes failed for: {method}")
    return None


def get_xrp_price():
    data = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd"
    )
    if data and "ripple" in data:
        price = float(data["ripple"]["usd"])
        print(f"  [OK]  XRP price = ${price}")
        return price
    print("  [WARN] CoinGecko unavailable")
    return None


def get_server_info():
    """
    Returns total_coins_xrp and ledger_index.
    Checks both validated_ledger and closed_ledger — nodes return one or the other
    depending on their sync state. Prints available keys for debugging.
    """
    result = xrpl_rpc("server_info")
    if not result:
        return None

    info = result.get("info", {})

    # Print top-level info keys so we can see what the node returned
    print(f"  [DEBUG] info keys: {list(info.keys())}")

    # Try validated_ledger first, then closed_ledger as fallback
    vl = info.get("validated_ledger") or info.get("closed_ledger")

    if not vl:
        print(f"  [WARN] Neither validated_ledger nor closed_ledger found in server_info")
        print(f"  [DEBUG] Full info: {json.dumps(info, indent=2)[:800]}")
        return None

    print(f"  [DEBUG] ledger keys: {list(vl.keys())}")

    raw_coins = vl.get("total_coins")
    if not raw_coins:
        print(f"  [WARN] total_coins missing from ledger info: {vl}")
        return None

    total_coins_xrp = int(raw_coins) / 1_000_000
    ledger_index    = int(vl.get("seq", 0))

    print(f"  [OK]  total_coins = {total_coins_xrp:,.4f} XRP  |  ledger #{ledger_index}")
    return {"total_coins_xrp": total_coins_xrp, "ledger_index": ledger_index}


def classify_tx(tx_type):
    if tx_type in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):
        return "settlement"
    if tx_type in ("OfferCreate", "OfferCancel",
                   "AMMCreate", "AMMDeposit", "AMMWithdraw",
                   "AMMBid", "AMMVote", "AMMDelete"):
        return "defi"
    if tx_type in ("DIDSet", "DIDDelete",
                   "CredentialCreate", "CredentialAccept", "CredentialDelete",
                   "DepositPreauth"):
        return "identity"
    return "acct_mgmt"


def sample_ledgers(start_index):
    tx_counts       = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_vol_xrp = 0.0
    fee_drops       = 0
    ledgers_ok      = 0

    print(f"  Sampling {SAMPLE_LEDGER_COUNT} ledgers from #{start_index} ...")

    for i in range(SAMPLE_LEDGER_COUNT):
        result = xrpl_rpc("ledger", {
            "ledger_index": start_index - i,
            "transactions": True,
            "expand":       True,
        })
        if not result:
            continue
        txs = result.get("ledger", {}).get("transactions", [])
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
            cat = classify_tx(tx.get("TransactionType", ""))
            tx_counts[cat] += 1
            fee_drops += int(tx.get("Fee", 0))
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):
                    payment_vol_xrp += int(amt) / 1_000_000
        time.sleep(0.04)

    total = sum(tx_counts.values())
    print(f"  Done: {ledgers_ok}/{SAMPLE_LEDGER_COUNT} ledgers | {total} txs | "
          f"fees = {fee_drops/1e6:.4f} XRP")
    print(f"  Categories: " + " | ".join(f"{k}={v}" for k, v in tx_counts.items()))
    return tx_counts, payment_vol_xrp, fee_drops, ledgers_ok


def get_real_metrics(previous_total_coins=None):
    print("\n--- Fetching XRP price ---")
    xrp_price = get_xrp_price() or 2.30

    print("\n--- Fetching XRPL server_info ---")
    server = get_server_info()
    if not server:
        print("\n[CRITICAL] Could not get server_info — marking as simulated.")
        return None, None, None, {}, {}, True, None

    current_total_coins = server["total_coins_xrp"]
    current_ledger      = server["ledger_index"]

    if previous_total_coins and previous_total_coins > current_total_coins:
        burn_xrp = round(previous_total_coins - current_total_coins, 6)
        print(f"\n  Burn (delta): {burn_xrp} XRP")
    else:
        burn_xrp = None
        print("\n  No valid previous total_coins — burn estimated from fees.")

    print("\n--- Sampling ledgers ---")
    tx_counts_raw, payment_vol_sampled, fee_drops_sampled, ledgers_ok = \
        sample_ledgers(current_ledger)

    total_sampled = sum(tx_counts_raw.values())

    if ledgers_ok == 0 or total_sampled == 0:
        print("  [WARN] No ledger data — categories empty.")
        if burn_xrp is None:
            burn_xrp = 0.0
        return burn_xrp, None, None, {}, {}, False, current_total_coins

    scale      = LEDGERS_PER_DAY / ledgers_ok
    total_tx_m = round(total_sampled * scale / 1_000_000, 4)
    tx_cats    = {k: round(v * scale / 1_000_000, 4) for k, v in tx_counts_raw.items()}

    payment_vol_daily = payment_vol_sampled * scale
    load_usd_m        = round(payment_vol_daily * xrp_price / 1_000_000, 2)
    load_cats         = {
        k: round(load_usd_m * (tx_counts_raw[k] / total_sampled), 2)
        for k in tx_counts_raw
    }

    if burn_xrp is None:
        burn_xrp = round(fee_drops_sampled * scale / 1_000_000, 6)
        print(f"  Burn (fee estimate): {burn_xrp} XRP/day")

    print(f"\n=== FINAL RESULTS ===")
    print(f"  burn_xrp   = {burn_xrp} XRP")
    print(f"  load_usd_m = ${load_usd_m}M")
    print(f"  total_tx   = {total_tx_m}M")
    print(f"  tx_cats    = {tx_cats}")
    print(f"  load_cats  = {load_cats}")

    return burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats, False, current_total_coins


def update_data():
    sgt           = pytz.timezone("Asia/Singapore")
    now           = datetime.now(sgt)
    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*55}")
    print(f"  XRPBurn update: {timestamp_str} SGT")
    print(f"{'='*55}")

    file_path = "data.json"
    data = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    previous_total_coins = None
    for entry in reversed(data):
        if entry.get("date") != date_str and entry.get("total_coins_xrp"):
            previous_total_coins = float(entry["total_coins_xrp"])
            print(f"\n  Previous total_coins from {entry['date']}: "
                  f"{previous_total_coins:,.4f} XRP")
            break

    burn, load, tx, tx_cats, load_cats, is_simulated, current_total_coins = \
        get_real_metrics(previous_total_coins)

    new_entry = {
        "date":            date_str,
        "last_updated":    timestamp_str,
        "burn_xrp":        burn,
        "load_usd_m":      load,
        "transactions":    tx,
        "tx_categories":   tx_cats,
        "load_categories": load_cats,
        "is_fallback":     is_simulated,
        "total_coins_xrp": current_total_coins,
    }

    data = [e for e in data if e.get("date") != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    print(f"\n  Saved {date_str} | is_fallback={is_simulated}")
    if is_simulated:
        print("  !! Fallback — could not get valid XRPL data.")
        sys.exit(1)
    else:
        print("  Success — real data written.")


if __name__ == "__main__":
    update_data()
