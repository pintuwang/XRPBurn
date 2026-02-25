"""
XRPBurn Data Generator — v9
============================
Key fix: open_coins_xrp is always fetched from the actual 00:00 SGT ledger
via binary search — NOT from "first run of the day."

This means:
  - Manual runs at any time produce the same burn figure
  - Multiple runs never distort the burn value
  - Burn = coins at 00:00 SGT − coins now (true calendar-day burn)

Option 1: open_coins_xrp stored in data.json but re-derived each run
          (stored only to avoid extra API call if already found today)
Option 3: is_partial / partial_as_of flags for chart transparency
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
import pytz

XRPL_NODES = [
    "https://xrplcluster.com",
    "https://xrpl.ws",
    "https://s2.ripple.com",
]

SAMPLE_LEDGER_COUNT = 40
REQUEST_TIMEOUT     = 30
RIPPLE_EPOCH        = 946684800   # Jan 1 2000 00:00 UTC in unix seconds
DAY_COMPLETE_HOUR   = 23
DAY_COMPLETE_MIN    = 30


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/9.0"})
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
                         "User-Agent": "XRPBurnTracker/9.0"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.loads(r.read().decode())
            result = data.get("result", {})
            if result.get("status") == "success":
                return result
        except Exception as e:
            pass
    return None


def parse_ledger_result(res):
    """Extract seq, total_coins_xrp, close_time_utc from a ledger RPC result."""
    if not res:
        return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    if not ld:
        return None
    seq       = int(ld.get("ledger_index") or ld.get("seqNum") or
                    res.get("ledger_index", 0))
    raw_coins = ld.get("total_coins")
    coins     = int(raw_coins) / 1_000_000 if raw_coins else None
    close_ts  = ld.get("close_time")
    close_utc = (datetime.fromtimestamp(close_ts + RIPPLE_EPOCH,
                                        tz=timezone.utc)
                 if close_ts is not None else None)
    return {"seq": seq, "coins": coins, "time_utc": close_utc}


# ---------------------------------------------------------------------------
# 1. Find the ledger at midnight SGT (binary search)
# ---------------------------------------------------------------------------

def find_midnight_ledger(current_seq, current_time_utc, midnight_utc):
    """
    Binary-search XRPL for the ledger whose close_time is closest to
    midnight SGT (= 16:00 UTC previous day).

    Returns total_coins_xrp at midnight, or None on failure.
    Each ledger closes ~3.5 seconds after the previous one.
    """
    print(f"  Target midnight UTC: {midnight_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    est_seq = current_seq + int(
        (midnight_utc - current_time_utc).total_seconds() / 3.5
    )

    for attempt in range(6):
        res  = xrpl_rpc("ledger", {"ledger_index": est_seq,
                                    "transactions": False})
        info = parse_ledger_result(res)
        if not info or not info["time_utc"]:
            print(f"  [WARN] Could not fetch ledger #{est_seq}")
            break
        delta_s    = (midnight_utc - info["time_utc"]).total_seconds()
        correction = int(delta_s / 3.5)
        sgt        = timezone(timedelta(hours=8))
        print(f"  Attempt {attempt+1}: #{info['seq']:,}  "
              f"{info['time_utc'].astimezone(sgt).strftime('%H:%M:%S SGT')}  "
              f"(Δ {delta_s/60:+.1f} min)")
        if abs(correction) < 5:
            if info["coins"]:
                print(f"  → Midnight coins: {info['coins']:,.4f} XRP")
                return info["coins"]
            break
        est_seq += correction

    print("  [WARN] Could not pinpoint midnight ledger.")
    return None


# ---------------------------------------------------------------------------
# 2. CoinGecko — price + 24h volume
# ---------------------------------------------------------------------------

def get_coingecko_data():
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
    simple = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd"
    )
    if simple and "ripple" in simple:
        price = float(simple["ripple"]["usd"])
        print(f"  [OK]  price=${price:.4f} (volume unavailable)")
        return price, None
    return None, None


# ---------------------------------------------------------------------------
# 3. Current validated ledger
# ---------------------------------------------------------------------------

def get_current_ledger():
    result = xrpl_rpc("ledger", {
        "ledger_index": "validated",
        "transactions": False,
        "expand":       False,
    })
    info = parse_ledger_result(result)
    if not info:
        return None
    if not info["coins"]:
        print(f"  [WARN] total_coins missing from validated ledger")
        return None
    print(f"  [OK]  seq=#{info['seq']:,} | coins={info['coins']:,.4f} XRP | "
          f"time={info['time_utc']}")
    return info


# ---------------------------------------------------------------------------
# 4. Category proportions from sampled ledgers
# ---------------------------------------------------------------------------

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


def get_category_proportions(current_ledger_seq):
    tx_counts  = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    fee_drops  = 0
    ledgers_ok = 0

    print(f"  Sampling {SAMPLE_LEDGER_COUNT} ledgers ...")
    for i in range(SAMPLE_LEDGER_COUNT):
        result = xrpl_rpc("ledger", {
            "ledger_index": current_ledger_seq - i,
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
    print(f"  {ledgers_ok} ledgers | {total} txs sampled")
    print(f"  " + " | ".join(f"{k}={v*100:.1f}%" for k, v in proportions.items()))
    return proportions, fee_drops, ledgers_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_real_metrics(midnight_utc, existing_midnight_coins=None):
    """
    midnight_utc: datetime of 00:00 SGT for today, in UTC
    existing_midnight_coins: cached value from a previous run today (skip re-fetch if present)
    """

    print("\n--- CoinGecko: price + 24h volume ---")
    xrp_price, volume_24h_usd = get_coingecko_data()
    if xrp_price is None:
        xrp_price = 2.30

    print("\n--- XRPL: current validated ledger ---")
    current = get_current_ledger()
    if not current:
        print("[CRITICAL] Cannot reach XRPL.")
        return None, None, None, {}, {}, True, None, None

    # ── Midnight coins (Option 1 — always from real midnight ledger) ──────
    print("\n--- XRPL: midnight SGT ledger ---")
    if existing_midnight_coins:
        # Already found and cached from an earlier run today — reuse it
        midnight_coins = existing_midnight_coins
        print(f"  Using cached midnight coins: {midnight_coins:,.4f} XRP")
    else:
        midnight_coins = find_midnight_ledger(
            current["seq"], current["time_utc"], midnight_utc
        )

    # ── Burn = midnight − now ─────────────────────────────────────────────
    if midnight_coins and midnight_coins > current["coins"]:
        burn_xrp = round(midnight_coins - current["coins"], 6)
        print(f"\n  Burn (midnight→now): {midnight_coins:.4f} − "
              f"{current['coins']:.4f} = {burn_xrp} XRP")
    else:
        burn_xrp = None
        print("\n  [WARN] Could not compute burn from midnight delta.")

    print("\n--- XRPL: category proportions ---")
    proportions, fee_drops, ledgers_ok = \
        get_category_proportions(current["seq"])

    # Burn fallback from fees
    if burn_xrp is None and ledgers_ok > 0:
        scale    = 25_000 / ledgers_ok
        burn_xrp = round(fee_drops * scale / 1_000_000, 6)
        print(f"  Burn (fee estimate): {burn_xrp} XRP")

    # ── Load ──────────────────────────────────────────────────────────────
    load_usd_m = round(volume_24h_usd / 1_000_000, 2) if volume_24h_usd else None

    # ── Tx count proxy ────────────────────────────────────────────────────
    total_tx_m = None
    if ledgers_ok > 0 and fee_drops > 0:
        avg_fee = fee_drops / max(ledgers_ok * 50, 1)
        if avg_fee > 0:
            scale      = 25_000 / ledgers_ok
            total_tx_m = round((fee_drops / avg_fee) * scale / 1_000_000, 4)

    # ── Category breakdowns ───────────────────────────────────────────────
    tx_cats = load_cats = {}
    if proportions:
        if total_tx_m:
            tx_cats = {k: round(proportions[k] * total_tx_m, 4) for k in proportions}
        if load_usd_m:
            load_cats = {k: round(proportions[k] * load_usd_m, 2) for k in proportions}

    print(f"\n=== RESULTS ===")
    print(f"  burn_xrp   = {burn_xrp} XRP")
    print(f"  load_usd_m = ${load_usd_m}M")
    print(f"  total_tx   = {total_tx_m}M")
    print(f"  tx_cats    = {tx_cats}")

    return (burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats,
            False, current["coins"], midnight_coins)


def update_data():
    sgt = pytz.timezone("Asia/Singapore")
    now = datetime.now(sgt)

    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_str      = now.strftime("%H:%M")

    # Midnight SGT today in UTC
    midnight_sgt = sgt.localize(
        datetime(now.year, now.month, now.day, 0, 0, 0)
    )
    midnight_utc = midnight_sgt.astimezone(timezone.utc)

    is_partial = not (now.hour > DAY_COMPLETE_HOUR or
                      (now.hour == DAY_COMPLETE_HOUR and
                       now.minute >= DAY_COMPLETE_MIN))

    print(f"\n{'='*57}")
    print(f"  XRPBurn update: {timestamp_str} SGT")
    print(f"  is_partial: {is_partial}  "
          f"(complete after {DAY_COMPLETE_HOUR}:{DAY_COMPLETE_MIN:02d} SGT)")
    print(f"{'='*57}")

    file_path = "data.json"
    data = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    # Check if we already found midnight_coins for today (skip re-search)
    existing_today         = next((e for e in data if e.get("date") == date_str), None)
    existing_midnight_coins = None
    if existing_today and existing_today.get("open_coins_xrp"):
        existing_midnight_coins = float(existing_today["open_coins_xrp"])
        print(f"\n  Cached midnight coins from earlier run: "
              f"{existing_midnight_coins:,.4f} XRP")

    (burn, load, tx, tx_cats, load_cats,
     is_simulated, current_coins, midnight_coins) = get_real_metrics(
        midnight_utc, existing_midnight_coins
    )

    new_entry = {
        "date":             date_str,
        "last_updated":     timestamp_str,
        # open_coins = coins at 00:00 SGT (from midnight ledger binary search)
        # Cached after first successful find — never changes for the same day
        "open_coins_xrp":  midnight_coins or (
            existing_midnight_coins if existing_midnight_coins else None
        ),
        "total_coins_xrp":  current_coins,
        "burn_xrp":         burn,
        "load_usd_m":       load,
        "transactions":     tx,
        "tx_categories":    tx_cats,
        "load_categories":  load_cats,
        "is_fallback":      is_simulated,
        "is_partial":       is_partial,
        "partial_as_of":    time_str if is_partial else None,
    }

    data = [e for e in data if e.get("date") != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    status = f"PARTIAL ({time_str} SGT)" if is_partial else "COMPLETE"
    print(f"\n  Saved {date_str} | {status} | is_fallback={is_simulated}")
    if is_simulated:
        print("  !! Fallback — XRPL unreachable.")
        sys.exit(1)
    else:
        print("  Success.")


if __name__ == "__main__":
    update_data()
