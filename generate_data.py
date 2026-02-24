import json
import os
from datetime import datetime
import pytz

def update_data():
    # Define Singapore Timezone
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    
    # Load existing data
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            data = json.load(f)
    else:
        data = []

    # Logic for "Growing" data:
    # In a real API integration, you would fetch the total burned 'since midnight SGT'.
    # Here we simulate the growth for Feb 24 based on the current hour.
    current_hour = now.hour
    
    # Example scaling: data grows as the day progresses
    # At 11:00 AM (hour 11), we show roughly 11/24ths of a full day's potential
    progress_factor = max(0.1, current_hour / 24.0)

    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": round(450 * progress_factor), # Grows toward ~450
        "load_usd_m": round(2100 * progress_factor), # Grows toward ~2100
        "transactions": round(1.2 * progress_factor, 2), # Grows toward ~1.2M
        "categories": {
            "settlement": round(1.3 * progress_factor, 2),
            "identity": round(0.4 * progress_factor, 2),
            "defi": round(0.3 * progress_factor, 2),
            "acct_mgmt": round(0.12 * progress_factor, 2)
        }
    }

    # UPSERT: If today's date exists, replace it with the new growing total.
    # If it's a new day, this simply appends.
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    
    # Keep 90 days of history
    data = data[-90:]

    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
