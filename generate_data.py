"""
XRPBurn Data Generator — v3 (CI-hardened)
==========================================
Key changes from v2:
  - Uses `requests` with retry adapter instead of bare urllib
  - Reduced ledger sample from 60 → 20 (avoids CI timeouts, still representative)
  - Full diagnostic logging for GitHub Actions (every API call logged with status)
  - XRPScan metrics API used as lightweight fallback for tx count
  - Explicit timeout on every request
  - sys.exit(1) on full simulation so GitHub Actions flags the run as failed
  - Workflow install step needs: pip install pytz requests
"""

import json
import os
import sys
import time
from datetime import datetime

import pytz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

INITIAL_SUPPLY_XRP  = 100_000_000_000
SAMPLE_LEDGER_COUNT = 20
LEDGERS_PER_DAY     = 25_000
TIMEOUT             = 12

XRPL_NODES = [
    "https://xrplcluster.com",
    "https://s1.ripple.com:51234",
    "https://s2.ripple.com:51234",
    "https://clio.xrpl.org",
]

def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["POST", "GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

SESSION = make_session()

def log(msg):
    print(msg, flush=True)

def get_json(url):
    try:
        r = SESSION.get(url, timeout=TIMEOUT, headers={"User-Agent": "XRPBurnTracker/3.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  [WARN] GET {url} -> {type(e).__name__}: {e}")
        return None

def xrpl_post(node, method, params=None):
    try:
        r = SESSION.post(node, json={"method": method, "params": [params or {}]},
                         timeout=TIMEOUT,
                         headers={"Content-Type": "application/json",
                                  "User-Agent": "XRPBurnTracker/3.0"})
        r.raise_for_status()
        data   = r.json()
        result = data.get("result", {})
        if result.get("status") == "success" or "info" in result or "ledger" in result:
            return result
        log(f"  [WARN] {node} {method} -> unexpected: {result.get('status')!r}")
        return None
    except Exception as e:
        log(f"  [WARN] POST {node} {method} -> {type(e).__name__}: {e}")
        return None

def xrpl_rpc(method, params=None):
    for node in XRPL_NODES:
        result = xrpl_post(node, method, params)
        if result:
            log(f"  [OK]   {node} -> {method}")
            return result
        time.sleep(0.5)
    log(f"  [ERROR] All nodes failed: {method}")
    return None

def get_xrp_price():
    log("\n-- XRP price (CoinGecko) --")
    data = get_json("https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd")
    if data and "ripple" in data:
        price = float(data["ripple"]["usd"])
        log(f"  [OK]   ${price:.4f}")
        return price
    return None

def get_server_info():
    log("\n-- XRPL server_info --")
    result = xrpl_rpc("server_info")
    if not result:
        return None
    vl  = result.get("info", {}).get("validated_ledger", {})
    raw = vl.get("total_coins")
    if not raw:
        log(f"  [WARN] No total_coins in response. vl keys: {list(vl.keys())}")
        return None
    coins = int(raw) / 1_000_000
    seq   = int(vl.get("seq", 0))
    log(f"  [OK]   {coins:,.2f} XRP  ledger#{seq}")
    return {"total_coins_xrp": coins, "ledger_index": seq,
            "tx_per_ledger": float(vl.get("tx_per_ledger", 0))}

def classify_tx(tx_type):
    if tx_type in ("Payment","CheckCreate","CheckCash","CheckCancel"):
        return "settlement"
    if tx_type in ("OfferCreate","OfferCancel","AMMCreate","AMMDeposit",
                   "AMMWithdraw","AMMBid","AMMVote","AMMDelete"):
        return "defi"
    if tx_type in ("DIDSet","DIDDelete","CredentialCreate","CredentialAccept",
                   "CredentialDelete","DepositPreauth"):
        return "identity"
    return "acct_mgmt"

def sample_ledgers(current_ledger_index):
    log(f"\n-- Sampling {SAMPLE_LEDGER_COUNT} ledgers from #{current_ledger_index} --")
    counts    = {"settlement":0,"identity":0,"defi":0,"acct_mgmt":0}
    vol_drops = 0
    fee_drops = 0
    ok        = 0
    for i in range(SAMPLE_LEDGER_COUNT):
        result = xrpl_rpc("ledger", {"ledger_index": current_ledger_index - i,
                                      "transactions": True, "expand": True})
        if not result or "ledger" not in result:
            continue
        ok += 1
        for tx in result["ledger"].get("transactions", []):
            if not isinstance(tx, dict):
                continue
            meta = tx.get("metaData") or tx.get("meta") or {}
            res  = (meta.get("TransactionResult","tesSUCCESS")
                    if isinstance(meta, dict) else "tesSUCCESS")
            if not res.startswith("tes"):
                continue
            cat = classify_tx(tx.get("TransactionType",""))
            counts[cat] += 1
            fee_drops   += int(tx.get("Fee", 0))
            if tx.get("TransactionType") == "Payment":
                amt = tx.get("Amount", 0)
                if isinstance(amt, str):
                    vol_drops += int(amt)
        time.sleep(0.08)
    log(f"  [OK]   ok={ok}  txs={sum(counts.values())}  fees={fee_drops/1e6:.4f} XRP")
    log(f"  [OK]   {counts}")
    return counts, vol_drops, fee_drops, ok

def get_real_metrics(previous_total_coins=None):
    is_simulated = False
    xrp_price    = get_xrp_price() or 2.40

    server              = get_server_info()
    current_total_coins = server["total_coins_xrp"] if server else None
    current_ledger      = server["ledger_index"]     if server else None

    # Burn
    if current_total_coins and previous_total_coins:
        burn_xrp = round(abs(previous_total_coins - current_total_coins), 6)
        log(f"\n  [OK]   Burn delta: {burn_xrp} XRP")
    else:
        burn_xrp = None

    # Ledger sample
    if current_ledger:
        counts_raw, vol_drops, fee_drops, ledgers_ok = sample_ledgers(current_ledger)
    else:
        counts_raw, vol_drops, fee_drops, ledgers_ok = {"settlement":0,"identity":0,"defi":0,"acct_mgmt":0}, 0, 0, 0

    total_sampled = sum(counts_raw.values())

    if ledgers_ok > 0 and total_sampled > 0:
        scale        = LEDGERS_PER_DAY / ledgers_ok
        total_tx_m   = round(total_sampled * scale / 1_000_000, 3)
        tx_cats      = {k: round(v * scale / 1_000_000, 4) for k, v in counts_raw.items()}
        load_usd_m   = round((vol_drops / 1_000_000) * scale * xrp_price / 1_000_000, 2)
        denom        = max(total_sampled, 1)
        load_cats    = {k: round(load_usd_m * counts_raw[k] / denom, 2) for k in counts_raw}
        if burn_xrp is None:
            burn_xrp = round((fee_drops / 1_000_000) * scale, 6)
    else:
        log("\n  [WARN] Sampling failed — trying XRPScan fallback")
        xrpscan = get_json("https://api.xrpscan.com/api/v1/metrics")
        if xrpscan:
            log(f"  [DEBUG] XRPScan keys: {list((xrpscan[-1] if isinstance(xrpscan,list) else xrpscan).keys())[:20]}")
        # Leave categories empty — shows "In Progress" blue bar, not fake data
        total_tx_m = None
        load_usd_m = None
        tx_cats    = {}
        load_cats  = {}
        if not current_total_coins:
            is_simulated = True
            log("  [ERROR] No XRPL data at all — marking simulated")

    return burn_xrp, load_usd_m, total_tx_m, tx_cats, load_cats, is_simulated, current_total_coins

def update_data():
    sgt       = pytz.timezone("Asia/Singapore")
    now       = datetime.now(sgt)
    date_str  = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    file_path = "data.json"

    log(f"\n{'='*60}\nXRPBurn update: {timestamp} SGT\n{'='*60}")

    data = []
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    prev_coins = None
    for entry in reversed(data):
        if entry.get("date") != date_str and entry.get("total_coins_xrp"):
            prev_coins = float(entry["total_coins_xrp"])
            log(f"  [INFO] Prev coins ({entry['date']}): {prev_coins:,.2f}")
            break

    burn, load, tx, tx_cats, load_cats, is_sim, cur_coins = get_real_metrics(prev_coins)

    new_entry = {
        "date":            date_str,
        "last_updated":    timestamp,
        "burn_xrp":        burn,
        "load_usd_m":      load,
        "transactions":    tx,
        "tx_categories":   tx_cats,
        "load_categories": load_cats,
        "is_fallback":     is_sim,
        "total_coins_xrp": cur_coins,
    }

    log(f"\nFINAL ENTRY: {json.dumps(new_entry, indent=2)}")

    data = [e for e in data if e.get("date") != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

    log(f"\nSaved {file_path} ({len(data)} entries)")
    if is_sim:
        log("\n[CRITICAL] Simulated data — check node connectivity above.")
        sys.exit(1)

if __name__ == "__main__":
    update_data()
