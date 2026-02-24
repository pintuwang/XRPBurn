import json
import os
from datetime import datetime
import pytz

def update_data():
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = []

    # Simulation logic: In a real setup, this would fetch the 
    # CUMULATIVE total for the current UTC/SGT day from an XRPL API.
    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": 442,        # This would be the "total burned so far today"
        "load_usd_m": 2120,     
        "transactions": 1.1,     
        "categories": {
            "settlement": 1.3,
            "identity": 0.4,
            "defi": 0.3,
            "acct_mgmt": 0.12
        }
    }

    # "Upsert" Logic: 
    # 1. Remove the existing entry for 'today' if it exists.
    # 2. Append the updated 'today' entry.
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    
    # Maintain 90-day history (90 entries)
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
