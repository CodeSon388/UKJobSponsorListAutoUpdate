import pandas as pd
import requests
from bs4 import BeautifulSoup
import os
import io
from datetime import datetime
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GOV_UK_URL = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
MASTER_FILE = "master_register.csv"
STATS_FILE = "stats.json"
HISTORY_FILE = "history.json"
DELTA_FILE = "daily_delta.json"

def get_csv_url():
    """Scrapes the GOV.UK page to find the latest CSV download link and extracts the date."""
    try:
        logging.info(f"Fetching {GOV_UK_URL}...")
        response = requests.get(GOV_UK_URL)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Look for the link
        link = soup.find('a', string=lambda t: t and 'Register of Worker and Temporary Worker licensed sponsors' in t)
        
        if not link:
             links = soup.find_all('a', href=True)
             for l in links:
                 if l['href'].lower().endswith('.csv') and 'worker' in l['href'].lower():
                     link = l
                     break
        
        if link:
            url = link['href']
            if not url.startswith('http'):
                url = "https://www.gov.uk" + url
            
            logging.info(f"Found CSV URL: {url}")
            
            # Extract date from URL (e.g., .../2026-01-15_-_Worker_...)
            # Default to today if extraction fails
            extracted_date = datetime.now().strftime('%Y-%m-%d')
            try:
                # Common format: /YYYY-MM-DD_-_Worker...
                filename = url.split('/')[-1]
                # Regex or simple split might work. It usually starts with YYYY-MM-DD
                possible_date = filename[:10]
                # Validate it's a date
                datetime.strptime(possible_date, '%Y-%m-%d')
                extracted_date = possible_date
                logging.info(f"Extracted date from filename: {extracted_date}")
            except Exception as e:
                logging.warning(f"Could not extract date from filename ({filename}), using today's date: {e}")

            return url, extracted_date
        else:
            raise Exception("Could not find CSV link on the page.")

    except Exception as e:
        logging.error(f"Error extracting CSV URL: {e}")
        raise

def download_csv(url):
    """Downloads the CSV content."""
    logging.info("Downloading CSV...")
    # ... previous code ...
    response = requests.get(url)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.content.decode('utf-8')))

def generate_unique_id(df):
    """Generates a unique ID for each sponsor."""
    # Clean column names (strip whitespace)
    df.columns = df.columns.str.strip()
    
    # Ensure all columns used for ID are string type and fill NaNs
    cols = ["Organisation Name", "Town/City", "County", "Type & Rating", "Route"]
    
    # Normalize City names (Title Case) to merge "London" and "LONDON"
    # We do this specifically for grouping
    if "Town/City" in df.columns:
        df["Town/City"] = df["Town/City"].astype(str).fillna("").str.strip().str.title()
    else:
        logging.warning("Town/City column not found during normalization!")

    for col in cols:
        if col not in df.columns:
            df[col] = ""
        # Ensure string and strip whitespace
        if col != "Town/City": # Already processed
             df[col] = df[col].astype(str).fillna("").str.strip()
    
    return df.apply(lambda row: f"{row['Organisation Name']}|{row['Town/City']}|{row['County']}|{row['Type & Rating']}|{row['Route']}", axis=1)

def update_master_register(new_df, file_date):
    """Updates the master register with new data."""
    
    # Use the File Date as the "Today" for data purposes
    today = file_date
    new_df['id'] = generate_unique_id(new_df)
    
    # DEDUPLICATE: The government file sometimes has duplicate rows.
    # We drop them so Total Active matches unique added counts.
    new_df = new_df.drop_duplicates(subset=['id'])
    
    if os.path.exists(MASTER_FILE):
        logging.info("Loading master register...")
        master_df = pd.read_csv(MASTER_FILE)
    else:
        logging.info("Creating new master register...")
        master_df = pd.DataFrame(columns=list(new_df.columns) + ['first_seen', 'last_updated', 'removed_date'])
    
    # Ensure 'id' column exists in master
    if 'id' not in master_df.columns and not master_df.empty:
         master_df['id'] = generate_unique_id(master_df)

    # Convert dataframe to dictionary for easier processing or set index
    # Using 'id' as index for efficient operations
    
    # 1. Identify New Entries
    existing_ids = set(master_df[master_df['removed_date'].isna()]['id']) # Only check against currently active
    new_ids = set(new_df['id'])
    
    added_ids = new_ids - existing_ids
    
    # 2. Identify Removed Entries
    # Removed means it was active (or just in master) but NOT in new_ids
    # We should look at all ids in master that are NOT removed yet
    active_master_ids = set(master_df[master_df['removed_date'].isna()]['id'])
    removed_ids = active_master_ids - new_ids
    
    logging.info(f"New entries: {len(added_ids)}")
    logging.info(f"Removed entries: {len(removed_ids)}")
    
    # Process New Entries
    new_entries = new_df[new_df['id'].isin(added_ids)].copy()
    new_entries['first_seen'] = today
    new_entries['last_updated'] = today
    new_entries['removed_date'] = None
    
    # Append new entries to master
    if not new_entries.empty:
        master_df = pd.concat([master_df, new_entries], ignore_index=True)
    
    # Process Removed Entries
    if removed_ids:
        master_df.loc[(master_df['id'].isin(removed_ids)) & (master_df['removed_date'].isna()), 'removed_date'] = today
    
    # Process Updates (Existing entries that are still present)
    # We update 'last_updated' for all entries in new_df that match master
    # Ideally we find rows in master where id is in new_ids and update last_seen
    # Note: re-finding strict matches
    
    # Mark 'last_updated' for all currently present IDs
    master_df.loc[master_df['id'].isin(new_ids), 'last_updated'] = today
    
    # Save Master
    logging.info(f"Saving {MASTER_FILE}...")
    master_df.to_csv(MASTER_FILE, index=False)
    
    return master_df, file_date

def generate_stats(master_df, file_date):
    """Generates statistics from the master register."""
    
    # Calculate daily counts from Master (Cumulative for the date)
    added_today_df = master_df[master_df['first_seen'] == file_date]
    removed_today_df = master_df[master_df['removed_date'] == file_date]
    added_count = len(added_today_df)
    removed_count = len(removed_today_df)

    active_df = master_df[master_df['removed_date'].isna()].copy()
    
    # Ensure standard casing for stats (even if master has inconsistencies)
    if "Town/City" in active_df.columns:
        active_df["Town/City"] = active_df["Town/City"].astype(str).fillna("").str.strip().str.title()
    
    # Use UTC for standard server time
    stats = {
        "generated_at": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC'),
        "daily_metrics": {
            "added_today": added_count,
            "removed_today": removed_count,
            "total_active_sponsors": len(active_df)
        },
        "categorical_totals": {
            "unique_organisations": int(active_df['Organisation Name'].nunique()),
            "unique_cities": int(active_df['Town/City'].nunique()),
            "unique_routes": int(active_df['Route'].nunique())
        },
        "rankings": {
            "top_routes": active_df['Route'].value_counts().head(5).to_dict(),
            "top_cities": active_df['Town/City'].value_counts().head(5).to_dict(),
            "top_ratings": active_df['Type & Rating'].value_counts().head(5).to_dict()
        },
        "recency": {
            "added_last_7_days": [], 
            "removed_last_14_days": [] 
        }
    }
    
    # Recency Lists Logic
    master_df['first_seen_dt'] = pd.to_datetime(master_df['first_seen'], errors='coerce')
    master_df['removed_date_dt'] = pd.to_datetime(master_df['removed_date'], errors='coerce')
    
    # Use baseline date for comparison logic
    if added_count > (len(active_df) * 0.9) and file_date == "2026-01-15":
        logging.info("Bulk import detected (Day 1). Skipping 'Added Last 7 Days' list to avoid UI clutter.")
        stats['recency']['added_last_7_days'] = []
    else:
        recent_added = master_df[
            (master_df['first_seen_dt'] >= (pd.Timestamp(file_date) - pd.Timedelta(days=7))) & 
            (master_df['first_seen'] != "2026-01-15") & 
            (master_df['removed_date'].isna())
        ].sort_values('first_seen_dt', ascending=False)
        stats['recency']['added_last_7_days'] = recent_added[['Organisation Name', 'Town/City', 'Route', 'first_seen']].head(1000).to_dict('records')
    
    recent_removed = master_df[
        master_df['removed_date_dt'] >= (pd.Timestamp(file_date) - pd.Timedelta(days=14))
    ].sort_values('removed_date_dt', ascending=False)
    
    stats['recency']['removed_last_14_days'] = recent_removed[['Organisation Name', 'Town/City', 'Route', 'removed_date']].to_dict('records')

    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=4)
    logging.info(f"Saved stats to {STATS_FILE}")

def update_history_log(master_df, file_date):
    """Appends the cumulative daily stats for file_date to a persistent history file."""
    
    added_count = len(master_df[master_df['first_seen'] == file_date])
    removed_count = len(master_df[master_df['removed_date'] == file_date])
    total_active = len(master_df[master_df['removed_date'].isna()])
    
    history_data = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                history_data = json.load(f)
        except:
             history_data = []
    
    entry = next((item for item in history_data if item["date"] == file_date), None)
    
    if entry:
        entry["added"] = added_count
        entry["removed"] = removed_count
        entry["total"] = total_active
    else:
        history_data.append({
            "date": file_date,
            "added": added_count,
            "removed": removed_count,
            "total": total_active
        })
    
    history_data.sort(key=lambda x: x['date'])
    
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history_data, f, indent=4)
    logging.info(f"Updated {HISTORY_FILE}")

def generate_daily_delta(master_df, file_date):
    """Generates a tiny JSON file containing all changes for the given date."""
    added_df = master_df[master_df['first_seen'] == file_date]
    removed_df = master_df[master_df['removed_date'] == file_date]
    
    delta = {
        "date": file_date,
        "added": added_df[['Organisation Name', 'Town/City', 'Route', 'Type & Rating', 'first_seen']].to_dict('records'),
        "removed": removed_df[['Organisation Name', 'Town/City', 'Route', 'Type & Rating', 'removed_date']].to_dict('records')
    }
    
    with open(DELTA_FILE, 'w') as f:
        json.dump(delta, f, indent=4)
    logging.info(f"Generated {DELTA_FILE}")

def main():
    try:
        csv_url, file_date = get_csv_url()
        new_df = download_csv(csv_url)
        master_df, data_date = update_master_register(new_df, file_date)
        
        generate_stats(master_df, data_date)
        update_history_log(master_df, data_date)
        generate_daily_delta(master_df, data_date)
        
        print("Tracker run completed successfully.")
        
    except Exception as e:
        logging.error(f"Tracker failed: {e}")
        exit(1)

if __name__ == "__main__":
    main()
