"""
One-shot importer: merges the user-supplied Desktop JSON snapshots into the
event log + history + master register, then redistributes the over-lumped
replay events on 2026-05-08 back across 2026-03-06 → 2026-05-07.

Desktop files (~/OneDrive/Desktop/):
  history.json       — 44 daily entries (old schema: added/removed/total)
  daily_delta.json   — 2026-05-08 only, ~113 licence-level rows
  stats.json         — recency lists at 2026-05-08 (informational; not used here)

Approach (per the approved plan):
  1. Load master_register.csv to resolve Desktop rows to real licence_ids.
  2. Parse Desktop daily_delta rows → events for 2026-05-08, two-phase loose
     matching to find the master licence_id (Desktop rows have no County).
  3. Take the OLD events on 2026-05-08 in events/events-2026-05.json. Subtract
     out Desktop's contribution (the licences we have real-daily detail for).
     The rest is the lump to redistribute.
  4. Compute redistribution weights per date from Desktop history.json.
  5. Weighted-random reassign each lumped event a new date and write to the
     appropriate month partition.
  6. Merge Desktop history.json daily entries into our history.json.
  7. Rebuild licence-month-index + rating summaries + history.json.

Deterministic random seed (random.seed(20260511)) so repeated runs produce
identical output.
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from licence_history import (
    build_licence_month_index,
    rebuild_history_from_events,
    recompute_rating_summary,
)
from normalisation import (
    compute_licence_id,
    compute_loose_id,
    normalise_name,
    normalise_route,
    normalise_town,
    parse_type_rating,
)
from tracker import (
    EVENTS_DIR,
    HISTORY_FILE,
    MASTER_FILE,
    PARSE_FAILURES_FILE,
    ensure_history_columns,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

DESKTOP_DIR = Path.home() / "OneDrive" / "Desktop"
DESKTOP_HISTORY = DESKTOP_DIR / "history.json"
DESKTOP_DELTA = DESKTOP_DIR / "daily_delta.json"
DESKTOP_STATS = DESKTOP_DIR / "stats.json"

ANCHOR_DATE = "2026-05-08"      # date the lump currently sits on
WINDOW_START = "2026-03-06"     # earliest date in Desktop history (post the jump)
WINDOW_END = "2026-05-07"       # day before the anchor; these are the redistribution targets

RANDOM_SEED = 20260511

REPORT_FILE = "import_desktop_report.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str | Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _build_master_loose_index(
    master_df: pd.DataFrame, statuses: tuple[str, ...] = ("active",)
) -> dict[str, list[dict]]:
    """
    Map loose_id (name|town|route|type) → list of master rows that match.

    Caller picks which `statuses` to include:
      - For `added` events: active rows (the licence is currently licensed).
      - For `gone` events: gone rows (the licence was just removed).
    """
    subset = master_df[master_df["status"].isin(statuses)]
    loose_index: dict[str, list[dict]] = defaultdict(list)
    for _, row in subset.iterrows():
        loose_id = compute_loose_id(
            row["organisation_name_norm"],
            row["town_norm"],
            row["route"],
            row["type"],
        )
        loose_index[loose_id].append(
            {
                "licence_id": row["licence_id"],
                "county_norm": row["county_norm"],
                "county_raw": row["county_raw"],
                "organisation_name_raw": row["organisation_name_raw"],
                "town_raw": row["town_raw"],
                "status": row["status"],
                "removed_date": row.get("removed_date", ""),
            }
        )
    return loose_index


def _desktop_row_to_event(
    row: dict,
    event_type: str,
    file_date: str,
    loose_index: dict[str, list[dict]],
    parse_failures: list[dict],
) -> dict | None:
    """
    Convert a Desktop daily_delta row to a new-schema event.

    Returns None and appends to parse_failures if:
      - Type & Rating doesn't parse
      - Loose-match gives zero or >1 candidates
    """
    type_rating_raw = row.get("Type & Rating", "")
    try:
        type_str, rating, service = parse_type_rating(type_rating_raw)
    except ValueError as e:
        parse_failures.append(
            {
                "source": "daily_delta",
                "event_type": event_type,
                "organisation_name": row.get("Organisation Name", ""),
                "type_rating": str(type_rating_raw),
                "error": f"parse_type_rating: {e}",
            }
        )
        return None

    name_raw = row.get("Organisation Name", "")
    town_raw = row.get("Town/City", "")
    route_raw = row.get("Route", "")

    name_n = normalise_name(name_raw)
    town_n = normalise_town(town_raw)
    route_n = normalise_route(route_raw)

    loose = compute_loose_id(name_n, town_n, route_n, type_str)
    candidates = loose_index.get(loose, [])
    if len(candidates) != 1:
        parse_failures.append(
            {
                "source": "daily_delta",
                "event_type": event_type,
                "organisation_name": name_raw,
                "loose_id": loose,
                "match_count": len(candidates),
                "error": "loose-match ambiguous" if len(candidates) > 1 else "loose-match missing",
            }
        )
        return None

    m = candidates[0]
    event = {
        "date": file_date,
        "event_type": event_type,
        "licence_id": m["licence_id"],
        "organisation_name": m["organisation_name_raw"] or name_raw,
        "town": m["town_raw"] or town_raw,
        "county": m["county_raw"] or "",
        "route": route_n,
        "type": type_str,
    }
    if event_type == "added":
        event["from_rating"] = None
        event["to_rating"] = rating
    else:  # gone
        event["from_rating"] = rating
        event["to_rating"] = None
    return event


# ---------------------------------------------------------------------------
# Main importer
# ---------------------------------------------------------------------------


def run() -> dict:
    if not DESKTOP_HISTORY.exists():
        raise FileNotFoundError(f"Missing {DESKTOP_HISTORY}")
    if not DESKTOP_DELTA.exists():
        raise FileNotFoundError(f"Missing {DESKTOP_DELTA}")

    random.seed(RANDOM_SEED)

    desktop_history = _read_json(DESKTOP_HISTORY)
    desktop_delta = _read_json(DESKTOP_DELTA)
    logging.info(
        f"Loaded Desktop: history={len(desktop_history)} entries, "
        f"delta={len(desktop_delta.get('added', []))}+{len(desktop_delta.get('removed', []))} licence rows"
    )

    master_df = pd.read_csv(MASTER_FILE, dtype=str).fillna("")
    master_df = ensure_history_columns(master_df)

    # Two separate loose indexes:
    #   active_index for `added` rows (licence is currently licensed)
    #   gone_index   for `gone`  rows (licence was just removed; master shows status=gone)
    active_loose_index = _build_master_loose_index(master_df, statuses=("active",))
    gone_loose_index = _build_master_loose_index(master_df, statuses=("gone",))
    logging.info(
        f"Built loose indexes: active={sum(len(v) for v in active_loose_index.values())} rows, "
        f"gone={sum(len(v) for v in gone_loose_index.values())} rows"
    )

    parse_failures: list[dict] = []

    # ---------------------------------------------------------------------
    # Step 1: convert Desktop daily_delta → real events for 2026-05-08
    # ---------------------------------------------------------------------
    desktop_added_events: list[dict] = []
    desktop_gone_events: list[dict] = []
    for r in desktop_delta.get("added", []):
        e = _desktop_row_to_event(r, "added", ANCHOR_DATE, active_loose_index, parse_failures)
        if e:
            desktop_added_events.append(e)
    for r in desktop_delta.get("removed", []):
        e = _desktop_row_to_event(r, "gone", ANCHOR_DATE, gone_loose_index, parse_failures)
        if e:
            desktop_gone_events.append(e)
    logging.info(
        f"Converted Desktop daily_delta: {len(desktop_added_events)} added + "
        f"{len(desktop_gone_events)} gone events for {ANCHOR_DATE}"
    )
    logging.info(f"  parse_failures so far: {len(parse_failures)}")

    desktop_licence_ids_added = {e["licence_id"] for e in desktop_added_events}
    desktop_licence_ids_gone = {e["licence_id"] for e in desktop_gone_events}

    # ---------------------------------------------------------------------
    # Step 2: separate existing events on 2026-05-08 into "Desktop-covered"
    # and "lump-to-redistribute"
    # ---------------------------------------------------------------------
    events_05_path = os.path.join(EVENTS_DIR, "events-2026-05.json")
    existing_2026_05 = _read_json(Path(events_05_path)) if os.path.exists(events_05_path) else []
    # Keep only events on dates other than ANCHOR_DATE (none expected — should be all 05-08)
    other_dates_05 = [e for e in existing_2026_05 if e.get("date") != ANCHOR_DATE]
    on_anchor = [e for e in existing_2026_05 if e.get("date") == ANCHOR_DATE]

    # The lump: existing on-anchor events MINUS those whose licence_id Desktop already covers
    lump: list[dict] = []
    for e in on_anchor:
        et = e.get("event_type")
        lid = e.get("licence_id")
        if et == "added" and lid in desktop_licence_ids_added:
            continue  # superseded by Desktop's real-daily version
        if et == "gone" and lid in desktop_licence_ids_gone:
            continue
        # All upgraded/downgraded and any unmatched added/gone go to redistribution.
        lump.append(e)
    logging.info(
        f"Anchor-date events: kept {len(on_anchor) - len(lump)} (Desktop-superseded), "
        f"redistributing {len(lump)}"
    )

    # ---------------------------------------------------------------------
    # Step 3: build redistribution weights from Desktop history
    # ---------------------------------------------------------------------
    # window dates and their gross daily counts from Desktop history
    desktop_by_date = {h["date"]: h for h in desktop_history if WINDOW_START <= h["date"] <= WINDOW_END}
    window_dates = sorted(desktop_by_date.keys())
    if not window_dates:
        raise RuntimeError("No Desktop history dates inside redistribution window")

    w_added_arr = [desktop_by_date[d]["added"] for d in window_dates]
    w_gone_arr = [desktop_by_date[d]["removed"] for d in window_dates]
    w_churn_arr = [a + g for a, g in zip(w_added_arr, w_gone_arr)]

    def _draw(weights: list[float]) -> str:
        total = sum(weights)
        if total <= 0:
            return random.choice(window_dates)
        return random.choices(window_dates, weights=weights, k=1)[0]

    # ---------------------------------------------------------------------
    # Step 4: redistribute the lump
    # ---------------------------------------------------------------------
    by_month: dict[str, list[dict]] = defaultdict(list)

    # First: load existing events from other months (we'll fold our redistribution into them)
    for month_file in sorted(os.listdir(EVENTS_DIR)) if os.path.isdir(EVENTS_DIR) else []:
        if not month_file.startswith("events-") or not month_file.endswith(".json"):
            continue
        if month_file in ("events-index.json", "licence-month-index.json"):
            continue
        if month_file == "events-2026-05.json":
            # We'll rebuild this one below; keep its non-anchor portion only.
            for e in other_dates_05:
                by_month["2026-05"].append(e)
            continue
        m = month_file.replace("events-", "").replace(".json", "")
        for e in _read_json(os.path.join(EVENTS_DIR, month_file)):
            by_month[m].append(e)

    # Now do the redistribution
    for e in lump:
        if e["event_type"] == "added":
            new_date = _draw(w_added_arr)
        elif e["event_type"] == "gone":
            new_date = _draw(w_gone_arr)
        else:  # upgraded / downgraded
            new_date = _draw(w_churn_arr)
        new_event = dict(e)
        new_event["date"] = new_date
        by_month[new_date[:7]].append(new_event)

    # Add Desktop's events for 2026-05-08
    for e in desktop_added_events + desktop_gone_events:
        by_month["2026-05"].append(e)

    # Sort + write all month files
    for month, events in by_month.items():
        events.sort(
            key=lambda e: (e.get("date", ""), e.get("event_type", ""), e.get("licence_id", ""))
        )
        # dedupe within a month: same (date, event_type, licence_id) collapse to one
        seen = set()
        deduped = []
        for e in events:
            key = (e.get("date"), e.get("event_type"), e.get("licence_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(e)
        path = os.path.join(EVENTS_DIR, f"events-{month}.json")
        _write_json(path, deduped)
        logging.info(f"Wrote {path} ({len(deduped)} events)")

    # ---------------------------------------------------------------------
    # Step 5: parse failures
    # ---------------------------------------------------------------------
    if parse_failures:
        existing_failures: list[dict] = []
        if os.path.exists(PARSE_FAILURES_FILE):
            try:
                existing_failures = _read_json(Path(PARSE_FAILURES_FILE))
                if isinstance(existing_failures, dict):
                    existing_failures = existing_failures.get("failures", [])
            except Exception:
                existing_failures = []
        _write_json(
            PARSE_FAILURES_FILE,
            {
                "source": "import_desktop_data",
                "count": len(parse_failures),
                "failures": parse_failures,
            },
        )
        logging.info(f"Wrote {PARSE_FAILURES_FILE} ({len(parse_failures)} import failures)")

    # ---------------------------------------------------------------------
    # Step 6: rebuild auxiliary artefacts
    # ---------------------------------------------------------------------
    build_licence_month_index(EVENTS_DIR)
    master_df = recompute_rating_summary(master_df, EVENTS_DIR)
    master_df.to_csv(MASTER_FILE, index=False)
    logging.info(f"Refreshed rating summaries on {MASTER_FILE}")

    current_active = int((master_df["status"] == "active").sum())
    history = rebuild_history_from_events(EVENTS_DIR, current_total_active=current_active)
    # Overlay Desktop history's daily added/gone counts (those are real cron
    # observations of churn). Deliberately DO NOT overlay Desktop's `total`
    # field — gov.uk's row count is 312 higher than our master because
    # migration collapsed 312 rating-formatting duplicates into single
    # licence_ids. Using Desktop's total would make the Daily Log "Active"
    # column disagree with the "Active Sponsors" KPI (which reads master).
    history_by_date = {h["date"]: h for h in history}
    for d in desktop_history:
        if d["date"] in history_by_date:
            entry = history_by_date[d["date"]]
            entry["added"] = d.get("added", entry.get("added", 0))
            entry["gone"] = d.get("removed", entry.get("gone", 0))
            entry["removed"] = entry["downgraded"] + entry["gone"]
        else:
            history_by_date[d["date"]] = {
                "date": d["date"],
                "added": d.get("added", 0),
                "upgraded": 0,
                "downgraded": 0,
                "gone": d.get("removed", 0),
                "removed": d.get("removed", 0),
                "total": None,  # filled in by back-projection below
            }
    merged_history = sorted(history_by_date.values(), key=lambda r: r["date"])

    # Re-back-project `total` for every entry from current_active so the whole
    # timeline is internally consistent with master_register.csv.
    # total_at(D) = total_at(D+1) - added[D+1] + gone[D+1]
    running = current_active
    for r in reversed(merged_history):
        r["total"] = running
        running = running - int(r.get("added", 0) or 0) + int(r.get("gone", 0) or 0)

    _write_json(HISTORY_FILE, merged_history)
    logging.info(f"Wrote {HISTORY_FILE} ({len(merged_history)} entries)")

    # ---------------------------------------------------------------------
    # Step 7: report
    # ---------------------------------------------------------------------
    final_2026_05 = [e for e in by_month["2026-05"] if e.get("date") == ANCHOR_DATE]
    by_month_counts = {m: len(v) for m, v in sorted(by_month.items())}
    blind_spot_count = sum(
        1
        for e in by_month.get("2026-02", []) + by_month.get("2026-03", [])
        if "2026-02-28" <= e.get("date", "") < WINDOW_START
    )
    report = {
        "desktop_added_events": len(desktop_added_events),
        "desktop_gone_events": len(desktop_gone_events),
        "lump_redistributed": len(lump),
        "events_on_anchor_after": len(final_2026_05),
        "events_per_month_after": by_month_counts,
        "parse_failures": len(parse_failures),
        "blind_spot_events_assigned": blind_spot_count,
        "random_seed": RANDOM_SEED,
        "history_entries": len(merged_history),
    }
    _write_json(REPORT_FILE, report)
    logging.info(f"Wrote {REPORT_FILE}")
    return report


if __name__ == "__main__":
    run()
