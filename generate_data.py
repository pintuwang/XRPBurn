import json
import os
from datetime import datetime
import pytz

def update_data():
    """
    Updates the data.json file with the latest XRP burn and network load metrics.
    Handles Singapore Timezone and maintains a rolling 90-day history.
    """
    # Define Singapore Timezone (SGT = UTC+8)
    sgt = pytz.timezone('Asia/Singapore')
    now = datetime.now(sgt)
    date_str = now.strftime('%Y-%m-%d')
    
    # Captures the precise time of the execution to update the dashboard timestamp
    # This ensures manual updates are reflected accurately in the "Last updated" field.
    timestamp_str = now.strftime('%Y-%m-%d %H:%M:%S')
    
    file_path = 'data.json'
    
    # Load existing data from the JSON file
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            # Start with an empty list if the file is corrupted or empty
            data = []
    else:
        data = []

    # New daily data entry with categorized metrics
    # Note: Replace these placeholders with your actual API or data fetching logic.
    new_entry = {
        "date": date_str,
        "last_updated": timestamp_str, 
        "burn_xrp": 442,        # Placeholder for actual daily XRP burn data
        "load_usd_m": 2120,     # Placeholder for actual network load in USD millions
        "transactions": 1.1,     # Placeholder for total transactions in millions
        "categories": {         # Categorized breakdown of transaction types
            "settlement": 1.3,
            "identity": 0.4,
            "defi": 0.3,
            "acct_mgmt": 0.12
        }
    }

    # Replace the existing entry for today to update the timestamp on manual runs
    # This prevents duplicate entries for the same calendar date.
    data = [entry for entry in data if entry['date'] != date_str]
    data.append(new_entry)
    
    # Keep only the last 90 days of data for optimal performance
    data = data[-90:]

    # Save the updated dataset back to data.json
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=4)

if __name__ == "__main__":
    update_data()
