"""
One-shot replay: rebuild events.json from historical gov.uk CSV snapshots.

Reads every CSV in `historical/` (or a directory given on the command line),
sorts them by their embedded date, and runs the same diff logic that tracker.py
uses against an in-memory pseudo-master. Each transition emits events dated at
the snapshot date.

Outputs:
  events/events-YYYY-MM.json + events/events-index.json   (overwritten)
  replay_report.json                                       (summary stats)

Does NOT touch master_register.csv. The migrated master already holds the
current state; replay's job is to populate the historical event trail.

Usage:
  python replay.py                       # reads historical/, default behaviour
  python replay.py --dir my-historical/  # different source directory
  python replay.py --emit-baseline       # ALSO emit 'added' events for the
                                         # very first snapshot. Default skips
                                         # them (would be ~140k spammy events).

Historical filename convention (matches gov.uk):
    YYYY-MM-DD_-_Worker_and_Temporary_Worker.csv
The leading 10 chars must parse as YYYY-MM-DD.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
from datetime import datetime

import pandas as pd

from tracker import (
    EVENTS_DIR,
    MASTER_FILE,
    _bucket_events,
    _rebuild_events_index,
    detect_events,
    ensure_history_columns,
    normalise_today,
    two_phase_match,
    update_master,
)
from licence_history import (
    build_licence_month_index,
    recompute_rating_summary,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

DEFAULT_DIR = "historical"
REPORT_FILE = "replay_report.json"

# YYYY-MM-DD prefix on filenames; matches gov.uk's naming convention.
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _discover_csvs(directory: str) -> list[tuple[str, str]]:
    """Return list of (date, path) tuples sorted by date."""
    if not os.path.isdir(directory):
        logging.warning(f"Directory {directory!r} does not exist.")
        return []
    out = []
    for path in glob.glob(os.path.join(directory, "*.csv")):
        fname = os.path.basename(path)
        m = DATE_RE.match(fname)
        if not m:
            logging.warning(f"Skipping {fname} (no YYYY-MM-DD prefix)")
            continue
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            logging.warning(f"Skipping {fname} (date doesn't parse)")
            continue
        out.append((m.group(1), path))
    out.sort(key=lambda t: t[0])
    return out


def _empty_master() -> pd.DataFrame:
    """In-memory pseudo-master with the new schema, starting empty."""
    return pd.DataFrame(
        columns=[
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
            "status",
            "first_seen_date",
            "last_seen_date",
            "removed_date",
        ]
    )


def _wipe_events_dir() -> None:
    if not os.path.isdir(EVENTS_DIR):
        return
    for fname in os.listdir(EVENTS_DIR):
        if fname.startswith("events-") and fname.endswith(".json"):
            os.remove(os.path.join(EVENTS_DIR, fname))


def _append_events_grouped(events_by_month: dict[str, list[dict]]) -> None:
    os.makedirs(EVENTS_DIR, exist_ok=True)
    for month, events in events_by_month.items():
        path = os.path.join(EVENTS_DIR, f"events-{month}.json")
        # Deterministic order matching tracker.append_events.
        events.sort(key=lambda e: (e.get("date", ""), e.get("event_type", ""), e.get("licence_id", "")))
        with open(path, "w") as f:
            json.dump(events, f, indent=2)
        logging.info(f"Wrote {path} ({len(events)} events)")


def replay(
    directory: str = DEFAULT_DIR,
    emit_baseline: bool = False,
) -> dict:
    csvs = _discover_csvs(directory)
    if not csvs:
        raise RuntimeError(
            f"No usable CSVs found in {directory!r}. "
            f"Expected filenames like '2026-01-15_-_Worker_and_Temporary_Worker.csv'."
        )

    logging.info(f"Found {len(csvs)} historical snapshots in {directory!r}:")
    for date_str, path in csvs:
        logging.info(f"  {date_str}  ({os.path.basename(path)})")

    pseudo_master = _empty_master()
    events_by_month: dict[str, list[dict]] = {}
    per_snapshot: list[dict] = []

    _wipe_events_dir()

    for i, (date_str, path) in enumerate(csvs):
        is_baseline = i == 0
        logging.info(
            f"[{i+1}/{len(csvs)}] {date_str} — {'baseline' if is_baseline else 'diff'}"
        )

        # Normalise today's snapshot
        raw_df = pd.read_csv(path)
        norm_df, parse_failures = normalise_today(raw_df)
        if parse_failures:
            logging.warning(
                f"  {len(parse_failures)} parse failures in {os.path.basename(path)}"
            )

        # Diff against current pseudo-master active set
        active = pseudo_master[pseudo_master["status"] == "active"]
        added_ids, gone_ids, present_ids, corrections = two_phase_match(norm_df, active)

        # Detect events at this snapshot's date
        events: list[dict] = []
        if not is_baseline or emit_baseline:
            events = detect_events(
                norm_df, active, added_ids, gone_ids, present_ids, corrections, date_str
            )

        # Group events by month for partitioned writing later
        for e in events:
            month = (e["date"] or "")[:7] or date_str[:7]
            events_by_month.setdefault(month, []).append(e)

        # Apply to pseudo-master (whether or not we emitted events)
        pseudo_master = update_master(
            pseudo_master, norm_df, added_ids, gone_ids, present_ids, corrections, date_str
        )

        bucket = _bucket_events(events)
        per_snapshot.append(
            {
                "date": date_str,
                "baseline": is_baseline,
                "snapshot_rows": len(norm_df),
                "added": len(bucket.get("added", [])),
                "upgraded": len(bucket.get("upgraded", [])),
                "downgraded": len(bucket.get("downgraded", [])),
                "gone": len(bucket.get("gone", [])),
                "county_corrections": len(corrections),
            }
        )

    _append_events_grouped(events_by_month)
    _rebuild_events_index()

    # Build the per-licence month index from the newly populated events/.
    # The dashboard uses this to fetch only relevant month files per licence.
    licence_month_index = build_licence_month_index(EVENTS_DIR)

    # Recompute rating-history summaries on the REAL master_register.csv from
    # the events we just emitted. (We don't touch identity/state on master —
    # that's already current from migration. Only the three summary columns.)
    summaries_updated = False
    if os.path.exists(MASTER_FILE):
        real_master = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
        real_master = ensure_history_columns(real_master)
        real_master = recompute_rating_summary(real_master, EVENTS_DIR)
        real_master.to_csv(MASTER_FILE, index=False)
        summaries_updated = True
        logging.info(f"Refreshed rating-history summaries on {MASTER_FILE}")

    final_active = (pseudo_master["status"] == "active").sum()
    report = {
        "directory": directory,
        "snapshots_processed": len(csvs),
        "first_date": csvs[0][0],
        "last_date": csvs[-1][0],
        "pseudo_master_final_licences": len(pseudo_master),
        "pseudo_master_final_active": int(final_active),
        "events_total": sum(len(v) for v in events_by_month.values()),
        "events_per_type_total": {
            etype: sum(
                1 for evs in events_by_month.values() for e in evs if e["event_type"] == etype
            )
            for etype in ("added", "upgraded", "downgraded", "gone")
        },
        "per_snapshot": per_snapshot,
        "baseline_skipped": not emit_baseline,
        "licences_in_month_index": len(licence_month_index),
        "master_summaries_updated": summaries_updated,
    }
    with open(REPORT_FILE, "w") as f:
        json.dump(report, f, indent=2)
    logging.info(f"Wrote {REPORT_FILE}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", default=DEFAULT_DIR, help="Directory of historical CSVs"
    )
    parser.add_argument(
        "--emit-baseline",
        action="store_true",
        help="Also emit 'added' events for the very first snapshot "
             "(default: skip — would be ~140k spammy events).",
    )
    args = parser.parse_args(argv)
    try:
        replay(directory=args.dir, emit_baseline=args.emit_baseline)
    except Exception as e:
        logging.exception(f"Replay failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
