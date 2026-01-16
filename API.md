# UK Sponsor Register Tracker API Documentation

This API provides public access to the accumulated database of UK licensed sponsors. Since this is hosted on GitHub Pages, access is provided via **Static JSON/CSV Endpoints**.

## Endpoints

### 1. Daily Delta (Recommended for Syncing)
**URL**: `https://Codeson388.github.io/UKJobSponsorListAutoUpdate/daily_delta.json`
- **Purpose**: Low-bandwidth way to get only the sponsors added or removed in the latest update.
- **Format**: JSON
- **Update Frequency**: 3x Daily (06:00, 12:00, 18:00 UTC)

### 2. Live Statistics
**URL**: `https://Codeson388.github.io/UKJobSponsorListAutoUpdate/stats.json`
- **Purpose**: Get current counts, top rankings, and recent changes (last 7-14 days).
- **Format**: JSON

### 3. Historical Trends
**URL**: `https://Codeson388.github.io/UKJobSponsorListAutoUpdate/history.json`
- **Purpose**: Daily record of add/remove counts since 2026-01-15.
- **Format**: JSON

### 4. Full Database
**URL**: `https://Codeson388.github.io/UKJobSponsorListAutoUpdate/master_register.csv`
- **Purpose**: Complete accumulated database containing every sponsor ever seen.
- **Format**: CSV (~15MB+)
- **Columns**: Organisation Name, Town/City, County, Type & Rating, Route, first_seen, last_updated, removed_date

## Usage Policy

- **Authentication**: None required. Endpoints are public.
- **Rate Limits**: Subject to GitHub Pages infrastructure limits.
- **Best Practice**: For syncing external databases, please pull `daily_delta.json` rather than the full `master_register.csv` to conserve bandwidth.

---
**Disclaimer**: This API is an independent project and is not affiliated with GOV.UK. Data is provided "as is" with no liability for accuracy.
