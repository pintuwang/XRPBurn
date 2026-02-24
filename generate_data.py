import json
import os
import urllib.request
from datetime import datetime
import pytz

def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return None

def get_real_metrics():
    # 1. Fetch live XRP Price
    price_data = fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd")
    xrp_price = price_data['ripple']['usd'] if price_data else 1.40

    # 2. Fetch Network Statistics
    stats = fetch_json("https://api.xrpscan.com/api/v1/statistics")
    tx_types = fetch_json("https://api.xrpscan.com/api/v1/statistics/transactions")

    # DEFAULT BASELINES
    burn_xrp = 440
    total_tx_count = 1200000
    payment_vol_xrp = 2100000000
    is_simulated = False
    
    # Check if we have basic network stats
    if stats and stats.get('xrp_burned', 0) > 0:
        burn_xrp = stats.get('xrp_burned')
        total_tx_count = stats.get('transaction_count')
        payment_vol_xrp = stats.get('payment_volume')
    else:
        is_simulated = True # API failed, using hardcoded baselines

    load_usd_m = (payment_vol_xrp * xrp_price) / 1_000_000
    total_tx_m = round(total_tx_count / 1_000_000, 3)

    # 3. Handle Categories
    tx_cats = {}
    load_cats = {}
    
    # Check if category breakdown is available
    if tx_types and any(item.get('count', 0) > 0 for item in tx_types):
        tx_counts = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
        for item in tx_types:
            t_type = item.get('type')
            count = item.get('count', 0)
            if t_type == 'Payment': tx_counts["settlement"] += count
            elif t_type in ['OfferCreate', 'AMMDeposit', 'AMMCreate']: tx_counts["defi"] += count
            elif t_type in ['AccountSet', 'DIDSet', 'CredentialCreate']: tx_counts["identity"] += count
            else: tx_counts["acct_mgmt"] += count
        
        tx_cats = {k: round(v / 1_000_000, 3) for k, v in tx_counts.items()}
        load_cats = {
            "settlement": round(load_usd_m * 0.90, 2),
            "identity": round(load_usd_m * 0.02, 2),
            "defi": round(load_usd_m * 0.07, 2),
            "acct_mgmt": round(load_usd_m * 0.01, 2)
        }
    elif is_simulated:
        # If no API data at all, provide a simulated breakdown for the balloon
        tx_cats = {"settlement": total_tx_m * 0.7, "identity": total_tx_m * 0.15, "defi": total_tx_m * 0.1, "acct_mgmt": total_tx_m * 0.05}
        load_cats = {"settlement": load_usd_m * 0.9, "identity": load_usd_m * 0.02, "defi": load_usd_m * 0.07, "acct_mgmt": load_usd_m * 0.01}

    # Note: If not simulated but categories are empty, it stays empty {} to trigger Blue "In Progress" bar
    return burn_xrp, round(load_usd_m, 2), total_tx_m, tx_cats, load_cats, is_simulated

def update_data():
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    burn, load, tx, tx_cats, load_cats, is_simulated = get_real_metrics()

    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            try:
                data = json.load(f)
            except:
                data = []
    else:
        data = []

    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": burn,
        "load_usd_m": load,
        "transactions": tx,
        "tx_categories": tx_cats,
        "load_categories": load_cats,
        "is_fallback": is_simulated
    }

    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
