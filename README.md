# UK Sponsor Register Tracker 🇬🇧

A fully automated tool to track the **UK Government's Register of Licensed Sponsors**.

🚀 **Live Dashboard:** [Link to your GitHub Pages URL will go here]

## Features
- **Daily Automation**: Runs every morning at 06:00 UTC via GitHub Actions.
- **Auto-Discovery**: Scrapes the official GOV.UK website for the latest daily CSV file.
- **Delta Tracking**: 
  - Tracks **New Sponsors** (First Seen Date).
  - Tracks **Removed Sponsors** (Removed Date).
- **Smart Data**:
  - Deduplicates repeated entries.
  - Normalizes city names (e.g., "London" vs "LONDON").
- **Dashboard**: Simple, responsive HTML dashboard visualizing:
  - Top Employee Routes.
  - Top Cities.
  - Sponsor Ratings.
  - Recency lists (Added last 7 days / Removed last 14 days).

## Setup (Local)
1. Install requirements: `pip install -r requirements.txt`
2. Run the tracker: `python tracker.py`
3. View the dashboard: Open `index.html` in your browser.

## Deployment
This repo is configured for **GitHub Actions**.
1. Push to GitHub.
2. Go to **Settings > Pages**.
3. Enable GitHub Pages from the `main` branch.

## Files
- `tracker.py`: Core logic script.
- `master_register.csv`: The persistent database of all sponsors.
- `stats.json`: Daily statistics fed into the dashboard.
- `.github/workflows/daily_update.yml`: Automation configuration.
