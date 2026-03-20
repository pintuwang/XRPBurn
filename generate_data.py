"""
XRPBurn Data Generator — v12
=============================
Burn logic (simplified & reliable):
  - FIRST run of the day (no open_coins_xrp stored yet):
      → snapshot current total_coins as today's midnight baseline
      → store open_coins_xrp + open_seq_xrpl in data.json, LOCKED for the day
  - ALL subsequent runs:
      → burn = open_coins_xrp − current total_coins
      → no binary search, no fallback drift, no re-anchoring
  - Emergency fallback (past BASELINE_GRACE_HOURS, still no baseline):
      → binary search once for true midnight ledger
      → marks is_baseline_search=True so you know it happened

Load:  CoinGecko 24h exchange volume (exchange-reported).
Tx:    Sampled count × scale factor; proportions are the reliable part.
RWA:   XRPSCAN gateway_balances + rwa.xyz scrape.
RLUSD: CoinGecko supply + XRPL gateway_balances.
Amendments: XRPL `feature` RPC.
Partial: is_partial=True before 23:30 SGT, False after.
"""

import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
import pytz

NODES                = ["https://xrplcluster.com", "https://xrpl.ws", "https://s2.ripple.com"]
SAMPLE_N             = 40
TIMEOUT              = 30
RIPPLE_EPOCH         = 946684800
COMPLETE_HOUR        = 23
COMPLETE_MIN         = 30
BASELINE_GRACE_HOURS = 1   # within this many hours of midnight → snapshot directly

# ── RLUSD Config ──────────────────────────────────────────────────────────────
# ✅ CONFIRMED via official Ripple docs (docs.ripple.com) and xrpscan.com
RLUSD_ISSUER_XRPL  = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY_HEX = "524C555344000000000000000000000000000000"  # hex for "RLUSD"
RLUSD_COINGECKO_ID = "ripple-usd"

# ── RWA Registry ──────────────────────────────────────────────────────────────
RWA_REGISTRY = [
    {   "name":         "OpenEden TBILL",
        "currency":     "5442494C4C000000000000000000000000000000",
        "currency_alt": "TBILL",
        "status":       "discover",
        "asset_type":   "bonds",
        "geography":    "SG",
        "price_usd":    1.0 },
    {   "name":         "Ondo OUSG",
        "currency":     "4F555347000000000000000000000000000000000",
        "currency_alt": "OUSG",
        "status":       "discover",
        "asset_type":   "bonds",
        "geography":    "US",
        "price_usd":    1.0 },
    {   "name":         "Archax abrdn Lux",
        "currency":     "4C555800000000000000000000000000000000000",
        "currency_alt": "LUX",
        "status":       "discover",
        "asset_type":   "fund",
        "geography":    "UK",
        "price_usd":    1.0 },
    {   "name":         "Archax HHIF",
        "currency":     "HHIF",
        "currency_alt": "HHIF",
        "status":       "discover",
        "asset_type":   "fund",
        "geography":    "UK",
        "price_usd":    1.0 },
    {   "name":         "SG-Forge EURCV",
        "currency":     "455552435600000000000000000000000000000000",
        "currency_alt": "EURCV",
        "status":       "discover",
        "asset_type":   "bonds",
        "geography":    "FR",
        "price_usd":    1.08 },
    {   "name":         "Braza BBRL",
        "currency":     "4242524C000000000000000000000000000000000",
        "currency_alt": "BBRL",
        "status":       "discover",
        "asset_type":   "bonds",
        "geography":    "BR",
        "price_usd":    0.178 },
]

# ── Amendment tracking ────────────────────────────────────────────────────────
TRACKED_AMENDMENTS = [
    {
        "name":            "LendingProtocol",
        "xls":             "XLS-66",
        "label":           "Native Lending (XLS-66)",
        "threshold":       80,
        "countdown_weeks": 2,
    },
    {
        "name":            "SingleAssetVault",
        "xls":             "XLS-65",
        "label":           "Single Asset Vault (XLS-65)",
        "threshold":       80,
        "countdown_weeks": 2,
    },
    {
        "name":            "PermissionedDomains",
        "xls":             "XLS-80",
        "label":           "Permissioned Domains (XLS-80)",
        "threshold":       80,
        "countdown_weeks": 2,
    },
]


# ── Network ───────────────────────────────────────────────────────────────────

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurn/12"})
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
                headers={"Content-Type": "application/json", "User-Agent": "XRPBurn/12"})
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


# ── Step 1: current ledger ────────────────────────────────────────────────────

def get_current():
    info = parse(rpc("ledger", {"ledger_index": "validated", "transactions": False}))
    if not info or not info["coins"]:
        print("  [FAIL] Cannot get validated ledger")
        return None
    sgt = timezone(timedelta(hours=8))
    print(f"  seq=#{info['seq']:,} | coins={info['coins']:,.4f} XRP | "
          f"{info['t'].astimezone(sgt).strftime('%H:%M:%S SGT')}")
    return info


# ── Step 2: midnight baseline (snapshot-first, search only as emergency) ──────

def get_midnight_baseline(current, midnight_utc, hours_elapsed,
                          cached_coins, cached_seq):
    """
    Returns: (open_coins, open_seq, is_baseline_search)

    Priority:
      1. Already stored today → return as-is (locked, never overwrite)
      2. First run within grace window → snapshot current ledger directly
      3. Past grace window, no baseline → binary search once (emergency)
      4. All else fails → return (None, None, False)
    """
    sgt = timezone(timedelta(hours=8))

    # ── Case 1: baseline already locked in from a previous run today ──────────
    if cached_coins is not None and cached_seq is not None:
        burn_so_far = cached_coins - current["coins"]
        print(f"  ✓ Using stored baseline: {cached_coins:,.4f} XRP "
              f"(seq #{cached_seq:,}) | burn so far = {burn_so_far:.4f} XRP")
        return cached_coins, cached_seq, False

    # ── Case 2: first run of the day, within grace window of midnight ─────────
    if hours_elapsed <= BASELINE_GRACE_HOURS:
        print(f"  ✓ First run ({hours_elapsed:.2f}h since midnight) — "
              f"snapshotting current ledger as baseline")
        print(f"    open_coins = {current['coins']:,.4f} XRP | seq #{current['seq']:,}")
        return current["coins"], current["seq"], False

    # ── Case 3: past grace window, no baseline — emergency binary search ──────
    print(f"  ⚠ No baseline and {hours_elapsed:.1f}h elapsed — "
          f"running emergency binary search for midnight ledger")

    diff_s  = (midnight_utc - current["t"]).total_seconds()
    est_seq = current["seq"] + int(diff_s / 3.5)
    best    = None

    for attempt in range(8):
        info = parse(rpc("ledger", {"ledger_index": est_seq, "transactions": False}))
        if not info or not info["t"]:
            est_seq += 10
            continue
        delta_s = (midnight_utc - info["t"]).total_seconds()
        corr    = int(delta_s / 3.5)
        print(f"  #{info['seq']:,} @ {info['t'].astimezone(sgt).strftime('%H:%M:%S SGT')} "
              f"(Δ {delta_s/60:+.1f}m)")
        if info["coins"]:
            if best is None or abs(delta_s) < abs((midnight_utc - best["t"]).total_seconds()):
                best = info
        if abs(delta_s) < 30:
            break
        est_seq += corr

    if best and best["coins"]:
        print(f"  ✓ Emergency search found #{best['seq']:,} | "
              f"coins={best['coins']:,.4f} XRP")
        return best["coins"], best["seq"], True

    print("  [FAIL] Emergency search failed — burn will be null today")
    return None, None, False


# ── Step 3: CoinGecko ─────────────────────────────────────────────────────────

def get_price_vol():
    d = fetch("https://api.coingecko.com/api/v3/coins/ripple"
              "?localization=false&tickers=false&market_data=true"
              "&community_data=false&developer_data=false")
    if d and "market_data" in d:
        md    = d["market_data"]
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


# ── Step 4: transaction category sample ───────────────────────────────────────

def classify(t):
    if t in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):
        return "settlement"
    if t in ("OfferCreate", "OfferCancel", "AMMCreate", "AMMDeposit",
             "AMMWithdraw", "AMMBid", "AMMVote", "AMMDelete"):
        return "defi"
    if t in ("DIDSet", "DIDDelete", "CredentialCreate",
             "CredentialAccept", "CredentialDelete", "DepositPreauth"):
        return "identity"
    return "acct_mgmt"


def get_categories(cur_seq, ledgers_per_day=17_500):
    counts = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    ok     = 0
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
        return None, 0, 0, 0

    props             = {k: v / total for k, v in counts.items()}
    avg_tx_per_ledger = total / ok
    REALISTIC_MAX     = 120
    capped_avg        = min(avg_tx_per_ledger, REALISTIC_MAX)
    projected_tx_m    = round(capped_avg * ledgers_per_day / 1_000_000, 4)

    print(f"  {ok} ledgers | {total} txs | avg={avg_tx_per_ledger:.0f}/ledger "
          f"(capped={capped_avg:.0f}) | projected={projected_tx_m:.3f}M/day")
    print("  " + " | ".join(f"{k}={v*100:.1f}%" for k, v in props.items()))
    return props, projected_tx_m, capped_avg, ok


# ── Step 5: RWA data ──────────────────────────────────────────────────────────

def xrpscan_token_lookup(currency_code):
    try:
        url = f"https://api.xrpscan.com/api/v1/token/{currency_code}"
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurn/12"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list) and data:
            verified = [t for t in data
                        if t.get("meta", {}).get("token", {}).get("trust_level", 0) >= 2]
            pool = verified if verified else data
            best = max(pool, key=lambda t: float(t.get("supply", 0) or 0))
            issuer = best.get("issuer")
            if issuer:
                print(f"  [XRPSCAN] {currency_code} → issuer={issuer} "
                      f"supply={best.get('supply')}")
                return issuer
        elif isinstance(data, dict) and data.get("issuer"):
            issuer = data["issuer"]
            print(f"  [XRPSCAN] {currency_code} → issuer={issuer}")
            return issuer
    except Exception as e:
        print(f"  [XRPSCAN] lookup failed for {currency_code}: {e}")
    return None


def get_gateway_obligations(issuer):
    res = rpc("gateway_balances", {"account": issuer, "ledger_index": "validated"})
    if not res:
        return {}
    return {k: float(v) for k, v in res.get("obligations", {}).items()}


def get_rwa_data(xrp_price_usd):
    by_type      = {}
    by_issuer    = {}
    by_geo       = {}
    total_usd    = 0.0
    issuer_cache = {}

    for token in RWA_REGISTRY:
        name      = token["name"]
        currency  = token["currency"]
        alt_cur   = token.get("currency_alt", currency)
        status    = token.get("status", "discover")
        atype     = token["asset_type"]
        geo       = token["geography"]
        price_usd = token.get("price_usd", 1.0)

        issuer = token.get("issuer") if status == "confirmed" else None
        if not issuer:
            cache_key = alt_cur or currency
            if cache_key in issuer_cache:
                issuer = issuer_cache[cache_key]
            else:
                issuer = xrpscan_token_lookup(alt_cur) or xrpscan_token_lookup(currency)
                issuer_cache[cache_key] = issuer

        if not issuer:
            print(f"  [SKIP] {name} — cannot resolve issuer")
            continue

        obligations = issuer_cache.get(f"obs_{issuer}")
        if obligations is None:
            obligations = get_gateway_obligations(issuer)
            issuer_cache[f"obs_{issuer}"] = obligations

        amount = obligations.get(currency, 0.0) or obligations.get(alt_cur, 0.0)
        if amount <= 0:
            print(f"  [ZERO] {name} ({alt_cur}) at {issuer} — no obligations found")
            continue

        usd_m           = amount * (price_usd if price_usd else 1.0) / 1_000_000
        total_usd      += usd_m
        by_type[atype]  = round(by_type.get(atype, 0)  + usd_m, 4)
        by_issuer[name] = round(by_issuer.get(name, 0) + usd_m, 4)
        by_geo[geo]     = round(by_geo.get(geo, 0)     + usd_m, 4)
        print(f"  {name}: {amount:,.2f} tokens @ ${price_usd} ≈ ${usd_m:.2f}M")

    total_usd = round(total_usd, 4)
    if total_usd == 0:
        print("  [WARN] No RWA data — all lookups failed or returned zero supply")
        return None, {}, {}, {}

    print(f"  Total RWA tracked: ${total_usd:.2f}M")
    return total_usd, by_type, by_issuer, by_geo


# ── Step 6: RLUSD data ────────────────────────────────────────────────────────

def get_rlusd_data():
    total_m = None
    d = fetch(f"https://api.coingecko.com/api/v3/coins/{RLUSD_COINGECKO_ID}"
              f"?localization=false&tickers=false&market_data=true"
              f"&community_data=false&developer_data=false")
    if d and "market_data" in d:
        cs = (d["market_data"].get("circulating_supply") or
              d["market_data"].get("total_supply"))
        if cs:
            total_m = round(float(cs) / 1e6, 4)
            print(f"  RLUSD total supply: {cs:,.0f} ≈ ${total_m:.2f}M (CoinGecko)")

    xrpl_m = None
    if RLUSD_ISSUER_XRPL:
        obs      = get_gateway_obligations(RLUSD_ISSUER_XRPL)
        xrpl_raw = obs.get(RLUSD_CURRENCY_HEX) or obs.get("RLUSD", 0)
        if xrpl_raw:
            xrpl_m = round(float(xrpl_raw) / 1e6, 4)
            print(f"  RLUSD XRPL supply: {xrpl_raw:,.0f} ≈ ${xrpl_m:.2f}M")

    eth_m = None
    if total_m is not None and xrpl_m is not None:
        eth_m = round(max(total_m - xrpl_m, 0), 4)

    if total_m is None:
        print("  [WARN] CoinGecko returned no RLUSD data")

    return total_m, xrpl_m, eth_m


# ── Step 7: RWA.xyz scrape ────────────────────────────────────────────────────

def get_rwa_xyz_data():
    import re
    try:
        url = "https://app.rwa.xyz/networks/xrp-ledger"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="ignore")

        dist_m = repr_m = None

        for pattern, key in [
            (r'Distributed Asset Value.{0,300}?\$([\d,]+\.?\d*)\s*([BM])', 'dist'),
            (r'Represented Asset Value.{0,300}?\$([\d,]+\.?\d*)\s*([BM])', 'repr'),
        ]:
            m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if m:
                val  = float(m.group(1).replace(',', ''))
                mult = 1000 if m.group(2).upper() == 'B' else 1
                if key == 'dist': dist_m = round(val * mult, 2)
                else:             repr_m = round(val * mult, 2)

        if dist_m or repr_m:
            print(f"  rwa.xyz: Distributed=${dist_m}M | Represented=${repr_m}M")
            return dist_m, repr_m

        all_amounts = re.findall(r'\$([\d,]+\.?\d*)\s*([BM])', html)
        parsed = []
        for val_str, unit in all_amounts:
            try:
                val  = float(val_str.replace(',', ''))
                mult = 1000 if unit.upper() == 'B' else 1
                parsed.append(round(val * mult, 2))
            except Exception:
                continue
        plausible = [v for v in parsed if 50 <= v <= 10000]
        if len(plausible) >= 2:
            dist_m = min(plausible[:6])
            repr_m = max(plausible[:6])
            if repr_m > dist_m:
                print(f"  rwa.xyz fallback: Distributed=${dist_m}M | "
                      f"Represented=${repr_m}M")
                return dist_m, repr_m

        print("  [WARN] rwa.xyz: page fully client-side rendered — no values extractable")
        return None, None

    except Exception as e:
        print(f"  [WARN] rwa.xyz fetch failed: {e}")
        return None, None


# ── Step 8: Amendment voting ──────────────────────────────────────────────────

def get_amendments():
    res = rpc("feature", {})
    if not res:
        print("  [WARN] Could not fetch amendment data")
        return []

    features = res.get("features", {})
    results  = []

    for amend in TRACKED_AMENDMENTS:
        name  = amend["name"]
        match = next(
            (v for v in features.values() if v.get("name") == name),
            None
        )
        if not match:
            print(f"  [WARN] Amendment '{name}' not found in feature list")
            results.append({
                "name":     name,
                "xls":      amend["xls"],
                "label":    amend["label"],
                "vote_pct": None,
                "enabled":  False,
                "status":   "unknown",
                "vetoed":   False,
            })
            continue

        count     = match.get("count", 0)
        threshold = match.get("threshold", 0)
        total_val = match.get("validations", 0)
        enabled   = match.get("enabled", False)
        vetoed    = match.get("vetoed", False)

        denominator = total_val if total_val > 0 else max(threshold, 1)
        vote_pct    = round(count / denominator * 100, 1) if denominator else None

        if enabled:
            status = "enabled"
        elif vetoed:
            status = "vetoed"
        elif vote_pct is not None and vote_pct >= amend["threshold"]:
            status = "supermajority"
        elif vote_pct is not None and vote_pct > 0:
            status = "voting"
        else:
            status = "pending"

        results.append({
            "name":     name,
            "xls":      amend["xls"],
            "label":    amend["label"],
            "vote_pct": vote_pct,
            "enabled":  enabled,
            "status":   status,
            "vetoed":   vetoed,
        })
        print(f"  {amend['label']}: {vote_pct}%  [{status}]")

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def update_data():
    sgt           = pytz.timezone("Asia/Singapore")
    now           = datetime.now(sgt)
    date_str      = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
    time_str      = now.strftime("%H:%M")
    is_partial    = not (now.hour > COMPLETE_HOUR or
                         (now.hour == COMPLETE_HOUR and now.minute >= COMPLETE_MIN))

    midnight_sgt  = sgt.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    midnight_utc  = midnight_sgt.astimezone(timezone.utc)
    hours_elapsed = (datetime.now(timezone.utc) - midnight_utc).total_seconds() / 3600

    print(f"\n{'='*55}")
    print(f"  XRPBurn v12: {timestamp_str} SGT | "
          f"{'PARTIAL' if is_partial else 'COMPLETE'} | "
          f"{hours_elapsed:.2f}h since midnight")
    print(f"{'='*55}")

    # ── Load existing data ────────────────────────────────────────────────────
    file_path = "data.json"
    data      = []
    if os.path.exists(file_path):
        with open(file_path) as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    existing     = next((e for e in data if e.get("date") == date_str), None)
    cached_coins = float(existing["open_coins_xrp"]) \
                   if existing and existing.get("open_coins_xrp") else None
    cached_seq   = int(existing["open_seq_xrpl"]) \
                   if existing and existing.get("open_seq_xrpl") else None

    # ── Current ledger ────────────────────────────────────────────────────────
    print("\n--- Current ledger ---")
    current = get_current()
    if not current:
        print("[CRITICAL] XRPL unreachable")
        sys.exit(1)

    # ── Midnight baseline: snapshot / locked / emergency search ───────────────
    print("\n--- Midnight baseline ---")
    open_coins, open_seq, is_baseline_search = get_midnight_baseline(
        current, midnight_utc, hours_elapsed, cached_coins, cached_seq
    )

    # ── Ledgers/day from actual measured window ───────────────────────────────
    if open_seq:
        ledgers_in_window = current["seq"] - open_seq
        real_lpd = (int(ledgers_in_window * 24 / hours_elapsed)
                    if hours_elapsed > 0 else 17_500)
        print(f"\n  Ledgers since midnight: {ledgers_in_window:,} over "
              f"{hours_elapsed:.2f}h → {real_lpd:,}/day extrapolated")
    else:
        ledgers_in_window = None
        real_lpd          = 17_500
        print(f"\n  No open_seq — using fallback {real_lpd:,} ledgers/day")

    # ── Transaction categories ────────────────────────────────────────────────
    print("\n--- Categories ---")
    props, projected_tx_m, capped_avg, ledgers_ok = get_categories(
        current["seq"], real_lpd
    )

    # Tx count: actual since midnight (partial) or full-day projection (complete)
    if is_partial and ledgers_in_window is not None and capped_avg:
        total_tx_m = round(capped_avg * ledgers_in_window / 1_000_000, 4)
        print(f"  PARTIAL: {ledgers_in_window:,} ledgers × {capped_avg:.0f} avg "
              f"= {total_tx_m:.3f}M actual so far")
    else:
        total_tx_m = projected_tx_m
        print(f"  COMPLETE: using projected {projected_tx_m:.3f}M/day")

    # ── CoinGecko ─────────────────────────────────────────────────────────────
    print("\n--- CoinGecko ---")
    xrp_price, vol_usd = get_price_vol()
    xrp_price = xrp_price or 2.30

    # ── Burn: simple subtraction from locked baseline ─────────────────────────
    burn_xrp = None
    if open_coins and open_coins > current["coins"]:
        burn_xrp = round(open_coins - current["coins"], 6)
        print(f"\n  Burn: {open_coins:.4f} − {current['coins']:.4f} = {burn_xrp} XRP")
    else:
        print(f"\n  [WARN] No valid baseline — burn stored as null "
              f"(open={open_coins}, current={current['coins']})")

    # ── Load & category splits ────────────────────────────────────────────────
    load_usd_m = round(vol_usd / 1e6, 2) if vol_usd else None
    tx_cats    = ({k: round(props[k] * total_tx_m, 4) for k in props}
                  if props and total_tx_m else {})
    load_cats  = ({k: round(props[k] * load_usd_m, 2) for k in props}
                  if props and load_usd_m else {})

    # ── RWA ───────────────────────────────────────────────────────────────────
    print("\n--- RWA on XRPL ---")
    rwa_total_m, rwa_by_type, rwa_by_issuer, rwa_by_geo = get_rwa_data(xrp_price)

    print("\n--- RWA.xyz (Distributed vs Represented) ---")
    rwa_distributed_m, rwa_represented_m = get_rwa_xyz_data()

    # ── RLUSD ─────────────────────────────────────────────────────────────────
    print("\n--- RLUSD supply ---")
    rlusd_total_m, rlusd_xrpl_m, rlusd_eth_m = get_rlusd_data()

    rlusd_minted_m = None
    prev = next((e for e in reversed(data)
                 if e.get("date") != date_str
                 and e.get("rlusd_total_m") is not None), None)
    if prev and rlusd_total_m is not None:
        rlusd_minted_m = round(rlusd_total_m - prev["rlusd_total_m"], 4)
        print(f"  RLUSD minted today: {rlusd_minted_m:+.4f}M "
              f"(prev={prev['rlusd_total_m']:.2f}M → now={rlusd_total_m:.2f}M)")

    # ── Amendments ────────────────────────────────────────────────────────────
    print("\n--- Amendment voting ---")
    amendments = get_amendments()

    # ── Burn threshold alert ──────────────────────────────────────────────────
    BURN_ALERT_THRESHOLD = 1000
    burn_alert = burn_xrp is not None and burn_xrp >= BURN_ALERT_THRESHOLD

    print(f"\n=== SUMMARY ===")
    print(f"  burn={burn_xrp} XRP {'⚠ ALERT' if burn_alert else ''} | "
          f"load=${load_usd_m}M | tx={total_tx_m}M")
    print(f"  baseline={'SEARCH (emergency)' if is_baseline_search else 'SNAPSHOT/LOCKED'}")
    print(f"  rwa_dist=${rwa_distributed_m}M | rwa_repr=${rwa_represented_m}M")
    print(f"  rlusd_total=${rlusd_total_m}M (xrpl={rlusd_xrpl_m}, eth={rlusd_eth_m})")

    # ── Build entry ───────────────────────────────────────────────────────────
    entry = {
        "date":               date_str,
        "last_updated":       timestamp_str,
        # Baseline — locked on first run, never overwritten mid-day
        "open_coins_xrp":     open_coins,
        "open_seq_xrpl":      open_seq,
        "total_coins_xrp":    current["coins"],
        # Burn
        "burn_xrp":           burn_xrp,
        "burn_alert":         burn_alert,
        # Load
        "load_usd_m":         load_usd_m,
        # Transactions
        "transactions":       total_tx_m,
        "projected_tx_m":     projected_tx_m,
        "tx_categories":      tx_cats,
        "load_categories":    load_cats,
        # RWA
        "rwa_total_usd_m":    rwa_total_m,
        "rwa_by_type":        rwa_by_type,
        "rwa_by_issuer":      rwa_by_issuer,
        "rwa_by_geography":   rwa_by_geo,
        "rwa_distributed_m":  rwa_distributed_m,
        "rwa_represented_m":  rwa_represented_m,
        # RLUSD
        "rlusd_total_m":      rlusd_total_m,
        "rlusd_xrpl_m":       rlusd_xrpl_m,
        "rlusd_eth_m":        rlusd_eth_m,
        "rlusd_minted_m":     rlusd_minted_m,
        # Amendments
        "amendments":         amendments,
        # Flags
        "is_fallback":        False,
        "is_baseline_search": is_baseline_search,
        "is_partial":         is_partial,
        "partial_as_of":      time_str if is_partial else None,
    }

    # Safety net: never lose a previously locked baseline due to a failed run
    if existing:
        if existing.get("open_coins_xrp") and entry["open_coins_xrp"] is None:
            entry["open_coins_xrp"] = existing["open_coins_xrp"]
        if existing.get("open_seq_xrpl") and entry["open_seq_xrpl"] is None:
            entry["open_seq_xrpl"] = existing["open_seq_xrpl"]

    data = [e for e in data if e.get("date") != date_str]
    data.append(entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    print(f"\n  ✓ Saved {date_str} | "
          f"{'PARTIAL ' + time_str if is_partial else 'COMPLETE'}")


if __name__ == "__main__":
    update_data()
