"""
UK Sponsor Licence Churn Tracker.

Runs daily via GitHub Actions. Pulls the latest gov.uk register CSV, diffs it
against the current master_register.csv using the new licence_id scheme, and
emits four event types:

    added       -- a licence_id seen for the first time
    upgraded    -- existing licence_id, rating tier increased
    downgraded  -- existing licence_id, rating tier decreased
    gone        -- existing licence_id no longer in today's CSV
                   (撤銷 and 消失 are not split; the register CSV provides
                    no signal to distinguish them)

Outputs:
    master_register.csv          identity + current state
    events/events-YYYY-MM.json   append-only churn log, partitioned by month
    events/events-index.json     metadata for dashboard discovery
    daily_delta.json             today's events bucketed by type
    stats.json                   dashboard metrics (with backward-compat aliases)
    history.json                 per-day counts (extended schema)
    county_corrections.json      silent county updates from two-phase match
    parse_failures.json          rows whose Type & Rating couldn't be parsed
    suspected_false_churn.json   Levenshtein-near add/gone pairs for review
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections import defaultdict
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

from normalisation import (
    compute_licence_id,
    compute_loose_id,
    normalise_county,
    normalise_name,
    normalise_route,
    normalise_town,
    parse_type_rating,
    rating_rank,
)
from licence_history import update_licence_month_index_for

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# --------------------------------------------------------------------- paths
GOV_UK_URL = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
MASTER_FILE = "master_register.csv"
EVENTS_DIR = "events"
EVENTS_INDEX = os.path.join(EVENTS_DIR, "events-index.json")
STATS_FILE = "stats.json"
HISTORY_FILE = "history.json"
DELTA_FILE = "daily_delta.json"
COUNTY_CORRECTIONS_FILE = "county_corrections.json"
PARSE_FAILURES_FILE = "parse_failures.json"
FALSE_CHURN_FILE = "suspected_false_churn.json"

REQUEST_TIMEOUT_SECONDS = 30
LEVENSHTEIN_THRESHOLD = 3  # max edit distance for false-churn pairing
FALSE_CHURN_DAY_WINDOW = 3  # days


# --------------------------------------------------------------------- fetch
def get_csv_url() -> tuple[str, str]:
    """Scrape gov.uk for the latest CSV download link and its publication date."""
    logging.info(f"Fetching {GOV_UK_URL}...")
    response = requests.get(GOV_UK_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")

    link = soup.find(
        "a",
        string=lambda t: t and "Register of Worker and Temporary Worker licensed sponsors" in t,
    )
    if not link:
        for candidate in soup.find_all("a", href=True):
            href = candidate["href"].lower()
            if href.endswith(".csv") and "worker" in href:
                link = candidate
                break
    if not link:
        raise RuntimeError("Could not find CSV link on the gov.uk page.")

    url = link["href"]
    if not url.startswith("http"):
        url = "https://www.gov.uk" + url

    # Extract date from the filename; fall back to today on failure.
    extracted_date = datetime.now().strftime("%Y-%m-%d")
    try:
        filename = url.split("/")[-1]
        possible = filename[:10]
        datetime.strptime(possible, "%Y-%m-%d")
        extracted_date = possible
    except Exception as e:
        logging.warning(f"Could not extract date from filename, using today: {e}")

    logging.info(f"CSV URL: {url}  (date: {extracted_date})")
    return url, extracted_date


def download_csv(url: str) -> pd.DataFrame:
    """Download the gov.uk CSV into a DataFrame."""
    logging.info("Downloading CSV...")
    response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return pd.read_csv(io.BytesIO(response.content))


# ---------------------------------------------------------------- normalise
def normalise_today(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Apply normalisation + identity to today's CSV. Returns (norm_df, parse_failures)."""
    df.columns = df.columns.str.strip()

    failures: list[dict] = []
    rows: list[dict] = []
    for idx, row in df.iterrows():
        type_rating_raw = row.get("Type & Rating", "")
        try:
            type_str, rating = parse_type_rating(type_rating_raw)
        except ValueError as e:
            failures.append(
                {
                    "row_index": int(idx),
                    "organisation_name": str(row.get("Organisation Name", "")),
                    "type_rating": str(type_rating_raw),
                    "error": str(e),
                }
            )
            continue

        name_raw = row.get("Organisation Name", "")
        town_raw = row.get("Town/City", "")
        county_raw = row.get("County", "")
        route_raw = row.get("Route", "")

        name_n = normalise_name(name_raw)
        town_n = normalise_town(town_raw)
        county_n = normalise_county(county_raw)
        route_n = normalise_route(route_raw)

        rows.append(
            {
                "licence_id": compute_licence_id(name_n, town_n, county_n, route_n, type_str),
                "loose_id": compute_loose_id(name_n, town_n, route_n, type_str),
                "organisation_name_raw": str(name_raw) if pd.notna(name_raw) else "",
                "organisation_name_norm": name_n,
                "town_raw": str(town_raw) if pd.notna(town_raw) else "",
                "town_norm": town_n,
                "county_raw": str(county_raw) if pd.notna(county_raw) else "",
                "county_norm": county_n,
                "route": route_n,
                "type": type_str,
                "current_rating": rating,
                "type_rating_raw": str(type_rating_raw),
            }
        )

    norm_df = pd.DataFrame(rows)
    # Dedupe today's CSV on licence_id (gov.uk occasionally publishes dupes).
    if not norm_df.empty:
        norm_df = norm_df.drop_duplicates(subset=["licence_id"], keep="first")
    return norm_df, failures


# ---------------------------------------------------------- two-phase match
def two_phase_match(
    today: pd.DataFrame, master_active: pd.DataFrame
) -> tuple[set, set, set, list[dict]]:
    """
    Match today's licence_ids against the currently-active master set.

    Returns:
        added_ids        -- in today, not in master_active (after loose fallback)
        gone_ids         -- in master_active, not in today (after loose fallback)
        present_ids      -- in both (using today's licence_id; for upgrade/downgrade)
        county_corrections -- list of {today_licence_id, master_licence_id, ...}
                              entries where the loose match absorbed a county change
    """
    today_strict = set(today["licence_id"])
    master_strict = set(master_active["licence_id"])

    strict_intersection = today_strict & master_strict
    today_unmatched = today_strict - master_strict
    master_unmatched = master_strict - today_strict

    if today.empty or master_active.empty or not today_unmatched or not master_unmatched:
        return today_unmatched, master_unmatched, strict_intersection, []

    # Loose-id maps for the remaining unmatched sides.
    today_unmatched_df = today[today["licence_id"].isin(today_unmatched)]
    master_unmatched_df = master_active[master_active["licence_id"].isin(master_unmatched)]

    # Master loose_id needs recomputing from its normalised fields.
    master_unmatched_df = master_unmatched_df.copy()
    master_unmatched_df["loose_id"] = master_unmatched_df.apply(
        lambda r: compute_loose_id(
            r["organisation_name_norm"], r["town_norm"], r["route"], r["type"]
        ),
        axis=1,
    )

    # Count how many candidates per loose_id on each side; only unique 1:1 pairs match.
    today_loose_counts = today_unmatched_df["loose_id"].value_counts()
    master_loose_counts = master_unmatched_df["loose_id"].value_counts()
    eligible_loose = set(today_loose_counts[today_loose_counts == 1].index) & set(
        master_loose_counts[master_loose_counts == 1].index
    )

    if not eligible_loose:
        return today_unmatched, master_unmatched, strict_intersection, []

    today_loose_to_lid = dict(
        zip(today_unmatched_df["loose_id"], today_unmatched_df["licence_id"])
    )
    master_loose_to_lid = dict(
        zip(master_unmatched_df["loose_id"], master_unmatched_df["licence_id"])
    )
    today_loose_to_county = dict(
        zip(today_unmatched_df["loose_id"], today_unmatched_df["county_norm"])
    )
    master_loose_to_county = dict(
        zip(master_unmatched_df["loose_id"], master_unmatched_df["county_norm"])
    )

    corrections = []
    matched_today_lids: set[str] = set()
    matched_master_lids: set[str] = set()
    for loose in eligible_loose:
        today_lid = today_loose_to_lid[loose]
        master_lid = master_loose_to_lid[loose]
        matched_today_lids.add(today_lid)
        matched_master_lids.add(master_lid)
        corrections.append(
            {
                "loose_id": loose,
                "old_licence_id": master_lid,
                "new_licence_id": today_lid,
                "old_county_norm": master_loose_to_county[loose],
                "new_county_norm": today_loose_to_county[loose],
            }
        )

    # Strict intersection plus loose-matched today licence_ids stay as "present"
    # (logically they refer to the same licence; we treat them under the today_lid).
    present_ids = strict_intersection | matched_today_lids
    added_ids = today_unmatched - matched_today_lids
    gone_ids = master_unmatched - matched_master_lids

    return added_ids, gone_ids, present_ids, corrections


# ------------------------------------------------------------ event helpers
def _event_record(
    file_date: str,
    event_type: str,
    today_row: pd.Series | None,
    master_row: pd.Series | None,
    from_rating: str | None,
    to_rating: str | None,
) -> dict:
    """Build an events.json record from whichever side has the data."""
    src = today_row if today_row is not None else master_row
    return {
        "date": file_date,
        "event_type": event_type,
        "licence_id": str(src["licence_id"]),
        "organisation_name": str(src.get("organisation_name_raw", "")),
        "town": str(src.get("town_raw", "")),
        "county": str(src.get("county_raw", "")),
        "route": str(src.get("route", "")),
        "type": str(src.get("type", "")),
        "from_rating": from_rating,
        "to_rating": to_rating,
    }


def detect_events(
    today: pd.DataFrame,
    master_active: pd.DataFrame,
    added_ids: set,
    gone_ids: set,
    present_ids: set,
    corrections: list[dict],
    file_date: str,
) -> list[dict]:
    """Produce the full list of churn events for this snapshot."""
    events: list[dict] = []

    if today.empty:
        return events

    today_by_lid = today.set_index("licence_id", drop=False)
    master_by_lid = (
        master_active.set_index("licence_id", drop=False) if not master_active.empty else None
    )

    # added
    for lid in added_ids:
        if lid not in today_by_lid.index:
            continue
        events.append(
            _event_record(
                file_date, "added", today_by_lid.loc[lid], None,
                from_rating=None, to_rating=str(today_by_lid.loc[lid]["current_rating"]),
            )
        )

    # gone
    if master_by_lid is not None:
        for lid in gone_ids:
            if lid not in master_by_lid.index:
                continue
            events.append(
                _event_record(
                    file_date, "gone", None, master_by_lid.loc[lid],
                    from_rating=str(master_by_lid.loc[lid].get("current_rating", "")),
                    to_rating=None,
                )
            )

    # upgraded / downgraded — for licences present in both sides
    correction_map = {c["new_licence_id"]: c["old_licence_id"] for c in corrections}
    if master_by_lid is not None:
        for today_lid in present_ids:
            if today_lid not in today_by_lid.index:
                continue
            master_lookup_lid = correction_map.get(today_lid, today_lid)
            if master_lookup_lid not in master_by_lid.index:
                continue
            new_row = today_by_lid.loc[today_lid]
            old_row = master_by_lid.loc[master_lookup_lid]
            new_rating = new_row["current_rating"]
            old_rating = old_row["current_rating"]
            if new_rating == old_rating:
                continue
            try:
                if rating_rank(new_rating) > rating_rank(old_rating):
                    etype = "upgraded"
                elif rating_rank(new_rating) < rating_rank(old_rating):
                    etype = "downgraded"
                else:
                    continue
            except ValueError as e:
                logging.warning(f"Skipping rating compare for {today_lid}: {e}")
                continue
            events.append(
                _event_record(
                    file_date, etype, new_row, old_row,
                    from_rating=str(old_rating), to_rating=str(new_rating),
                )
            )

    return events


# ----------------------------------------------------- partitioned events log
def _events_path_for(date_str: str) -> str:
    return os.path.join(EVENTS_DIR, f"events-{date_str[:7]}.json")


def append_events(events: list[dict], file_date: str) -> None:
    """Append events to the current month's file. Updates events-index.json."""
    if not events:
        logging.info("No events to append.")
        return
    os.makedirs(EVENTS_DIR, exist_ok=True)
    month_path = _events_path_for(file_date)
    existing: list[dict] = []
    if os.path.exists(month_path):
        try:
            with open(month_path) as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            logging.warning(f"Couldn't parse {month_path}; starting fresh.")
            existing = []

    # Dedupe within the file: same (date, event_type, licence_id) ⇒ one record.
    key = lambda e: (e.get("date"), e.get("event_type"), e.get("licence_id"))
    seen = {key(e) for e in existing}
    for e in events:
        if key(e) not in seen:
            existing.append(e)
            seen.add(key(e))

    existing.sort(key=lambda e: (e.get("date", ""), e.get("event_type", "")))
    with open(month_path, "w") as f:
        json.dump(existing, f, indent=2)
    logging.info(f"Appended {len(events)} events -> {month_path} (total {len(existing)})")

    _rebuild_events_index()


def _rebuild_events_index() -> None:
    """Rebuild events-index.json by scanning the events directory."""
    if not os.path.isdir(EVENTS_DIR):
        return
    index = []
    for fname in sorted(os.listdir(EVENTS_DIR)):
        if not fname.startswith("events-") or not fname.endswith(".json"):
            continue
        if fname == "events-index.json":
            continue
        path = os.path.join(EVENTS_DIR, fname)
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, list):
            continue
        dates = [e.get("date", "") for e in data if e.get("date")]
        index.append(
            {
                "month": fname.replace("events-", "").replace(".json", ""),
                "file": fname,
                "count": len(data),
                "date_min": min(dates) if dates else None,
                "date_max": max(dates) if dates else None,
            }
        )
    with open(EVENTS_INDEX, "w") as f:
        json.dump(index, f, indent=2)


# -------------------------------------------------------- master update
def update_master(
    master_df: pd.DataFrame,
    today: pd.DataFrame,
    added_ids: set,
    gone_ids: set,
    present_ids: set,
    corrections: list[dict],
    file_date: str,
) -> pd.DataFrame:
    """
    Apply today's diff to master_register.csv. Vectorised — no per-row loops
    over the master DataFrame.

    - Added licences get appended with status=active, first_seen=last_seen=file_date.
    - Gone licences get status=gone, removed_date=file_date.
    - Present licences update last_seen_date and (if rating changed) current_rating.
    - Loose-matched (county_corrections) licences: master row adopts today's
      licence_id + county_norm + county_raw, last_seen advances.
    """
    if not master_df.empty:
        master_df = master_df.copy()

    correction_map_new_to_old = {c["new_licence_id"]: c["old_licence_id"] for c in corrections}
    correction_map_old_to_new = {c["old_licence_id"]: c["new_licence_id"] for c in corrections}

    # 1) Mark gone licences (vectorised).
    if gone_ids and not master_df.empty:
        mask = master_df["licence_id"].isin(gone_ids) & (master_df["status"] == "active")
        master_df.loc[mask, "status"] = "gone"
        master_df.loc[mask, "removed_date"] = file_date

    # 2) Vectorised update for present licences.
    # Resolve "today_lid -> master_lookup_lid" en masse, then join on today's rows.
    if present_ids and not master_df.empty and not today.empty:
        # Build a DataFrame of (master_lookup_lid, today_lid) for every present licence.
        present_pairs = pd.DataFrame(
            [
                (correction_map_new_to_old.get(lid, lid), lid)
                for lid in present_ids
            ],
            columns=["master_lookup_lid", "today_lid"],
        )

        # Pull current_rating / type_rating_raw / county fields from today, indexed by today's licence_id.
        today_attrs = (
            today.set_index("licence_id")[
                ["current_rating", "type_rating_raw", "county_norm", "county_raw"]
            ]
            .rename(columns={
                "current_rating": "_new_rating",
                "type_rating_raw": "_new_tr_raw",
                "county_norm": "_new_county_norm",
                "county_raw": "_new_county_raw",
            })
        )
        present_pairs = present_pairs.merge(
            today_attrs, left_on="today_lid", right_index=True, how="left"
        )

        # Merge into master on master_lookup_lid so each master row gets its update payload.
        master_df = master_df.merge(
            present_pairs,
            left_on="licence_id",
            right_on="master_lookup_lid",
            how="left",
        )

        present_mask = master_df["master_lookup_lid"].notna()
        master_df.loc[present_mask, "last_seen_date"] = file_date
        # Update rating + raw string for every present licence.
        master_df.loc[present_mask, "current_rating"] = master_df.loc[present_mask, "_new_rating"]
        master_df.loc[present_mask, "type_rating_raw"] = master_df.loc[present_mask, "_new_tr_raw"]

        # For loose-matched rows, adopt today's licence_id and county fields.
        # (correction_map_old_to_new is keyed by the original master licence_id.)
        loose_mask = present_mask & master_df["licence_id"].isin(correction_map_old_to_new.keys())
        master_df.loc[loose_mask, "licence_id"] = master_df.loc[loose_mask, "today_lid"]
        master_df.loc[loose_mask, "county_norm"] = master_df.loc[loose_mask, "_new_county_norm"]
        master_df.loc[loose_mask, "county_raw"] = master_df.loc[loose_mask, "_new_county_raw"]

        # Drop helper columns.
        master_df = master_df.drop(
            columns=[
                "master_lookup_lid",
                "today_lid",
                "_new_rating",
                "_new_tr_raw",
                "_new_county_norm",
                "_new_county_raw",
            ]
        )

    # 3) Append added licences as new master rows (vectorised).
    if added_ids and not today.empty:
        added_rows = today[today["licence_id"].isin(added_ids)].copy()
        if not added_rows.empty:
            added_rows = added_rows[
                [
                    "licence_id",
                    "organisation_name_raw",
                    "organisation_name_norm",
                    "town_raw",
                    "town_norm",
                    "county_raw",
                    "county_norm",
                    "route",
                    "type",
                    "current_rating",
                    "type_rating_raw",
                ]
            ]
            added_rows = added_rows.assign(
                status="active",
                first_seen_date=file_date,
                last_seen_date=file_date,
                removed_date="",
            )
            master_df = pd.concat([master_df, added_rows], ignore_index=True)

    return master_df


# -------------------------------------------------------- rating-history summary
HISTORY_COLUMNS = (
    ("rating_change_count", "0"),
    ("first_rating_change_date", ""),
    ("last_rating_change_date", ""),
)


def ensure_history_columns(master_df: pd.DataFrame) -> pd.DataFrame:
    """Add the three rating-history summary columns if missing (forward-compat)."""
    for col, default in HISTORY_COLUMNS:
        if col not in master_df.columns:
            master_df[col] = default
    return master_df


def update_rating_summaries(
    master_df: pd.DataFrame, events: list[dict], file_date: str
) -> pd.DataFrame:
    """
    For each upgraded/downgraded event, bump the per-licence summary fields
    on master:
        rating_change_count       += 1
        first_rating_change_date  ← file_date if currently empty
        last_rating_change_date   ← file_date (always)
    """
    rating_events = [e for e in events if e["event_type"] in ("upgraded", "downgraded")]
    if not rating_events:
        return master_df

    changed_lids = list({e["licence_id"] for e in rating_events})
    mask = master_df["licence_id"].isin(changed_lids)
    if not mask.any():
        return master_df

    # Increment count (cast through int to be robust to string-loaded CSV values).
    current = pd.to_numeric(
        master_df.loc[mask, "rating_change_count"], errors="coerce"
    ).fillna(0).astype(int)
    master_df.loc[mask, "rating_change_count"] = (current + 1).astype(str)

    # First date — only fill if currently empty.
    first_col = master_df.loc[mask, "first_rating_change_date"].fillna("").astype(str)
    fill_first = mask.copy()
    fill_first.loc[mask] = (first_col == "")
    master_df.loc[fill_first, "first_rating_change_date"] = file_date

    # Last date — always overwrite with today's date for changed licences.
    master_df.loc[mask, "last_rating_change_date"] = file_date

    return master_df


# -------------------------------------------------------- output writers
def _bucket_events(events: list[dict]) -> dict[str, list[dict]]:
    buckets = defaultdict(list)
    for e in events:
        buckets[e["event_type"]].append(e)
    return buckets


def generate_daily_delta(events: list[dict], file_date: str) -> None:
    buckets = _bucket_events(events)
    delta = {
        "date": file_date,
        "added": buckets.get("added", []),
        "upgraded": buckets.get("upgraded", []),
        "downgraded": buckets.get("downgraded", []),
        "gone": buckets.get("gone", []),
        # Backward-compat alias for legacy consumers:
        "removed": buckets.get("downgraded", []) + buckets.get("gone", []),
    }
    with open(DELTA_FILE, "w") as f:
        json.dump(delta, f, indent=2)
    logging.info(f"Wrote {DELTA_FILE}")


def generate_stats(master_df: pd.DataFrame, events: list[dict], file_date: str) -> None:
    active = master_df[master_df["status"] == "active"].copy()
    buckets = _bucket_events(events)

    added_count = len(buckets.get("added", []))
    upg_count = len(buckets.get("upgraded", []))
    dwn_count = len(buckets.get("downgraded", []))
    gone_count = len(buckets.get("gone", []))

    # Recency: read events index + recent months
    recent_events = _load_recent_events(file_date, days=14)
    recent_added_7 = [
        e for e in recent_events
        if e["event_type"] == "added" and _within_days(e["date"], file_date, 7)
    ][:1000]
    recent_removed_14 = [  # legacy: downgraded + gone over 14 days
        e for e in recent_events
        if e["event_type"] in ("downgraded", "gone")
        and _within_days(e["date"], file_date, 14)
    ]
    recent_downgraded_14 = [e for e in recent_events if e["event_type"] == "downgraded"
                            and _within_days(e["date"], file_date, 14)]
    recent_upgraded_14 = [e for e in recent_events if e["event_type"] == "upgraded"
                          and _within_days(e["date"], file_date, 14)]
    recent_gone_14 = [e for e in recent_events if e["event_type"] == "gone"
                      and _within_days(e["date"], file_date, 14)]

    def _trim(rec_list: list[dict]) -> list[dict]:
        return [
            {
                "Organisation Name": e["organisation_name"],
                "Town/City": e["town"],
                "Route": e["route"],
                "from_rating": e.get("from_rating"),
                "to_rating": e.get("to_rating"),
                "date": e["date"],
            }
            for e in rec_list
        ]

    stats = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "data_date": file_date,
        "daily_metrics": {
            "added_today": added_count,
            "upgraded_today": upg_count,
            "downgraded_today": dwn_count,
            "gone_today": gone_count,
            # Legacy alias (kept one release):
            "removed_today": dwn_count + gone_count,
            "total_active_sponsors": int(len(active)),
        },
        "categorical_totals": {
            "unique_organisations": int(active["organisation_name_norm"].nunique()) if not active.empty else 0,
            "unique_cities": int(active["town_norm"].nunique()) if not active.empty else 0,
            "unique_routes": int(active["route"].nunique()) if not active.empty else 0,
        },
        "rankings": {
            "top_routes": active["route"].value_counts().head(5).to_dict() if not active.empty else {},
            "top_cities": active["town_raw"].value_counts().head(5).to_dict() if not active.empty else {},
            "top_ratings": active["current_rating"].value_counts().head(5).to_dict() if not active.empty else {},
            "top_types": active["type"].value_counts().to_dict() if not active.empty else {},
        },
        "recency": {
            "added_last_7_days": _trim(recent_added_7),
            "removed_last_14_days": _trim(recent_removed_14),
            "upgraded_last_14_days": _trim(recent_upgraded_14),
            "downgraded_last_14_days": _trim(recent_downgraded_14),
            "gone_last_14_days": _trim(recent_gone_14),
        },
    }
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)
    logging.info(f"Wrote {STATS_FILE}")


def update_history(events: list[dict], master_df: pd.DataFrame, file_date: str) -> None:
    buckets = _bucket_events(events)
    added_count = len(buckets.get("added", []))
    upg_count = len(buckets.get("upgraded", []))
    dwn_count = len(buckets.get("downgraded", []))
    gone_count = len(buckets.get("gone", []))
    total_active = int((master_df["status"] == "active").sum())

    history: list[dict] = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except json.JSONDecodeError:
            history = []
    if not isinstance(history, list):
        history = []

    new_entry = {
        "date": file_date,
        "added": added_count,
        "upgraded": upg_count,
        "downgraded": dwn_count,
        "gone": gone_count,
        "removed": dwn_count + gone_count,  # legacy alias
        "total": total_active,
    }
    existing = next((h for h in history if h.get("date") == file_date), None)
    if existing:
        existing.update(new_entry)
    else:
        history.append(new_entry)
    history.sort(key=lambda h: h.get("date", ""))

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)
    logging.info(f"Wrote {HISTORY_FILE}")


def _within_days(date_str: str, ref_date: str, days: int) -> bool:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        r = datetime.strptime(ref_date, "%Y-%m-%d")
        return 0 <= (r - d).days <= days
    except Exception:
        return False


def _load_recent_events(file_date: str, days: int) -> list[dict]:
    """Load events from this month and the previous month (covers any 14-day window)."""
    if not os.path.isdir(EVENTS_DIR):
        return []
    results: list[dict] = []
    # Current month + previous month
    try:
        ref = datetime.strptime(file_date, "%Y-%m-%d")
    except Exception:
        return []
    months_to_load = {ref.strftime("%Y-%m")}
    prev_month = (ref.replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m")
    months_to_load.add(prev_month)
    for month in months_to_load:
        path = os.path.join(EVENTS_DIR, f"events-{month}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                results.extend(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    # Keep events within (file_date - days, file_date].
    return [e for e in results if _within_days(e.get("date", ""), file_date, days)]


# ----------------------------------------------------- false-churn detector
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            dele = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            curr.append(min(ins, dele, sub))
        prev = curr
    return prev[-1]


def detect_false_churn(events: list[dict], file_date: str) -> None:
    """
    Daily diagnostic — find suspected false add/gone pairs.

    Scoping: same (town_norm-ish, route, type) AND Levenshtein(name) <= threshold.
    Without that scope, two unrelated companies with similar names would pair.
    Note: town here is the raw value (we don't keep norm in the event record);
    that's slightly less robust but good enough as an alert signal.
    """
    recent = _load_recent_events(file_date, days=FALSE_CHURN_DAY_WINDOW * 2)
    # Limit to events within ±N days of the current run date
    in_window = [
        e for e in recent if _within_days(e.get("date", ""), file_date, FALSE_CHURN_DAY_WINDOW * 2)
    ]
    gone = [e for e in in_window if e["event_type"] == "gone"]
    added = [e for e in in_window if e["event_type"] == "added"]
    suspects: list[dict] = []

    for g in gone:
        for a in added:
            if g["licence_id"] == a["licence_id"]:
                continue
            if (g.get("town", "").strip().lower() != a.get("town", "").strip().lower()
                    or g.get("route") != a.get("route")
                    or g.get("type") != a.get("type")):
                continue
            try:
                gd = datetime.strptime(g["date"], "%Y-%m-%d")
                ad = datetime.strptime(a["date"], "%Y-%m-%d")
            except Exception:
                continue
            if abs((ad - gd).days) > FALSE_CHURN_DAY_WINDOW:
                continue
            dist = _levenshtein(
                g.get("organisation_name", "").lower(),
                a.get("organisation_name", "").lower(),
            )
            if dist <= LEVENSHTEIN_THRESHOLD:
                suspects.append(
                    {
                        "name_distance": dist,
                        "gone": g,
                        "added": a,
                    }
                )

    if suspects:
        with open(FALSE_CHURN_FILE, "w") as f:
            json.dump(suspects, f, indent=2)
        logging.info(f"Wrote {FALSE_CHURN_FILE} ({len(suspects)} suspected pairs)")
    elif os.path.exists(FALSE_CHURN_FILE):
        os.remove(FALSE_CHURN_FILE)


# ---------------------------------------------------- ancillary writers
def write_county_corrections(corrections: list[dict], file_date: str) -> None:
    if not corrections:
        return
    log: list[dict] = []
    if os.path.exists(COUNTY_CORRECTIONS_FILE):
        try:
            with open(COUNTY_CORRECTIONS_FILE) as f:
                log = json.load(f)
        except json.JSONDecodeError:
            log = []
    for c in corrections:
        log.append({"date": file_date, **c})
    with open(COUNTY_CORRECTIONS_FILE, "w") as f:
        json.dump(log, f, indent=2)
    logging.info(f"Wrote {COUNTY_CORRECTIONS_FILE} (+{len(corrections)})")


def write_parse_failures(failures: list[dict], file_date: str) -> None:
    if not failures:
        if os.path.exists(PARSE_FAILURES_FILE):
            os.remove(PARSE_FAILURES_FILE)
        return
    payload = {"date": file_date, "count": len(failures), "failures": failures}
    with open(PARSE_FAILURES_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    logging.warning(f"Wrote {PARSE_FAILURES_FILE} (count={len(failures)})")


# ----------------------------------------------------------- main
def main() -> int:
    try:
        csv_url, file_date = get_csv_url()
        today_raw = download_csv(csv_url)
        logging.info(f"Downloaded {len(today_raw)} rows for {file_date}")

        today_norm, parse_failures = normalise_today(today_raw)
        write_parse_failures(parse_failures, file_date)
        logging.info(
            f"Normalised {len(today_norm)} unique licences "
            f"(parse failures: {len(parse_failures)})"
        )

        if not os.path.exists(MASTER_FILE):
            raise RuntimeError(
                f"{MASTER_FILE} not found. Run migrate_master.py before the first tracker run."
            )
        master_df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
        master_df = ensure_history_columns(master_df)
        master_active = master_df[master_df["status"] == "active"]
        logging.info(f"Loaded master ({len(master_df)} licences, {len(master_active)} active)")

        added_ids, gone_ids, present_ids, corrections = two_phase_match(
            today_norm, master_active
        )
        write_county_corrections(corrections, file_date)
        logging.info(
            f"Match: +{len(added_ids)} added, -{len(gone_ids)} gone, "
            f"={len(present_ids)} present, county_corrections={len(corrections)}"
        )

        events = detect_events(
            today_norm, master_active, added_ids, gone_ids, present_ids, corrections, file_date
        )
        bucket = _bucket_events(events)
        logging.info(
            f"Events: added={len(bucket.get('added', []))}, "
            f"upgraded={len(bucket.get('upgraded', []))}, "
            f"downgraded={len(bucket.get('downgraded', []))}, "
            f"gone={len(bucket.get('gone', []))}"
        )

        new_master = update_master(
            master_df, today_norm, added_ids, gone_ids, present_ids, corrections, file_date
        )
        # Bump rating-history summary columns for any upgraded/downgraded events.
        new_master = update_rating_summaries(new_master, events, file_date)
        new_master.to_csv(MASTER_FILE, index=False)
        logging.info(f"Wrote {MASTER_FILE} ({len(new_master)} licences)")

        append_events(events, file_date)
        # Incrementally update the per-licence month index so the dashboard's
        # per-licence history view can find this licence's months without
        # scanning every events file.
        update_licence_month_index_for(events, EVENTS_DIR)
        generate_daily_delta(events, file_date)
        generate_stats(new_master, events, file_date)
        update_history(events, new_master, file_date)
        detect_false_churn(events, file_date)

        logging.info("Tracker run completed.")
        return 0
    except Exception as e:
        logging.exception(f"Tracker failed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
