import json
import os
import urllib.request
from datetime import datetime
import pytz

def fetch_json(url):
    """Helper to fetch JSON data from a URL without external dependencies like 'requests'."""
    try:
        with urllib.request.urlopen(url) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

def get_real_metrics():
    # 1. Get XRP Price from CoinGecko
    price_data = fetch_json("https://api.coingecko.com/api/v3/simple/price?ids=ripple&vs_currencies=usd")
    xrp_price = price_data['ripple']['usd'] if price_data else 1.0

    # 2. Get Network Statistics (Burn, Volume, and Transaction types)
    # We use XRPScan's public API endpoints
    stats = fetch_json("https://api.xrpscan.com/api/v1/statistics")
    tx_types = fetch_json("https://api.xrpscan.com/api/v1/statistics/transactions")

    # Default fallbacks if API is unavailable
    burn_xrp = 450
    total_tx_m = 1.2
    payment_vol_xrp = 2100000000
    
    if stats:
        # Note: These fields are mapped from the live explorer's 24h metrics
        burn_xrp = stats.get('xrp_burned', 450)
        total_tx_m = stats.get('transaction_count', 1200000) / 1_000_000
        # For USD Load, we use the payment volume in XRP * price
        payment_vol_xrp = stats.get('payment_volume', 2100000000)

    load_usd_m = (payment_vol_xrp * xrp_price) / 1_000_000

    # 3. Categorize Transactions based on on-chain types
    categories = {"settlement": 0, "identity": 0, "defi": 0, "acct_mgmt": 0}
    
    if tx_types:
        for item in tx_types:
            t_type = item.get('type')
            count = item.get('count', 0)
            
            # MAPPING LOGIC:
            # Settlement -> Standard Payments (XRP or Issued Currencies)
            if t_type == 'Payment':
                categories["settlement"] += count
            
            # DeFi -> DEX Offers & AMM Liquidity
            elif t_type in ['OfferCreate', 'OfferCancel', 'AMMCreate', 'AMMDeposit', 'AMMWithdraw', 'AMMVote', 'AMMBid']:
                categories["defi"] += count
            
            # Identity -> Account settings, DIDs, and Credentials
            elif t_type in ['AccountSet', 'DIDSet', 'DIDDelete', 'CredentialCreate', 'CredentialAccept', 'CredentialDelete']:
                categories["identity"] += count
            
            # Account Mgmt -> Trust lines, multi-sig, and housekeeping
            else:
                categories["acct_mgmt"] += count
    
    # Convert counts to Millions for the chart
    final_cats = {k: round(v / 1_000_000, 3) for k, v in categories.items()}

    return burn_xrp, round(load_usd_m, 2), round(total_tx_m, 3), final_cats

def update_data():
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    
    # Fetch actual on-chain data
    burn, load, tx, cats = get_real_metrics()

    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = []

    # New entry for today
    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": burn,
        "load_usd_m": load,
        "transactions": tx,
        "categories": cats
    }

    # "Upsert": replace today's entry as it grows hourly
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    
    # Maintain 90-day history
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
