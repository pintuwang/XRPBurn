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
    xrp_price = price_data['ripple']['usd'] if price_data else 1.0

    # 2. Fetch Network Statistics (Totals)
    stats = fetch_json("https://api.xrpscan.com/api/v1/statistics")
    # 3. Fetch Transaction Types (Categories)
    tx_types = fetch_json("https://api.xrpscan.com/api/v1/statistics/transactions")

    burn_xrp = 0
    total_tx_count = 0
    payment_vol_xrp = 0
    
    if stats:
        burn_xrp = stats.get('xrp_burned', 0)
        total_tx_count = stats.get('transaction_count', 0)
        payment_vol_xrp = stats.get('payment_volume', 0)

    load_usd_m = (payment_vol_xrp * xrp_price) / 1_000_000
    total_tx_m = round(total_tx_count / 1_000_000, 3)

    # Calculate Categories
    cats = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    
    if tx_types:
        for item in tx_types:
            t_type = item.get('type')
            count = item.get('count', 0)
            if t_type == 'Payment': cats["settlement"] += count
            elif t_type in ['OfferCreate', 'AMMDeposit', 'AMMCreate']: cats["defi"] += count
            elif t_type in ['AccountSet', 'DIDSet', 'CredentialCreate']: cats["identity"] += count
            else: cats["acct_mgmt"] += count
    
    # If the categories sum to 0, it means the API hasn't released today's breakdown yet
    if sum(cats.values()) == 0:
        final_cats = {} # Empty object signals "In Progress"
    else:
        final_cats = {k: round(v / 1_000_000, 3) for k, v in cats.items()}

    return burn_xrp, round(load_usd_m, 2), total_tx_m, final_cats

def update_data():
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    burn, load, tx, cats = get_real_metrics()

    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = []

    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": burn,
        "load_usd_m": load,
        "transactions": tx,
        "categories": cats
    }

    # Replace today's entry (Update hourly)
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
