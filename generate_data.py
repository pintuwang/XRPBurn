"""
XRPBurn Data Generator — v8
============================
Option 1: Opening snapshot protection
  - open_coins_xrp written ONCE at first run of the day, never overwritten
  - Burn = open_coins_xrp − current_coins (true midnight-to-now delta)
  - Safe to run manually mid-day without corrupting the day's burn figure

Option 3: Partial day awareness
  - is_partial = True whenever run before 23:30 SGT
  - partial_as_of = "HH:MM" SGT timestamp
  - 23:30+ SGT run sets is_partial = False (day considered complete)
  - index.html shows semi-transparent bars for partial days
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
REQUEST_TIMEOUT     = 30
DAY_COMPLETE_HOUR   = 23   # SGT hour after which day is considered complete
DAY_COMPLETE_MIN    = 30   # SGT minute


def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/8.0"})
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
                         "User-Agent": "XRPBurnTracker/8.0"}
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
    print(f"  [FAIL] All nodes failed for: {method}")
    return None


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def get_coingecko_data():
    """Price + rolling 24h exchange volume."""
    data = fetch_json(
        "https://api.coingecko.com/api/v3/coins/ripple"
        "?localization=false&tickers=false&market_data=true"
        "&community_data=false&developer_data=false"
    )
    if data and "market_data" in data:
        md    = data["market_data"]
        price = float(md["current_price"]["usd"])
        vol   = float(md["total_volume"]["usd"])
        print(f"  [OK]  price=${price:.4f} | 24h vol=${vol/1e6:.0f}M")
        return price, vol
    # Fallback — price only
    simple = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd"
    )
    if simple and "ripple" in simple:
        price = float(simple["ripple"]["usd"])
        print(f"  [OK]  price=${price:.4f} (volume unavailable)")
        return price, None
    return None, None


def get_ledger_info():
    """Validated ledger: total_coins + seq. Uses ledger RPC (server_info omits total_coins)."""
    result = xrpl_rpc("ledger", {
        "ledger_index": "validated",
        "transactions": False,
        "expand":       False,
    })
    if not result:
        return None
    ledger = result.get("ledger") or result.get("closed", {}).get("ledger", {})
    if not ledger:
        print(f"  [WARN] No ledger object. Keys: {list(result.keys())}")
        return None
    raw_coins = ledger.get("total_coins")
    if not raw_coins:
        print(f"  [WARN] total_coins missing. Keys: {list(ledger.keys())}")
        return None
    total_coins_xrp = int(raw_coins) / 1_000_000
    ledger_index    = int(
        ledger.get("ledger_index") or ledger.get("seqNum") or
        result.get("ledger_index", 0)
    )
    print(f"  [OK]  total_coins={total_coins_xrp:,.4f} XRP | ledger #{ledger_index}")
    return {"total_coins_xrp": total_coins_xrp, "ledger_index": ledger_index}


def classify_tx(tx_type):
    if tx_type in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):
        return "settlement"
    if tx_type in ("OfferCreate", "OfferCancel", "AMMCreate", "AMMDeposit",
                   "AMMWithdraw", "AMMBid", "AMMVote", "AMMDelete"):
        return "defi"
    if tx_type in ("DIDSet", "DIDDelete", "CredentialCreate",
                   "CredentialAccept", "CredentialDelete", "DepositPreauth"):
        return "identity"
    return "acct_mgmt"


def get_category_proportions(current_ledger):
    """
    Sample 40 ledgers for tx type breakdown.
    Returns proportions (ratios) — reliable even from small sample.
    Also returns fee_drops for burn fallback.
    """
    tx_counts  = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    fee_drops  = 0
    ledgers_ok = 0

    print(f"  Sampling {SAMPLE_LEDGER_COUNT} ledgers from #{current_ledger} ...")

    for i in range(SAMPLE_LEDGER_COUNT):
        result = xrpl_rpc("ledger", {
            "ledger_index": current_ledger - i,
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
        time.sleep(0.04)

    total = sum(tx_counts.values())
    if total == 0 or ledgers_ok == 0:
        print("  [WARN] No tx data sampled.")
        return None, fee_drops, ledgers_ok

    proportions = {k: v / total for k, v in tx_counts.items()}
    print(f"  {ledgers_ok} ledgers | {total} txs")
    print(f"  Proportions: " +
          " | ".join(f"{k}={v*100:.1f}%" for k, v in proportions.items()))
    return proportions, fee_drops, ledgers_ok


# ---------------------------------------------------------------------------
# Main metric builder
# ---------------------------------------------------------------------------

def get_real_metrics(open_coins_xrp=None):
    """
    open_coins_xrp: total_coins at 00:00 SGT today (preserved from first run).
    If None this is the first run of the day — current coins become the opening snapshot.
    """
    print("\n--- CoinGecko: price + 24h volume ---")
    xrp_price, volume_24h_usd = get_coingecko_data()
    if xrp_price is None:
        xrp_price = 2.30
        print(f"  Using fallback price ${xrp_price}")

    print("\n--- XRPL: validated ledger ---")
    ledger_info = get_ledger_info()
    if not ledger_info:
        print("\n[CRITICAL] Cannot reach XRPL — marking as simulated.")
        return None, None, None, {}, {}, True, None, None

    current_total_coins = ledger_info["total_coins_xrp"]
    current_ledger      = ledger_info["ledger_index"]

    # ── Burn calculation ──────────────────────────────────────────────────
    # open_coins_xrp is the snapshot from 00:00 SGT (first run of the day).
    # We NEVER overwrite it — so mid-day manual runs don't corrupt it.
    if open_coins_xrp and open_coins_xrp > current_total_coins:
        burn_xrp = round(open_coins_xrp - current_total_coins, 6)
        print(f"\n  Burn (open→now): {open_coins_xrp:.4f} − "
              f"{current_total_coins:.4f} = {burn_xrp} XRP")
    else:
        burn_xrp = None
        print("\n  First run of day — burn will be estimated from fees.")

    print("\n--- XRPL: category proportions ---")
    proportions, fee_drops_sampled, ledgers_ok = \
        get_category_proportions(current_ledger)

    # Burn fallback from fees if no open snapshot yet
    if burn_xrp is None:
        if ledgers_ok > 0:
            scale    = 25_000 / ledgers_ok
            burn_xrp = round(fee_drops_sampled * scale / 1_000_000, 6)
            print(f"  Burn (fee estimate): {burn_xrp} XRP")
        else:
            burn_xrp = 0.0

    # ── Load from CoinGecko 24h volume ────────────────────────────────────
    load_usd_m = round(volume_24h_usd / 1_000_000, 2) if volume_24h_usd else None
    print(f"\n  Load (CoinGecko 24h): ${load_usd_m:,.0f}M" if load_usd_m else
          "\n  [WARN] No volume data")

    # ── Tx count from fee-based proxy ────────────────────────────────────
    total_tx_m = None
    if ledgers_ok > 0 and fee_drops_sampled > 0:
        avg_fee = fee_drops_sampled / max(
            sum(1 for _ in range(ledgers_ok)) * 50, 1
        )
        if avg_fee > 0:
            scale      = 25_000 / ledgers_ok
            total_tx_m = round(
                (fee_drops_sampled / avg_fee) * scale / 1_000_000, 4
            )

    # ── Category breakdowns ───────────────────────────────────────────────
    tx_cats   = {}
    load_cats = {}
    if proportions and total_tx_m:
        tx_cats = {k: round(proportions[k] * total_tx_m, 4) for k in proportions}
    if proportions and load_usd_m:
        load_cats = {k: round(proportions[k] * load_usd_m, 2) for k in proportions}

    print(f"\n=== RESULTS ===")
    print(f"  burn_xrp   = {burn_xrp} XRP")
    print(f"  load_usd_m = ${load_usd_m}M")
    print(f"  total_tx   = {total_tx_m}M")
    print(f"  tx_cats    = {tx_cats}")

    return (burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats,
            False, current_total_coins, proportions)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def update_data():
    sgt           = pytz.timezone("Asia/Singapore")
    now           = datetime.now(sgt)
    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_str      = now.strftime("%H:%M")

    # Day is "complete" after 23:30 SGT
    is_partial = not (now.hour > DAY_COMPLETE_HOUR or
                      (now.hour == DAY_COMPLETE_HOUR and
                       now.minute >= DAY_COMPLETE_MIN))

    print(f"\n{'='*55}")
    print(f"  XRPBurn update: {timestamp_str} SGT")
    print(f"  is_partial: {is_partial} (complete after {DAY_COMPLETE_HOUR}:{DAY_COMPLETE_MIN:02d} SGT)")
    print(f"{'='*55}")

    file_path = "data.json"
    data = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    # Find existing entry for today (if any)
    existing_today = next((e for e in data if e.get("date") == date_str), None)

    # ── OPTION 1: Preserve opening snapshot ──────────────────────────────
    # open_coins_xrp is only written on the FIRST run of each day.
    # All subsequent runs (including manual) read it back and keep it intact.
    # This means burn always measures from true day-open, not last-run.
    open_coins_xrp = None
    if existing_today and existing_today.get("open_coins_xrp"):
        open_coins_xrp = float(existing_today["open_coins_xrp"])
        print(f"\n  Preserved open_coins from first run: {open_coins_xrp:,.4f} XRP")
    else:
        print(f"\n  First run of the day — will set open_coins_xrp from current state.")

    burn, load, tx, tx_cats, load_cats, is_simulated, current_total_coins, proportions = \
        get_real_metrics(open_coins_xrp)

    # ── Build new entry ───────────────────────────────────────────────────
    new_entry = {
        "date":             date_str,
        "last_updated":     timestamp_str,

        # OPTION 1: open_coins set once (first run), then frozen
        "open_coins_xrp":  open_coins_xrp if open_coins_xrp else current_total_coins,

        # Current coins stored for reference / next day's open comparison
        "total_coins_xrp":  current_total_coins,

        "burn_xrp":         burn,
        "load_usd_m":       load,
        "transactions":     tx,
        "tx_categories":    tx_cats,
        "load_categories":  load_cats,
        "is_fallback":      is_simulated,

        # OPTION 3: partial day flags
        "is_partial":       is_partial,
        "partial_as_of":    time_str if is_partial else None,
    }

    data = [e for e in data if e.get("date") != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    status = "PARTIAL (" + time_str + " SGT)" if is_partial else "COMPLETE"
    print(f"\n  Saved {date_str} | {status} | is_fallback={is_simulated}")
    if is_simulated:
        print("  !! Fallback — XRPL unreachable.")
        sys.exit(1)
    else:
        print("  Success.")


if __name__ == "__main__":
    update_data()
