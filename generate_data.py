import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import pytz

# Configuration
XRPL_NODES = ["https://xrplcluster.com", "https://xrpl.ws", "https://s2.ripple.com"]
SAMPLE_LEDGER_COUNT = 150  # Increased for better statistical stability
LEDGERS_PER_DAY = 25000
MAX_PAYMENT_XRP = 10_000_000  # Cap at 10M XRP to filter internal shuffles
REQUEST_TIMEOUT = 30

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "XRPBurnTracker/6.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception: return None

def xrpl_rpc(method, params=None):
    payload = json.dumps({"method": method, "params": [params or {}]}).encode()
    for node in XRPL_NODES:
        try:
            req = urllib.request.Request(node, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                data = json.loads(r.read().decode())
            if data.get("result", {}).get("status") == "success":
                return data["result"]
        except Exception: continue
    return None

def get_ledger_info():
    res = xrpl_rpc("ledger", {"ledger_index": "validated"})
    if not res: return None
    ld = res.get("ledger") or res.get("closed", {}).get("ledger", {})
    return {
        "coins": int(ld["total_coins"]) / 1e6 if ld.get("total_coins") else None,
        "seq": int(ld.get("ledger_index") or ld.get("seqNum") or 0)
    }

def classify(tx_type):
    if tx_type in ("Payment", "CheckCreate", "CheckCash", "CheckCancel"): return "settlement"
    if tx_type in ("OfferCreate", "OfferCancel", "AMMCreate", "AMMDeposit", "AMMWithdraw", "AMMBid", "AMMVote", "AMMDelete"): return "defi"
    if tx_type in ("DIDSet", "DIDDelete", "CredentialCreate", "CredentialAccept", "CredentialDelete", "DepositPreauth"): return "identity"
    return "acct_mgmt"

def sample_ledgers(start_seq):
    counts = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    payment_amounts = []
    ledgers_ok = 0
    for i in range(SAMPLE_LEDGER_COUNT):
        res = xrpl_rpc("ledger", {"ledger_index": start_seq - i, "transactions": True, "expand": True})
        if not res: continue
        txs = res.get("ledger", {}).get("transactions", [])
        if not txs: continue
        ledgers_ok += 1
        for tx in txs:
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):
                    xrp_amt = int(amt) / 1e6
                    if xrp_amt <= MAX_PAYMENT_XRP: payment_amounts.append(xrp_amt)
            counts[classify(tx.get("TransactionType", ""))] += 1
        time.sleep(0.04)
    return counts, payment_amounts, ledgers_ok

def get_real_metrics(prev_entry):
    price_data = fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd")
    xrp_price = float(price_data["ripple"]["usd"]) if price_data else 2.30

    info = get_ledger_info()
    if not info: return None, None, None, {}, {}, True, None

    # Burn Calculation (with Gap Recovery)
    burn_xrp = None
    if prev_entry and prev_entry.get("total_coins_xrp"):
        days_diff = (datetime.now() - datetime.strptime(prev_entry["date"], "%Y-%m-%d")).days
        total_burn = float(prev_entry["total_coins_xrp"]) - info["coins"]
        burn_xrp = round(total_burn / max(1, days_diff), 6)

    # Sampling
    counts, payment_amts, ledgers_ok = sample_ledgers(info["seq"])
    total_sampled = sum(counts.values())

    if not ledgers_ok or not total_sampled:
        return burn_xrp, None, None, {}, {}, False, info["coins"]

    scale = LEDGERS_PER_DAY / ledgers_ok
    total_tx_m = round(total_sampled * scale / 1e6, 4)
    tx_cats = {k: round(v * scale / 1e6, 4) for k, v in counts.items()}

    # Median Method for Load
    payment_amts.sort()
    median_val = payment_amts[len(payment_amts)//2] if payment_amts else 0
    load_usd_m = round((median_val * (counts["settlement"] * scale) * xrp_price) / 1e6, 2)
    load_cats = {k: round(load_usd_m * (v / total_sampled), 2) for k, v in counts.items()}

    return burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats, False, info["coins"]

def update_data():
    sgt = pytz.timezone("Asia/Singapore")
    date_str = datetime.now(sgt).strftime("%Y-%m-%d")
    file_path = "data.json"
    
    data = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try: data = json.load(f)
            except: data = []

    prev = next((e for e in reversed(data) if e["date"] != date_str), None)
    burn, load, tx, tx_cats, load_cats, is_sim, coins = get_real_metrics(prev)

    new_entry = {
        "date": date_str,
        "last_updated": datetime.now(sgt).strftime("%Y-%m-%d %H:%M:%S"),
        "burn_xrp": burn,
        "load_usd_m": load,
        "transactions": tx,
        "tx_categories": tx_cats,
        "load_categories": load_cats,
        "is_fallback": is_sim,
        "total_coins_xrp": coins
    }

    data = [e for e in data if e["date"] != date_str] + [new_entry]
    with open(file_path, "w") as f: json.dump(data[-90:], f, indent=4)

if __name__ == "__main__":
    update_data()
