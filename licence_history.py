"""
Rating-history utilities — derive per-licence churn summaries and indexes
from the events/ partitioned log.

Three exports:

    recompute_rating_summary(master_df, events_dir) -> master_df
        For each licence_id in master, count its upgraded/downgraded events
        across all events/events-YYYY-MM.json files and populate:
            rating_change_count
            first_rating_change_date
            last_rating_change_date

    build_licence_month_index(events_dir) -> dict
        Scan all events files; return a {licence_id: sorted list of months}
        mapping. Also writes to events/licence-month-index.json.

    get_licence_events(licence_id, events_dir, index=None) -> list[dict]
        Fetch all events for one licence, sorted by date. Uses the month
        index if supplied (otherwise scans every file).

These are CACHES of the data that already lives in events/. They are safe
to rebuild from scratch at any time — the events log is the source of truth.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Iterable

import pandas as pd

EVENTS_DIR_DEFAULT = "events"
LICENCE_MONTH_INDEX_FILE = "licence-month-index.json"

# Only these event types contribute to "rating change" counts/dates.
RATING_EVENTS = ("upgraded", "downgraded")


def _iter_event_files(events_dir: str) -> Iterable[tuple[str, str]]:
    """Yield (month, file_path) for every events-YYYY-MM.json in the dir."""
    if not os.path.isdir(events_dir):
        return
    for fname in sorted(os.listdir(events_dir)):
        if not fname.startswith("events-") or not fname.endswith(".json"):
            continue
        if fname == "events-index.json" or fname == LICENCE_MONTH_INDEX_FILE:
            continue
        month = fname.replace("events-", "").replace(".json", "")
        yield month, os.path.join(events_dir, fname)


def _load_events_file(path: str) -> list[dict]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        logging.warning(f"Couldn't load {path}; treating as empty.")
        return []


# ---------------------------------------------------------------------------
# Summary recomputation
# ---------------------------------------------------------------------------


def recompute_rating_summary(
    master_df: pd.DataFrame, events_dir: str = EVENTS_DIR_DEFAULT
) -> pd.DataFrame:
    """
    Recompute rating_change_count / first_rating_change_date / last_rating_change_date
    columns on master_df from the events/ partitioned log.

    Pure function: returns a new DataFrame, doesn't mutate the input.
    """
    df = master_df.copy()

    # Initialise columns if missing.
    for col, default in (
        ("rating_change_count", 0),
        ("first_rating_change_date", ""),
        ("last_rating_change_date", ""),
    ):
        if col not in df.columns:
            df[col] = default

    counts: dict[str, int] = defaultdict(int)
    first_dates: dict[str, str] = {}
    last_dates: dict[str, str] = {}

    for _month, path in _iter_event_files(events_dir):
        for event in _load_events_file(path):
            etype = event.get("event_type")
            if etype not in RATING_EVENTS:
                continue
            lid = event.get("licence_id")
            date = event.get("date")
            if not lid or not date:
                continue
            counts[lid] += 1
            if lid not in first_dates or date < first_dates[lid]:
                first_dates[lid] = date
            if lid not in last_dates or date > last_dates[lid]:
                last_dates[lid] = date

    # Vectorised application via map.
    df["rating_change_count"] = df["licence_id"].map(counts).fillna(0).astype(int)
    df["first_rating_change_date"] = df["licence_id"].map(first_dates).fillna("")
    df["last_rating_change_date"] = df["licence_id"].map(last_dates).fillna("")

    n_with_history = (df["rating_change_count"] > 0).sum()
    logging.info(
        f"Rating summary recomputed: {n_with_history} licences have history "
        f"(total {sum(counts.values())} change events)."
    )
    return df


# ---------------------------------------------------------------------------
# Per-licence month index
# ---------------------------------------------------------------------------


def build_licence_month_index(
    events_dir: str = EVENTS_DIR_DEFAULT,
    write_to_file: bool = True,
) -> dict[str, list[str]]:
    """
    Scan all events files; return {licence_id: [month, month, ...]} mapping.

    A licence appears in a month's list iff it has at least one event that
    month. Months are unique-sorted. Used by the dashboard to fetch only
    relevant month files when displaying a single licence's history.
    """
    index: dict[str, set] = defaultdict(set)
    for month, path in _iter_event_files(events_dir):
        for event in _load_events_file(path):
            lid = event.get("licence_id")
            if lid:
                index[lid].add(month)

    out = {lid: sorted(months) for lid, months in index.items()}

    if write_to_file:
        os.makedirs(events_dir, exist_ok=True)
        path = os.path.join(events_dir, LICENCE_MONTH_INDEX_FILE)
        with open(path, "w") as f:
            json.dump(out, f, separators=(",", ":"))  # compact
        logging.info(
            f"Wrote licence-month index -> {path} "
            f"({len(out)} licences with events)"
        )
    return out


def update_licence_month_index_for(
    new_events: list[dict],
    events_dir: str = EVENTS_DIR_DEFAULT,
) -> None:
    """
    Incrementally update licence-month-index.json with new events from one
    tracker run. Cheaper than rebuilding the whole index.
    """
    if not new_events:
        return
    path = os.path.join(events_dir, LICENCE_MONTH_INDEX_FILE)
    index: dict[str, list[str]] = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                index = json.load(f)
        except (OSError, json.JSONDecodeError):
            index = {}

    changed = False
    for e in new_events:
        lid = e.get("licence_id")
        date = e.get("date", "")
        if not lid or not date:
            continue
        month = date[:7]
        months = set(index.get(lid, []))
        if month not in months:
            months.add(month)
            index[lid] = sorted(months)
            changed = True

    if changed:
        os.makedirs(events_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(index, f, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Per-licence history retrieval (used by ad-hoc lookups, replay reports, etc.)
# ---------------------------------------------------------------------------


def get_licence_events(
    licence_id: str,
    events_dir: str = EVENTS_DIR_DEFAULT,
    index: dict[str, list[str]] | None = None,
) -> list[dict]:
    """
    Return all events for one licence_id, sorted ascending by date.

    If `index` is provided (or licence-month-index.json exists on disk), only
    the relevant month files are scanned. Otherwise every events file is read.
    """
    if index is None:
        index_path = os.path.join(events_dir, LICENCE_MONTH_INDEX_FILE)
        if os.path.exists(index_path):
            try:
                with open(index_path) as f:
                    index = json.load(f)
            except (OSError, json.JSONDecodeError):
                index = None

    months_to_scan: list[str]
    if index is not None:
        months_to_scan = index.get(licence_id, [])
        if not months_to_scan:
            return []
    else:
        months_to_scan = [m for m, _ in _iter_event_files(events_dir)]

    results = []
    for month in months_to_scan:
        path = os.path.join(events_dir, f"events-{month}.json")
        if not os.path.exists(path):
            continue
        for e in _load_events_file(path):
            if e.get("licence_id") == licence_id:
                results.append(e)

    results.sort(key=lambda e: e.get("date", ""))
    return results
