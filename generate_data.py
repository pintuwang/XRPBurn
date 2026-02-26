"""
XRPBurn Data Generator — v11 (clean rewrite)
=============================================
Burn: Always coin delta from midnight SGT ledger.
      Binary search → fallback to -25k ledgers → null (never fee estimate).
      open_coins_xrp cached in data.json; reused only if strictly > current_coins.
Load: CoinGecko 24h exchange volume (clean, exchange-reported).
Tx:   Sampled count × scale factor (proportions are the reliable part).
Partial: is_partial=True before 23:30 SGT, False after.
"""

import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import pytz

NODES           = ["https://xrplcluster.com", "https://xrpl.ws", "https://s2.ripple.com"]
SAMPLE_N        = 40
TIMEOUT         = 30
RIPPLE_EPOCH    = 946684800
COMPLETE_HOUR   = 23
COMPLETE_MIN    = 30


# ── Network ─────────────────────────────────────────────────────────────────

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurn/11"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [ERR] {url}: {e}")
        return None


def rpc(method, params=None):
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in NODES:
        try:
            req = urllib.request.Request(
                node, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "XRPBurn/11"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                result = json.loads(r.read().decode()).get("result", {})
            if result.get("status") == "success":
                return result
        except Exception:
            pass
    return None


def parse(res):
    if not res:
        return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    if not ld:
        return None
    coins = int(ld["total_coins"]) / 1e6 if ld.get("total_coins") else None
    ts    = ld.get("close_time")
    t     = datetime.fromtimestamp(ts + RIPPLE_EPOCH, tz=timezone.utc) if ts is not None else None
    seq   = int(ld.get("ledger_index") or ld.get("seqNum") or res.get("ledger_index", 0))
    return {"seq": seq, "coins": coins, "t": t}


# ── Step 1: current ledger ───────────────────────────────────────────────────

def get_current():
    info = parse(rpc("ledger", {"ledger_index": "validated", "transactions": False}))
    if not info or not info["coins"]:
        print("  [FAIL] Cannot get validated ledger")
        return None
    sgt = timezone(timedelta(hours=8))
    print(f"  seq=#{info['seq']:,} | coins={info['coins']:,.4f} XRP | "
          f"{info['t'].astimezone(sgt).strftime('%H:%M:%S SGT')}")
    return info


# ── Step 2: midnight coins (binary search) ───────────────────────────────────

def get_midnight_coins(cur_seq, cur_t, midnight_utc, cached=None):
    sgt = timezone(timedelta(hours=8))

    # Use cached value only if it's meaningfully larger than current coins
    # (max plausible daily burn ~5000 XRP; if delta is outside 0-5000 it's wrong)
    if cached is not None:
        info = parse(rpc("ledger", {"ledger_index": "validated", "transactions": False}))
        cur_now = info["coins"] if info else None
        if cur_now and 0 < (cached - cur_now) < 5000:
            print(f"  Using cached: {cached:,.4f} XRP (delta={cached-cur_now:.4f})")
            return cached, None, None
        else:
            print(f"  Cached {cached} invalid vs current {cur_now} — re-searching")

    # Binary search
    diff_s  = (midnight_utc - cur_t).total_seconds()
    est_seq = cur_seq + int(diff_s / 3.5)
    best    = None

    print(f"  Searching for {midnight_utc.astimezone(sgt).strftime('%H:%M SGT')} "
          f"from #{cur_seq:,} (est #{est_seq:,})")

    for attempt in range(7):
        info = parse(rpc("ledger", {"ledger_index": est_seq, "transactions": False}))
        if not info or not info["t"]:
            est_seq += 10
            continue
        delta_s = (midnight_utc - info["t"]).total_seconds()
        corr    = int(delta_s / 3.5)
        print(f"  #{info['seq']:,} @ {info['t'].astimezone(sgt).strftime('%H:%M:%S SGT')} "
              f"(Δ {delta_s/60:+.1f}m, corr {corr:+d})")
        if info["coins"]:
            # Keep the closest result found so far
            if best is None or abs(delta_s) < abs((midnight_utc - best["t"]).total_seconds()):
                best = info
        if abs(delta_s) < 30:
            break
        est_seq += corr

    if best and best["coins"]:
        print(f"  ✓ Midnight ledger #{best['seq']:,} | coins={best['coins']:,.4f} XRP")
        return best["coins"], best["seq"], best["t"]

    # Fallback: ~24h ago
    print("  Binary search failed — trying ledger 25,000 back (~24h)")
    fb = parse(rpc("ledger", {"ledger_index": cur_seq - 25_000, "transactions": False}))
    if fb and fb["coins"]:
        hrs = (cur_t - fb["t"]).total_seconds() / 3600 if fb["t"] else None
        print(f"  ✓ Fallback #{fb['seq']:,} | {fb['coins']:,.4f} XRP "
              f"({hrs:.1f}h ago)" if hrs else f"  ✓ Fallback: {fb['coins']:,.4f} XRP")
        return fb["coins"], fb["seq"], fb["t"]

    print("  [FAIL] Could not find midnight baseline — burn will be null")
    return None, None, None


# ── Step 3: CoinGecko ────────────────────────────────────────────────────────

def get_price_vol():
    d = fetch("https://api.coingecko.com/api/v3/coins/ripple"
              "?localization=false&tickers=false&market_data=true"
              "&community_data=false&developer_data=false")
    if d and "market_data" in d:
        md = d["market_data"]
        price = float(md["current_price"]["usd"])
        vol   = float(md["total_volume"]["usd"])
        print(f"  price=${price:.4f} | vol=${vol/1e6:.0f}M")
        return price, vol
    d = fetch("https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd")
    if d and "ripple" in d:
        price = float(d["ripple"]["usd"])
        print(f"  price=${price:.4f} (no volume)")
        return price, None
    return None, None


# ── Step 4: category sample ──────────────────────────────────────────────────

def classify(t):
    if t in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):   return "settlement"
    if t in ("OfferCreate", "OfferCancel", "AMMCreate", "AMMDeposit",
             "AMMWithdraw", "AMMBid", "AMMVote", "AMMDelete"):        return "defi"
    if t in ("DIDSet", "DIDDelete", "CredentialCreate",
             "CredentialAccept", "CredentialDelete", "DepositPreauth"): return "identity"
    return "acct_mgmt"


def get_categories(cur_seq, ledgers_per_day=17_500):
    counts = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    ok = 0
    print(f"  Sampling {SAMPLE_N} ledgers from #{cur_seq:,}")
    for i in range(SAMPLE_N):
        res = rpc("ledger", {"ledger_index": cur_seq - i,
                             "transactions": True, "expand": True})
        if not res:
            continue
        txs = res.get("ledger", {}).get("transactions", [])
        if not txs:
            continue
        ok += 1
        for tx in txs:
            if not isinstance(tx, dict):
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            if isinstance(meta, dict) and \
               meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
                continue
            counts[classify(tx.get("TransactionType", ""))] += 1
        time.sleep(0.04)

    total = sum(counts.values())
    if total == 0 or ok == 0:
        print("  [WARN] No tx data")
        return None, 0, 0

    props = {k: v / total for k, v in counts.items()}
    scale = 25_000 / ok

    # Cap avg tx/ledger at 130 to prevent burst-window inflation
    # XRPL reality: median ~80-110 tx/ledger, hard ceiling ~200 during spam
    # Uncapped: a 180 tx/ledger sample × 625 = 4.5M (wrong)
    # Capped:   min(180, 130) × 625 = 2.03M still high but bounded
    # Better: use capped avg × 25,000 directly
    avg_tx_per_ledger = total / ok
    REALISTIC_MAX_TX_PER_LEDGER = 120   # conservative ceiling
    capped_avg = min(avg_tx_per_ledger, REALISTIC_MAX_TX_PER_LEDGER)
    total_tx_m = round(capped_avg * ledgers_per_day / 1_000_000, 4)

    print(f"  {ok} ledgers | {total} txs | avg={avg_tx_per_ledger:.0f}/ledger "
          f"(capped to {capped_avg:.0f}) | est {total_tx_m:.3f}M/day")
    print("  " + " | ".join(f"{k}={v*100:.1f}%" for k, v in props.items()))
    return props, total_tx_m, ok


# ── Main ─────────────────────────────────────────────────────────────────────

def update_data():
    sgt           = pytz.timezone("Asia/Singapore")
    now           = datetime.now(sgt)
    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_str      = now.strftime("%H:%M")
    is_partial    = not (now.hour > COMPLETE_HOUR or
                         (now.hour == COMPLETE_HOUR and now.minute >= COMPLETE_MIN))

    midnight_sgt = sgt.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    midnight_utc = midnight_sgt.astimezone(timezone.utc)

    print(f"\n{'='*55}")
    print(f"  XRPBurn v11: {timestamp_str} SGT | "
          f"{'PARTIAL' if is_partial else 'COMPLETE'}")
    print(f"{'='*55}")

    # Load existing data
    file_path = "data.json"
    data = []
    if os.path.exists(file_path):
        with open(file_path) as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    existing = next((e for e in data if e.get("date") == date_str), None)
    cached_open = float(existing["open_coins_xrp"]) \
                  if existing and existing.get("open_coins_xrp") else None

    # Fetch all metrics
    print("\n--- Current ledger ---")
    current = get_current()
    if not current:
        print("[CRITICAL] XRPL unreachable")
        sys.exit(1)

    print("\n--- Midnight ledger ---")
    midnight_coins, midnight_seq, midnight_time = get_midnight_coins(
        current["seq"], current["t"], midnight_utc, cached_open
    )

    print("\n--- CoinGecko ---")
    xrp_price, vol_usd = get_price_vol()
    xrp_price = xrp_price or 2.30

    # Compute real ledgers/day from actual window (no assumption needed)
    from datetime import timezone as _tz
    now_utc = datetime.now(_tz.utc)
    if midnight_time and midnight_seq:
        elapsed_hours    = (now_utc - midnight_time).total_seconds() / 3600
        ledgers_in_window = current["seq"] - midnight_seq
        real_lpd = int(ledgers_in_window * 24 / elapsed_hours) if elapsed_hours > 0 else 17_500
        print(f"\n  Real ledgers/day = {ledgers_in_window:,} ledgers in "
              f"{elapsed_hours:.2f}h × 24 = {real_lpd:,}")
    else:
        real_lpd = 17_500   # fallback only if midnight_seq unavailable
        print(f"\n  Using fallback ledgers/day = {real_lpd:,}")

    print("\n--- Categories ---")
    props, total_tx_m, ledgers_ok = get_categories(current["seq"], real_lpd)

    # Burn — coin delta only, never fee estimate
    burn_xrp = None
    if midnight_coins and midnight_coins > current["coins"]:
        burn_xrp = round(midnight_coins - current["coins"], 6)
        print(f"\n  Burn: {midnight_coins:.4f} − {current['coins']:.4f} = {burn_xrp} XRP")
    else:
        print(f"\n  [WARN] No valid burn baseline (midnight={midnight_coins}, "
              f"current={current['coins']}) — storing null")

    load_usd_m = round(vol_usd / 1e6, 2) if vol_usd else None

    tx_cats = load_cats = {}
    if props and total_tx_m:
        tx_cats   = {k: round(props[k] * total_tx_m, 4) for k in props}
    if props and load_usd_m:
        load_cats = {k: round(props[k] * load_usd_m, 2) for k in props}

    print(f"\n=== SUMMARY ===")
    print(f"  burn={burn_xrp} XRP | load=${load_usd_m}M | tx={total_tx_m}M")

    entry = {
        "date":            date_str,
        "last_updated":    timestamp_str,
        "open_coins_xrp":  midnight_coins if midnight_coins else cached_open,
        "total_coins_xrp": current["coins"],
        "burn_xrp":        burn_xrp,
        "load_usd_m":      load_usd_m,
        "transactions":    total_tx_m,
        "tx_categories":   tx_cats,
        "load_categories": load_cats,
        "is_fallback":     False,
        "is_partial":      is_partial,
        "partial_as_of":   time_str if is_partial else None,
    }

    data = [e for e in data if e.get("date") != date_str]
    data.append(entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    print(f"\n  ✓ Saved {date_str} | {'PARTIAL '+time_str if is_partial else 'COMPLETE'}")


if __name__ == "__main__":
    update_data()
