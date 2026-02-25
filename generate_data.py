"""
XRPBurn Data Generator — Fixed Version
=======================================
Changes from original:
  - Removed broken XRPScan statistics endpoints (field names were wrong → always simulated)
  - Uses XRPL JSON-RPC directly (xrplcluster.com) for all on-chain data
  - Real burn = daily delta of total_coins from server_info (stored in data.json)
  - Real tx categories = sampled from last N ledgers, classified by TransactionType
  - Real load USD = sampled payment volume × live XRP price
  - Fake categories no longer written when data is unavailable (empty {} triggers blue bar)
  - Multi-node fallback for XRPL connectivity
"""

import json
import os
import time
import urllib.request
from datetime import datetime
import pytz

# ---------------------------------------------------------------------------
# XRPL node pool — tried in order until one responds
# ---------------------------------------------------------------------------
XRPL_NODES = [
    "https://xrplcluster.com",
    "https://s1.ripple.com:51234",
    "https://s2.ripple.com:51234",
]

# How many recent ledgers to sample for tx classification.
# XRPL closes ~25,000 ledgers/day (~3-4s each). Sampling 60 ledgers
# covers ~3-4 minutes of real traffic — enough to get representative proportions.
SAMPLE_LEDGER_COUNT = 60

# Estimated ledgers per day used for scaling sampled counts to daily totals.
LEDGERS_PER_DAY = 25_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_json(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"  [WARN] fetch_json({url}): {e}")
        return None


def xrpl_rpc(method, params=None, timeout=15):
    """Call XRPL JSON-RPC, trying each node in the pool."""
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in XRPL_NODES:
        try:
            req = urllib.request.Request(
                node, data=payload,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            result = data.get("result", {})
            if result.get("status") == "success" or "ledger" in result or "info" in result:
                return result
        except Exception as e:
            print(f"  [WARN] xrpl_rpc node={node} method={method}: {e}")
            continue
    return None


# ---------------------------------------------------------------------------
# Metric fetchers
# ---------------------------------------------------------------------------

def get_xrp_price():
    """Fetch live XRP/USD from CoinGecko free tier."""
    data = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=ripple&vs_currencies=usd&include_24hr_change=true"
    )
    if data and "ripple" in data:
        price = data["ripple"].get("usd")
        print(f"  [OK] XRP price: ${price}")
        return float(price)
    print("  [WARN] CoinGecko failed, using fallback price")
    return None


def get_server_info():
    """
    Fetch validated ledger info from XRPL.
    Returns dict with total_coins_xrp, ledger_index, tx_per_ledger.
    total_coins is in XRP (converted from drops ÷ 1,000,000).
    """
    result = xrpl_rpc("server_info")
    if not result:
        return None
    info = result.get("info", {})
    vl = info.get("validated_ledger", {})
    raw_coins = vl.get("total_coins")
    if not raw_coins:
        return None
    total_coins_xrp = int(raw_coins) / 1_000_000
    ledger_index = int(vl.get("seq", 0))
    tx_per_ledger = float(vl.get("tx_per_ledger", 0))
    print(f"  [OK] total_coins={total_coins_xrp:,.2f} XRP  ledger#{ledger_index}  avg_tx={tx_per_ledger:.1f}")
    return {
        "total_coins_xrp": total_coins_xrp,
        "ledger_index": ledger_index,
        "tx_per_ledger": tx_per_ledger,
    }


def get_ledger_transactions(ledger_index):
    """Fetch expanded transactions for a single ledger."""
    result = xrpl_rpc("ledger", {
        "ledger_index": ledger_index,
        "transactions": True,
        "expand": True,
    })
    if result and "ledger" in result:
        return result["ledger"].get("transactions", [])
    return []


def classify_tx(tx):
    """
    Map a TransactionType to one of the four institutional categories.

    Settlement  (#2ca02c): Payments, Checks — value transfer
    DeFi        (#ff7f0e): Offers, AMM ops — automated market activity
    Identity    (#9467bd): DID, Credentials, DepositPreauth — onboarding/KYC
    Acct Mgmt   (#7f7f7f): Everything else — wallet config, governance
    """
    t = tx.get("TransactionType", "")

    if t in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"):
        return "settlement"

    if t in (
        "OfferCreate", "OfferCancel",
        "AMMCreate", "AMMDeposit", "AMMWithdraw",
        "AMMBid", "AMMVote", "AMMDelete",
    ):
        return "defi"

    if t in (
        "DIDSet", "DIDDelete",
        "CredentialCreate", "CredentialAccept", "CredentialDelete",
        "DepositPreauth",
    ):
        return "identity"

    # AccountSet, AccountDelete, SignerListSet, SetRegularKey,
    # EscrowCreate/Finish/Cancel, PaymentChannelCreate/Fund/Claim,
    # NFT ops, XChain ops, Clawback, EnableAmendment, etc.
    return "acct_mgmt"


def sample_ledgers(current_ledger_index):
    """
    Sample SAMPLE_LEDGER_COUNT recent ledgers.
    Returns:
      tx_counts      — raw sampled counts per category
      payment_vol_xrp — total XRP moved in Payment txs (sampled)
      fee_drops      — total fees burned in sampled ledgers
      ledgers_ok     — how many ledgers were successfully fetched
    """
    tx_counts = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_vol_xrp = 0.0
    fee_drops = 0
    ledgers_ok = 0

    for i in range(SAMPLE_LEDGER_COUNT):
        idx = current_ledger_index - i
        txs = get_ledger_transactions(idx)
        if not txs:
            continue
        ledgers_ok += 1

        for tx in txs:
            if not isinstance(tx, dict):
                continue

            # Only count transactions that succeeded
            meta = tx.get("metaData") or tx.get("meta") or {}
            if isinstance(meta, dict):
                if meta.get("TransactionResult", "tesSUCCESS") != "tesSUCCESS":
                    continue

            cat = classify_tx(tx)
            tx_counts[cat] += 1

            # Accumulate fee burn (in drops)
            fee_drops += int(tx.get("Fee", 0))

            # Accumulate XRP payment volume (drops → XRP)
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):  # XRP is a string in drops
                    payment_vol_xrp += int(amt) / 1_000_000

        time.sleep(0.05)  # gentle throttle — ~3s total for 60 ledgers

    print(f"  [OK] Sampled {ledgers_ok}/{SAMPLE_LEDGER_COUNT} ledgers, "
          f"{sum(tx_counts.values())} txs, "
          f"{fee_drops/1e6:.4f} XRP fees burned")
    return tx_counts, payment_vol_xrp, fee_drops, ledgers_ok


# ---------------------------------------------------------------------------
# Main metric builder
# ---------------------------------------------------------------------------

def get_real_metrics(previous_total_coins=None):
    """
    Returns:
      burn_xrp        — XRP burned today (daily delta of total_coins)
      load_usd_m      — estimated daily payment volume in USD millions
      total_tx_m      — estimated daily transaction count in millions
      tx_cats         — dict of category → tx count in millions (real or {})
      load_cats       — dict of category → USD millions (real or {})
      is_simulated    — True only if XRPL RPC is completely unreachable
      current_total_coins — for storing in data.json for tomorrow's delta
    """
    is_simulated = False

    # -- 1. Price --
    xrp_price = get_xrp_price()
    if xrp_price is None:
        xrp_price = 2.36  # last-resort fallback; won't affect category proportions
        print(f"  [WARN] Using fallback price ${xrp_price}")

    # -- 2. Server info --
    server = get_server_info()
    current_total_coins = server["total_coins_xrp"] if server else None
    current_ledger = server["ledger_index"] if server else None

    # -- 3. Burn = daily delta of total_coins --
    if current_total_coins and previous_total_coins:
        burn_xrp = round(previous_total_coins - current_total_coins, 4)
        if burn_xrp < 0:
            # Shouldn't happen — guard against stale data
            burn_xrp = abs(burn_xrp)
        print(f"  [OK] Burn (delta method): {burn_xrp} XRP")
    else:
        burn_xrp = None  # will be estimated from sampled fees below
        print("  [INFO] No previous total_coins — burn will be estimated from sampled fees")

    # -- 4. Sample ledgers for real tx classification --
    tx_counts_raw = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_vol_xrp_sampled = 0.0
    fee_drops_sampled = 0
    ledgers_ok = 0

    if current_ledger:
        tx_counts_raw, payment_vol_xrp_sampled, fee_drops_sampled, ledgers_ok = \
            sample_ledgers(current_ledger)

    total_sampled = sum(tx_counts_raw.values())

    if ledgers_ok > 0 and total_sampled > 0:
        # Scale sampled data to full-day estimate
        scale = LEDGERS_PER_DAY / ledgers_ok

        total_tx_daily = round(total_sampled * scale / 1_000_000, 3)
        tx_cats = {
            k: round(v * scale / 1_000_000, 4)
            for k, v in tx_counts_raw.items()
        }

        # Payment volume: scale sampled XRP vol to daily, convert to USD millions
        payment_vol_daily_xrp = payment_vol_xrp_sampled * scale
        load_usd_m = round((payment_vol_daily_xrp * xrp_price) / 1_000_000, 2)

        # Load categories: proportional to tx category share
        # (settlement dominates because payments carry the most value)
        total_raw = max(total_sampled, 1)
        load_cats = {
            k: round(load_usd_m * (tx_counts_raw[k] / total_raw), 2)
            for k in tx_counts_raw
        }

        # Burn fallback: estimate from sampled fees if delta not available
        if burn_xrp is None:
            burn_xrp = round(fee_drops_sampled * scale / 1_000_000, 4)
            print(f"  [INFO] Burn (fee estimate): {burn_xrp} XRP/day")

        print(f"  [OK] Daily est: {total_tx_daily}M txs | ${load_usd_m}M load | "
              f"settlement={tx_cats['settlement']:.3f}M defi={tx_cats['defi']:.3f}M "
              f"identity={tx_cats['identity']:.4f}M acctmgmt={tx_cats['acct_mgmt']:.3f}M")

    else:
        # XRPL completely unreachable — mark as simulated, leave categories empty
        # so the chart shows the blue "In Progress" bar rather than fake coloured bars
        is_simulated = True
        total_tx_daily = None
        load_usd_m = None
        tx_cats = {}       # empty → triggers blue "In Progress" bar in index.html
        load_cats = {}     # empty → triggers blue "In Progress" bar in index.html
        if burn_xrp is None:
            burn_xrp = None
        print("  [ERROR] Could not reach XRPL — entry will be fully marked as simulated")

    return (
        burn_xrp,
        load_usd_m,
        total_tx_daily,
        tx_cats,
        load_cats,
        is_simulated,
        current_total_coins,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def update_data():
    sgt = pytz.timezone("Asia/Singapore")
    now = datetime.now(sgt)
    date_str = now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

    file_path = "data.json"

    # Load existing data
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []
    else:
        data = []

    # Find previous day's total_coins for burn delta calculation
    previous_total_coins = None
    for entry in reversed(data):
        if entry.get("date") != date_str and entry.get("total_coins_xrp"):
            previous_total_coins = entry["total_coins_xrp"]
            print(f"  [INFO] Found previous total_coins: {previous_total_coins:,.2f} XRP")
            break

    print(f"\n=== XRPBurn update: {timestamp_str} SGT ===")
    burn, load, tx, tx_cats, load_cats, is_simulated, current_total_coins = \
        get_real_metrics(previous_total_coins)

    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str,
        "burn_xrp": burn,
        "load_usd_m": load,
        "transactions": tx,
        "tx_categories": tx_cats,
        "load_categories": load_cats,
        "is_fallback": is_simulated,
        # Store total_coins so tomorrow's run can compute a proper burn delta
        "total_coins_xrp": current_total_coins,
    }

    # Replace today's entry if it exists, then keep last 90 days
    data = [e for e in data if e.get("date") != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    print(f"\n  Saved entry for {date_str}:")
    print(f"    burn={burn} XRP | load=${load}M | tx={tx}M | simulated={is_simulated}")


if __name__ == "__main__":
    update_data()
