import json
import os
from datetime import datetime
import pytz

def update_data():
    # Define Singapore Timezone
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    
    # Load existing data
    file_path = 'data.json'
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = []

    # New daily data entry (Sample logic - replace with your actual API fetch)
    # Burn is in XRP, Load is in USD (Millions)
    new_entry = {
        "date": date_str,
        "burn_xrp": 450,        # Replace with actual daily burn
        "load_usd_m": 2100,     # Replace with actual daily load
        "transactions": 1.2,     # Millions
        "categories": {
            "settlement": 0.8,
            "identity": 0.2,
            "defi": 0.15,
            "acct_mgmt": 0.05
        }
    }

    # Append and prevent duplicates for the same day
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    
    # Keep only last 90 days for performance
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
