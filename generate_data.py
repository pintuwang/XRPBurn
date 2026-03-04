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

# ── RLUSD Config ─────────────────────────────────────────────────────────────
# ✅ CONFIRMED via official Ripple docs (docs.ripple.com) and xrpscan.com
RLUSD_ISSUER_XRPL  = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY_HEX = "524C555344000000000000000000000000000000"  # hex for "RLUSD"
RLUSD_COINGECKO_ID = "ripple-usd"

# ── RWA Registry ─────────────────────────────────────────────────────────────
# Status key:
#   "confirmed"  = issuer address verified from on-chain/official sources
#   "discover"   = no confirmed address; script will auto-discover via XRPSCAN API
#   "skip"       = known to exist but no public issuer address available yet
#
# To look up an address manually:
#   1. Visit https://xrpscan.com and search the currency code (e.g. "TBILL")
#   2. Find the verified/labelled issuer account
#   3. Copy the address into "issuer" and change status to "confirmed"
#
# Based on rwa.xyz data (Mar 4 2026), top XRPL RWA platforms by value:
#   Ondo ~$160M | CRX Digital Assets ~$106M | Zeconomy ~$70M
#   Braza Crypto ~$67M | Archax ~$63M | OpenEden ~$62M | SG-Forge ~$12M
#
# price_usd: fixed USD price per token.
#   Use 1.0 for $1-pegged tokens (T-bills priced ~$1, MMFs ~$1, stablecoins)
#   Use None for float-priced tokens (script will try DEX price lookup)

RWA_REGISTRY = [
    # ── OpenEden TBILL (SG, bonds) ────────────────────────────────────────────
    # Launched Aug 2024 on XRPL. Currency is 4-char "TBILL" (or hex variant).
    # Ripple invested $10M. ~$62M on XRPL as of Mar 2026.
    {   "name":       "OpenEden TBILL",
        "currency":   "5442494C4C000000000000000000000000000000",  # hex "TBILL"
        "currency_alt": "TBILL",   # fallback short code
        "status":     "discover",  # auto-lookup issuer via XRPSCAN
        "asset_type": "bonds",
        "geography":  "SG",
        "price_usd":  1.0 },

    # ── Ondo OUSG (US, bonds) ────────────────────────────────────────────────
    # Launched Jun 2025 on XRPL. ~$160M as of Mar 2026.
    {   "name":       "Ondo OUSG",
        "currency":   "4F555347000000000000000000000000000000000",  # hex "OUSG"
        "currency_alt": "OUSG",
        "status":     "discover",
        "asset_type": "bonds",
        "geography":  "US",
        "price_usd":  1.0 },

    # ── Archax / abrdn Lux Fund (UK, fund) ───────────────────────────────────
    # First tokenized MMF on XRPL, launched Nov 2024. ~$63M as of Mar 2026.
    # Currency codes may be LUX, HHIF, or a hex variant — discover tries all.
    {   "name":       "Archax abrdn Lux",
        "currency":   "4C555800000000000000000000000000000000000",   # hex "LUX"
        "currency_alt": "LUX",
        "status":     "discover",
        "asset_type": "fund",
        "geography":  "UK",
        "price_usd":  1.0 },
    {   "name":       "Archax HHIF",
        "currency":   "HHIF",
        "currency_alt": "HHIF",
        "status":     "discover",
        "asset_type": "fund",
        "geography":  "UK",
        "price_usd":  1.0 },

    # ── SG-FORGE EURCV (France, stablecoin/bonds) ────────────────────────────
    # Launched on XRPL 2025 as 3rd chain after ETH and Solana. ~$12M Mar 2026.
    # EUR-pegged, so USD value = amount × EUR/USD rate (use 1.08 as proxy)
    {   "name":       "SG-Forge EURCV",
        "currency":   "455552435600000000000000000000000000000000", # hex "EURCV"
        "currency_alt": "EURCV",
        "status":     "discover",
        "asset_type": "bonds",
        "geography":  "FR",
        "price_usd":  1.08 },  # approximate EUR/USD — close enough for trend tracking

    # ── Braza BBRL (Brazil, stablecoin) ──────────────────────────────────────
    # BRL-pegged stablecoin on XRPL. ~$67M as of Mar 2026.
    # BRL ≈ 0.178 USD as of early 2026
    {   "name":       "Braza BBRL",
        "currency":   "4242524C000000000000000000000000000000000",  # hex "BBRL"
        "currency_alt": "BBRL",
        "status":     "discover",
        "asset_type": "bonds",
        "geography":  "BR",
        "price_usd":  0.178 },  # approximate BRL/USD

    # ── Zeconomy (Unknown) ───────────────────────────────────────────────────
    # ~$70M on rwa.xyz but no public info on currency code — skip for now.
    # When you know the currency code, add it here with status "discover".

    # ── CRX Digital Assets (Unknown) ────────────────────────────────────────
    # 7 assets totalling ~$106M. Currency codes not publicly known — skip.
]


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

    avg_tx_per_ledger = total / ok
    REALISTIC_MAX = 120   # cap: prevents burst-window inflation
    capped_avg = min(avg_tx_per_ledger, REALISTIC_MAX)

    # Full-day projection (used for complete days)
    projected_tx_m = round(capped_avg * ledgers_per_day / 1_000_000, 4)

    print(f"  {ok} ledgers | {total} txs | avg={avg_tx_per_ledger:.0f}/ledger "
          f"(capped={capped_avg:.0f}) | projected={projected_tx_m:.3f}M/day")
    print("  " + " | ".join(f"{k}={v*100:.1f}%" for k, v in props.items()))
    return props, projected_tx_m, capped_avg, ok


# ── Step 5: RWA data ─────────────────────────────────────────────────────────

def xrpscan_token_lookup(currency_code):
    """
    Query XRPSCAN public API to find the top verified issuer for a currency code.
    Returns issuer address string or None.
    XRPSCAN API: https://api.xrpscan.com/api/v1/token/{CURRENCY}
    """
    for code in [currency_code]:
        try:
            url = f"https://api.xrpscan.com/api/v1/token/{code}"
            req = urllib.request.Request(url, headers={"User-Agent": "XRPBurn/11"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
            # API may return a list of tokens with that currency code
            if isinstance(data, list) and data:
                # Prefer trust_level=3 (verified) or highest supply
                verified = [t for t in data if t.get("meta", {}).get("token", {}).get("trust_level", 0) >= 2]
                pool = verified if verified else data
                best = max(pool, key=lambda t: float(t.get("supply", 0) or 0))
                issuer = best.get("issuer")
                if issuer:
                    print(f"  [XRPSCAN] {code} → issuer={issuer} supply={best.get('supply')}")
                    return issuer
            elif isinstance(data, dict) and data.get("issuer"):
                issuer = data["issuer"]
                print(f"  [XRPSCAN] {code} → issuer={issuer}")
                return issuer
        except Exception as e:
            print(f"  [XRPSCAN] lookup failed for {code}: {e}")
    return None


def get_gateway_obligations(issuer):
    """Return all obligations for an issuer via gateway_balances RPC."""
    res = rpc("gateway_balances", {"account": issuer, "ledger_index": "validated"})
    if not res:
        return {}
    return {k: float(v) for k, v in res.get("obligations", {}).items()}


def get_rwa_data(xrp_price_usd):
    """
    For each token in RWA_REGISTRY:
      - If status="discover": auto-lookup issuer via XRPSCAN API
      - If status="confirmed": use issuer directly
      - Query gateway_balances for the issuer
      - Value = supply × price_usd
    Returns total_usd_m, by_type, by_issuer, by_geography dicts.
    """
    by_type    = {}
    by_issuer  = {}
    by_geo     = {}
    total_usd  = 0.0

    # Cache of already-discovered issuers (avoid duplicate RPC calls)
    issuer_cache = {}

    for token in RWA_REGISTRY:
        name     = token["name"]
        currency = token["currency"]
        alt_cur  = token.get("currency_alt", currency)
        status   = token.get("status", "discover")
        atype    = token["asset_type"]
        geo      = token["geography"]
        price_usd = token.get("price_usd", 1.0)

        # Resolve issuer
        issuer = token.get("issuer") if status == "confirmed" else None
        if not issuer:
            # Try cache first
            cache_key = alt_cur or currency
            if cache_key in issuer_cache:
                issuer = issuer_cache[cache_key]
            else:
                # Try short code first (more reliable with XRPSCAN), then hex
                issuer = xrpscan_token_lookup(alt_cur) or xrpscan_token_lookup(currency)
                issuer_cache[cache_key] = issuer

        if not issuer:
            print(f"  [SKIP] {name} — cannot resolve issuer")
            continue

        obligations = issuer_cache.get(f"obs_{issuer}")
        if obligations is None:
            obligations = get_gateway_obligations(issuer)
            issuer_cache[f"obs_{issuer}"] = obligations

        # Try hex currency first, then short code
        amount = obligations.get(currency, 0.0) or obligations.get(alt_cur, 0.0)
        if amount <= 0:
            print(f"  [ZERO] {name} ({alt_cur}) at {issuer} — no obligations found")
            continue

        usd_val = amount * (price_usd if price_usd else 1.0)
        usd_m   = usd_val / 1_000_000

        total_usd += usd_m
        by_type[atype]   = round(by_type.get(atype, 0)   + usd_m, 4)
        by_issuer[name]  = round(by_issuer.get(name, 0)  + usd_m, 4)
        by_geo[geo]      = round(by_geo.get(geo, 0)      + usd_m, 4)
        print(f"  {name}: {amount:,.2f} tokens @ ${price_usd} ≈ ${usd_m:.2f}M")

    total_usd = round(total_usd, 4)
    if total_usd == 0:
        print("  [WARN] No RWA data — all XRPSCAN lookups failed or returned zero supply")
        print("         Note: these are permissioned tokens with very few holders.")
        print("         Check xrpscan.com manually and add confirmed issuer addresses.")
        return None, {}, {}, {}

    print(f"  Total RWA tracked: ${total_usd:.2f}M")
    return total_usd, by_type, by_issuer, by_geo



# ── Step 6: RLUSD data ───────────────────────────────────────────────────────

def get_rlusd_data():
    """
    Fetch total RLUSD supply from CoinGecko and XRPL-side supply from gateway_balances.
    Returns (total_m, xrpl_m, eth_m) all in USD millions (RLUSD is $1 pegged).
    """
    # Total supply — CoinGecko
    total_m = None
    d = fetch(f"https://api.coingecko.com/api/v3/coins/{RLUSD_COINGECKO_ID}"
              f"?localization=false&tickers=false&market_data=true"
              f"&community_data=false&developer_data=false")
    if d and "market_data" in d:
        cs = d["market_data"].get("circulating_supply") or d["market_data"].get("total_supply")
        if cs:
            total_m = round(float(cs) / 1e6, 4)
            print(f"  RLUSD total supply: {cs:,.0f} ≈ ${total_m:.2f}M (CoinGecko)")

    # XRPL side — gateway_balances (confirmed issuer address)
    xrpl_m = None
    if RLUSD_ISSUER_XRPL:
        obs = get_gateway_obligations(RLUSD_ISSUER_XRPL)
        # RLUSD uses hex currency code on mainnet
        xrpl_raw = obs.get(RLUSD_CURRENCY_HEX) or obs.get("RLUSD", 0)
        if xrpl_raw:
            xrpl_m = round(float(xrpl_raw) / 1e6, 4)
            print(f"  RLUSD XRPL supply: {xrpl_raw:,.0f} ≈ ${xrpl_m:.2f}M")

    # Ethereum = total - XRPL (residual)
    eth_m = None
    if total_m is not None and xrpl_m is not None:
        eth_m = round(max(total_m - xrpl_m, 0), 4)

    if total_m is None:
        print("  [WARN] CoinGecko returned no RLUSD data")

    return total_m, xrpl_m, eth_m






# ── Step 7: RWA.xyz scrape (Distributed + Represented) ───────────────────────

def get_rwa_xyz_data():
    """
    Fetch Distributed and Represented Asset Values for XRPL from rwa.xyz.
    rwa.xyz is mostly client-side rendered, so we extract values from
    whatever server-side HTML is present, with multiple fallback strategies.
    Returns (distributed_m, represented_m) in USD millions, or (None, None).
    """
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

        # Strategy 1: label immediately followed by value (server-rendered)
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

        # Strategy 2: find all dollar amounts, infer from order/size
        # rwa.xyz always shows Distributed first (smaller), Represented second (larger)
        all_amounts = re.findall(r'\$([\d,]+\.?\d*)\s*([BM])', html)
        parsed = []
        for val_str, unit in all_amounts:
            try:
                val = float(val_str.replace(',', ''))
                mult = 1000 if unit.upper() == 'B' else 1
                parsed.append(round(val * mult, 2))
            except Exception:
                continue
        plausible = [v for v in parsed if 50 <= v <= 10000]
        if len(plausible) >= 2:
            dist_m = min(plausible[:6])
            repr_m = max(plausible[:6])
            if repr_m > dist_m:
                print(f"  rwa.xyz fallback: Distributed=${dist_m}M | Represented=${repr_m}M")
                return dist_m, repr_m

        print("  [WARN] rwa.xyz: page is fully client-side rendered — no values extractable")
        return None, None

    except Exception as e:
        print(f"  [WARN] rwa.xyz fetch failed: {e}")
        return None, None


# ── Step 8: Amendment voting ──────────────────────────────────────────────────

# Amendments we track — name must match the XRPL feature name exactly.
# Status reference (Jan–Mar 2026):
#   PermissionedDomains  → ENABLED Feb 4 2026 (91% validator support)
#   SingleAssetVault     → In voting (XLS-65, required for LendingProtocol)
#   LendingProtocol      → In voting since Jan 28 2026 (XLS-66d)
TRACKED_AMENDMENTS = [
    {
        "name":    "LendingProtocol",
        "xls":     "XLS-66",
        "label":   "Native Lending (XLS-66)",
        "threshold": 80,         # % needed to activate
        "countdown_weeks": 2,    # weeks of supermajority needed after 80%
    },
    {
        "name":    "SingleAssetVault",
        "xls":     "XLS-65",
        "label":   "Single Asset Vault (XLS-65)",
        "threshold": 80,
        "countdown_weeks": 2,
    },
    {
        "name":    "PermissionedDomains",
        "xls":     "XLS-80",
        "label":   "Permissioned Domains (XLS-80)",
        "threshold": 80,
        "countdown_weeks": 2,
    },
]

def get_amendments():
    """
    Call the XRPL `feature` RPC to get live amendment voting status.
    Returns list of dicts with name, xls, label, vote_pct, status, enabled.
    """
    res = rpc("feature", {})
    if not res:
        print("  [WARN] Could not fetch amendment data")
        return []

    features = res.get("features", {})
    results  = []

    for amend in TRACKED_AMENDMENTS:
        name = amend["name"]
        # features dict is keyed by amendment hash; find by name field
        match = next(
            (v for v in features.values() if v.get("name") == name),
            None
        )
        if not match:
            print(f"  [WARN] Amendment '{name}' not found in feature list")
            results.append({
                "name":      name,
                "xls":       amend["xls"],
                "label":     amend["label"],
                "vote_pct":  None,
                "enabled":   False,
                "status":    "unknown",
                "vetoed":    False,
            })
            continue

        count      = match.get("count", 0)
        threshold  = match.get("threshold", 0)     # validators needed for 80%
        total_val  = match.get("validations", 0)   # total dUNL validators (~34)
        enabled    = match.get("enabled", False)
        vetoed     = match.get("vetoed", False)

        # vote_pct = validators supporting / total validators
        denominator = total_val if total_val > 0 else max(threshold, 1)
        vote_pct    = round(count / denominator * 100, 1) if denominator else None

        if enabled:
            status = "enabled"
        elif vetoed:
            status = "vetoed"
        elif vote_pct is not None and vote_pct >= amend["threshold"]:
            status = "supermajority"   # ≥80% — 2-week countdown running
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
    props, projected_tx_m, capped_avg_per_ledger, ledgers_ok = get_categories(current["seq"], real_lpd)

    # TX COUNT:
    # Partial day  → actual count since midnight (grows through the day, honest)
    # Complete day → full-day projection (best estimate at 23:30+)
    if is_partial and midnight_seq:
        ledgers_since_midnight = current["seq"] - midnight_seq
        actual_tx_m = round(capped_avg_per_ledger * ledgers_since_midnight / 1_000_000, 4)
        total_tx_m  = actual_tx_m
        print(f"  PARTIAL: {ledgers_since_midnight:,} ledgers since midnight × "
              f"{capped_avg_per_ledger:.0f} avg = {actual_tx_m:.3f}M actual so far")
    else:
        total_tx_m = projected_tx_m
        print(f"  COMPLETE: using projected {projected_tx_m:.3f}M/day")

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

    print("\n--- RWA on XRPL ---")
    rwa_total_m, rwa_by_type, rwa_by_issuer, rwa_by_geo = get_rwa_data(xrp_price or 2.30)

    print("\n--- RWA.xyz (Distributed vs Represented) ---")
    rwa_distributed_m, rwa_represented_m = get_rwa_xyz_data()

    print("\n--- RLUSD supply ---")
    rlusd_total_m, rlusd_xrpl_m, rlusd_eth_m = get_rlusd_data()

    # RLUSD daily minted = today total minus previous day total
    rlusd_minted_m = None
    prev = next((e for e in reversed(data) if e.get("date") != date_str
                 and e.get("rlusd_total_m") is not None), None)
    if prev and rlusd_total_m is not None:
        rlusd_minted_m = round(rlusd_total_m - prev["rlusd_total_m"], 4)
        print(f"  RLUSD minted today: {rlusd_minted_m:+.4f}M "
              f"(prev={prev['rlusd_total_m']:.2f}M → now={rlusd_total_m:.2f}M)")

    print("\n--- Amendment voting ---")
    amendments = get_amendments()

    # Burn threshold alert (1000 XRP/day)
    BURN_ALERT_THRESHOLD = 1000
    burn_alert = burn_xrp is not None and burn_xrp >= BURN_ALERT_THRESHOLD

    print(f"  burn={burn_xrp} XRP {'ALERT' if burn_alert else ''} | load=${load_usd_m}M | tx={total_tx_m}M")
    print(f"  rwa_dist=${rwa_distributed_m}M | rwa_repr=${rwa_represented_m}M")
    print(f"  rlusd_total=${rlusd_total_m}M (xrpl={rlusd_xrpl_m}, eth={rlusd_eth_m})")

    entry = {
        "date":              date_str,
        "last_updated":      timestamp_str,
        "open_coins_xrp":    midnight_coins if midnight_coins else cached_open,
        "total_coins_xrp":   current["coins"],
        "burn_xrp":          burn_xrp,
        "burn_alert":        burn_alert,
        "load_usd_m":        load_usd_m,
        "transactions":      total_tx_m,
        "projected_tx_m":    projected_tx_m,
        "tx_categories":     tx_cats,
        "load_categories":   load_cats,
        # ── RWA (gateway_balances) ────────────────────────────────────────────
        "rwa_total_usd_m":   rwa_total_m,
        "rwa_by_type":       rwa_by_type,
        "rwa_by_issuer":     rwa_by_issuer,
        "rwa_by_geography":  rwa_by_geo,
        # ── RWA (rwa.xyz Distributed vs Represented) ──────────────────────────
        "rwa_distributed_m": rwa_distributed_m,
        "rwa_represented_m": rwa_represented_m,
        # ── RLUSD ─────────────────────────────────────────────────────────────
        "rlusd_total_m":     rlusd_total_m,
        "rlusd_xrpl_m":      rlusd_xrpl_m,
        "rlusd_eth_m":       rlusd_eth_m,
        "rlusd_minted_m":    rlusd_minted_m,
        # ── Amendments ────────────────────────────────────────────────────────
        "amendments":        amendments,
        # ── Flags ─────────────────────────────────────────────────────────────
        "is_fallback":       False,
        "is_partial":        is_partial,
        "partial_as_of":     time_str if is_partial else None,
    }

    data = [e for e in data if e.get("date") != date_str]
    data.append(entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    print(f"\n  ✓ Saved {date_str} | {'PARTIAL '+time_str if is_partial else 'COMPLETE'}")


if __name__ == "__main__":
    update_data()
