"""
XRPBurn Data Generator — v10
==============================
Core fix: burn is ALWAYS from coin delta, never from fee estimate.
Fee estimate is removed entirely — it undercounts by 30-40x because
it misses AccountDelete reserve burns (2 XRP each).

Burn sources in priority order:
  1. coins_at_midnight  − coins_now       (binary search for 00:00 SGT ledger)
  2. coins_at_25k_back  − coins_now       (fallback: ~24h ago, if search fails)
  3. null                                 (shown as empty bar, never faked)

open_coins_xrp cached in data.json after first successful find.
Manual re-runs reuse cached value — burn never changes within same day.

Option 3: is_partial / partial_as_of for chart transparency.
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
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/10.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {url}")
        return None
    except Exception as e:
        print(f"  [ERR] {type(e).__name__}: {e}")
        return None


def xrpl_rpc(method, params=None):
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in XRPL_NODES:
        try:
            req = urllib.request.Request(
                node, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "XRPBurnTracker/10.0"}
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.loads(r.read().decode())
            result = data.get("result", {})
            if result.get("status") == "success":
                return result
        except Exception:
            pass
    return None


def get_ledger(index):
    """Fetch a single ledger header (no tx expansion). index = int or 'validated'."""
    return xrpl_rpc("ledger", {
        "ledger_index": index,
        "transactions": False,
        "expand":       False,
    })


def parse_ledger(res):
    """
    Extract seq, total_coins_xrp, close_time_utc from ledger RPC result.
    Returns dict or None.
    """
    if not res:
        return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    if not ld:
        return None
    raw_coins = ld.get("total_coins")
    close_ts  = ld.get("close_time")
    seq       = int(
        ld.get("ledger_index") or ld.get("seqNum") or
        res.get("ledger_index", 0)
    )
    coins = int(raw_coins) / 1_000_000 if raw_coins else None
    close_utc = (
        datetime.fromtimestamp(close_ts + RIPPLE_EPOCH, tz=timezone.utc)
        if close_ts is not None else None
    )
    return {"seq": seq, "coins": coins, "time_utc": close_utc}


# ---------------------------------------------------------------------------
# 1. Current validated ledger
# ---------------------------------------------------------------------------

def get_current_ledger():
    res  = get_ledger("validated")
    info = parse_ledger(res)
    if not info:
        print("  [FAIL] Could not fetch validated ledger.")
        return None
    if not info["coins"]:
        print(f"  [FAIL] total_coins missing. Check node response.")
        return None
    sgt = timezone(timedelta(hours=8))
    print(f"  seq={info['seq']:,} | coins={info['coins']:,.4f} XRP | "
          f"time={info['time_utc'].astimezone(sgt).strftime('%H:%M:%S SGT')}")
    return info


# ---------------------------------------------------------------------------
# 2. Find midnight SGT ledger via binary search
# ---------------------------------------------------------------------------

def find_midnight_coins(current_seq, current_time_utc, midnight_utc):
    """
    Binary search for the ledger closest to midnight SGT (= UTC-8h).
    Returns total_coins_xrp at that ledger, or None on failure.

    Fallback: if search fails, go back exactly 25,000 ledgers (~24h).
    """
    sgt = timezone(timedelta(hours=8))

    # ── Binary search ─────────────────────────────────────────────────────
    diff_s  = (midnight_utc - current_time_utc).total_seconds()
    est_seq = current_seq + int(diff_s / 3.5)

    print(f"  Current:  #{current_seq:,} @ "
          f"{current_time_utc.astimezone(sgt).strftime('%H:%M:%S SGT')}")
    print(f"  Target:   {midnight_utc.astimezone(sgt).strftime('%H:%M:%S SGT')} "
          f"(Δ {diff_s/3600:.2f}h → est #{est_seq:,})")

    last_good = None
    for attempt in range(7):
        res  = get_ledger(est_seq)
        info = parse_ledger(res)

        if not info or not info["time_utc"]:
            print(f"  Attempt {attempt+1}: #{est_seq:,} → fetch failed")
            # Try adjacent ledger
            est_seq += 10
            continue

        delta_s    = (midnight_utc - info["time_utc"]).total_seconds()
        correction = int(delta_s / 3.5)

        print(f"  Attempt {attempt+1}: #{info['seq']:,} @ "
              f"{info['time_utc'].astimezone(sgt).strftime('%H:%M:%S SGT')} "
              f"(Δ {delta_s/60:+.1f} min | correction {correction:+d})")

        if info["coins"]:
            last_good = info

        if abs(delta_s) < 30:   # within 30 seconds of midnight — good enough
            if info["coins"]:
                print(f"  ✓ Found midnight ledger: {info['coins']:,.4f} XRP")
                return info["coins"]
            break

        if abs(correction) < 3:
            # Close enough but coins missing — use last good
            break

        est_seq += correction

    if last_good and last_good["coins"]:
        print(f"  ✓ Best approximation: #{last_good['seq']:,} | "
              f"{last_good['coins']:,.4f} XRP")
        return last_good["coins"]

    # ── Fallback: go back 25,000 ledgers (≈24h) ───────────────────────────
    print(f"\n  [WARN] Binary search failed — falling back to ~24h ago ledger.")
    fallback_seq = current_seq - 25_000
    res  = get_ledger(fallback_seq)
    info = parse_ledger(res)
    if info and info["coins"]:
        actual_offset_h = None
        if info["time_utc"] and current_time_utc:
            actual_offset_h = (current_time_utc - info["time_utc"]).total_seconds() / 3600
        print(f"  Fallback ledger #{info['seq']:,}: {info['coins']:,.4f} XRP "
              f"({actual_offset_h:.1f}h ago)" if actual_offset_h else
              f"  Fallback ledger #{info['seq']:,}: {info['coins']:,.4f} XRP")
        return info["coins"]

    print("  [FAIL] All burn baselines failed — burn will be null.")
    return None


# ---------------------------------------------------------------------------
# 3. CoinGecko — price + 24h volume
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
        print(f"  price=${price:.4f} | 24h vol=${vol/1e6:.0f}M USD")
        return price, vol
    simple = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd"
    )
    if simple and "ripple" in simple:
        price = float(simple["ripple"]["usd"])
        print(f"  price=${price:.4f} (volume unavailable)")
        return price, None
    return None, None


# ---------------------------------------------------------------------------
# 4. Category proportions
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
    ledgers_ok = 0

    print(f"  Sampling {SAMPLE_LEDGER_COUNT} ledgers from #{current_ledger_seq:,} ...")
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
            tx_counts[classify_tx(tx.get("TransactionType", ""))] += 1
        time.sleep(0.04)

    total = sum(tx_counts.values())
    if total == 0 or ledgers_ok == 0:
        print("  [WARN] No tx data sampled.")
        return None, ledgers_ok

    proportions = {k: v / total for k, v in tx_counts.items()}
    proportions["_total"] = total   # raw count for tx scaling (popped by caller)
    print(f"  {ledgers_ok} ledgers | {total} txs | " +
          " | ".join(f"{k}={v*100:.1f}%" for k, v in tx_counts.items()
                     if k != "_total") + 
          f"  (proportions: " + 
          " | ".join(f"{k}={v*100:.1f}%" for k, v in proportions.items()
                     if k != "_total") + ")")
    return proportions, ledgers_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_real_metrics(midnight_utc, cached_midnight_coins=None):
    print("\n--- CoinGecko ---")
    xrp_price, volume_24h_usd = get_coingecko_data()
    xrp_price = xrp_price or 2.30

    print("\n--- Current ledger ---")
    current = get_current_ledger()
    if not current:
        print("[CRITICAL] Cannot reach XRPL.")
        return None, None, None, {}, {}, True, None, None

    # ── Midnight coins ─────────────────────────────────────────────────────
    print("\n--- Midnight SGT ledger ---")
    if cached_midnight_coins:
        midnight_coins = cached_midnight_coins
        print(f"  Using cached: {midnight_coins:,.4f} XRP")
    else:
        midnight_coins = find_midnight_coins(
            current["seq"], current["time_utc"], midnight_utc
        )

    # ── Burn = midnight − now (ONLY from coin delta, never from fees) ──────
    if midnight_coins and midnight_coins > current["coins"]:
        burn_xrp = round(midnight_coins - current["coins"], 6)
        print(f"\n  Burn: {midnight_coins:.4f} − {current['coins']:.4f} "
              f"= {burn_xrp:.6f} XRP")
    else:
        burn_xrp = None
        print("\n  [WARN] Could not compute burn — will store null.")

    print("\n--- Category proportions ---")
    proportions, ledgers_ok = get_category_proportions(current["seq"])

    # ── Load ──────────────────────────────────────────────────────────────
    load_usd_m = round(volume_24h_usd / 1_000_000, 2) if volume_24h_usd else None

    # ── Tx count from actual sample ──────────────────────────────────────
    # proportions tuple now also returns raw total from sample
    # sampled_total × (25000 / ledgers_ok) = daily estimate
    total_tx_m = None
    if ledgers_ok > 0 and proportions:
        # sampled_total is stored in proportions["_total"] by get_category_proportions
        _sampled_total = proportions.pop("_total", None)
        if _sampled_total:
            _scale     = 25_000 / ledgers_ok
            total_tx_m = round(_sampled_total * _scale / 1_000_000, 4)
            print(f"  Tx count: {_sampled_total} sampled × {_scale:.0f} scale "
                  f"= {total_tx_m:.3f}M/day")

    # ── Category breakdowns ────────────────────────────────────────────────
    tx_cats = load_cats = {}
    if proportions:
        if total_tx_m:
            tx_cats   = {k: round(proportions[k] * total_tx_m, 4)
                         for k in proportions}
        if load_usd_m:
            load_cats = {k: round(proportions[k] * load_usd_m, 2)
                         for k in proportions}

    print(f"\n=== RESULTS ===")
    print(f"  burn_xrp   = {burn_xrp} XRP")
    print(f"  load_usd_m = ${load_usd_m}M")
    print(f"  total_tx   = {total_tx_m}M (estimate)")
    print(f"  tx_cats    = {tx_cats}")

    return (burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats,
            False, current["coins"], midnight_coins)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def update_data():
    sgt = pytz.timezone("Asia/Singapore")
    now = datetime.now(sgt)

    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_str      = now.strftime("%H:%M")

    midnight_sgt = sgt.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    midnight_utc = midnight_sgt.astimezone(timezone.utc)

    is_partial = not (now.hour > DAY_COMPLETE_HOUR or
                      (now.hour == DAY_COMPLETE_HOUR and
                       now.minute >= DAY_COMPLETE_MIN))

    print(f"\n{'='*57}")
    print(f"  XRPBurn update: {timestamp_str} SGT")
    print(f"  {'PARTIAL DAY' if is_partial else 'COMPLETE DAY'} "
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

    # Reuse cached midnight_coins only if valid (must be > current coins)
    existing_today        = next((e for e in data if e.get("date") == date_str), None)
    cached_midnight_coins = None
    if existing_today and existing_today.get("open_coins_xrp"):
        candidate = float(existing_today["open_coins_xrp"])
        # Validate: fetch current coins quickly to check cache is plausible
        _vres  = get_ledger("validated")
        _vinfo = parse_ledger(_vres)
        _vcur  = _vinfo["coins"] if _vinfo else None
        if _vcur and candidate > _vcur and (candidate - _vcur) < 5000:
            # Difference must be > 0 and < 5000 XRP (daily burn is ~400-600 XRP)
            cached_midnight_coins = candidate
            print(f"\n  Cached midnight coins: {candidate:,.4f} XRP ✓ "
                  f"(delta = {candidate - _vcur:.4f} XRP so far today)")
        else:
            print(f"\n  Cached open_coins={candidate:,.4f} failed validation "
                  f"(current={_vcur}) — re-searching midnight ledger.")

    (burn, load, tx, tx_cats, load_cats,
     is_simulated, current_coins, midnight_coins) = get_real_metrics(
        midnight_utc, cached_midnight_coins
    )

    # open_coins_xrp = midnight ledger coins (cached once, never overwritten)
    open_coins = (
        cached_midnight_coins or midnight_coins
    )

    new_entry = {
        "date":            date_str,
        "last_updated":    timestamp_str,
        "open_coins_xrp":  open_coins,
        "total_coins_xrp": current_coins,
        "burn_xrp":        burn,
        "load_usd_m":      load,
        "transactions":    tx,
        "tx_categories":   tx_cats,
        "load_categories": load_cats,
        "is_fallback":     is_simulated,
        "is_partial":      is_partial,
        "partial_as_of":   time_str if is_partial else None,
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
