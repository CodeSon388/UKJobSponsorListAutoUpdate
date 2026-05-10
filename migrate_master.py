"""
One-shot migration from the old master_register.csv schema to the new one.

OLD schema (composite-key-as-identity, rating included in identity):
    Organisation Name, Town/City, County, Type & Rating, Route,
    id, first_seen, last_updated, removed_date

NEW schema (identity decoupled from state):
    licence_id, organisation_name_raw, organisation_name_norm,
    town_raw, town_norm, county_raw, county_norm,
    route, type, current_rating, type_rating_raw, status,
    first_seen_date, last_seen_date, removed_date

Behaviour:
- Backs up the existing master_register.csv to master_register.csv.bak
- Reads all rows, normalises identity fields, computes licence_id
- Groups by licence_id (so rows that the old key kept apart because of rating
  formatting drift now collapse into one)
- Writes the new-schema master_register.csv
- Writes migration_report.json with consolidation statistics for review

Run:
    python migrate_master.py            # full migration, writes new master
    python migrate_master.py --dry-run  # report only, no files written

Safe to re-run: backup is only created on the first run (won't overwrite a
backup); subsequent runs use the existing backup as the source of truth.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections import Counter

import pandas as pd

from normalisation import (
    compute_licence_id,
    normalise_county,
    normalise_name,
    normalise_route,
    normalise_town,
    parse_type_rating,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

MASTER_FILE = "master_register.csv"
BACKUP_FILE = "master_register.csv.bak"
REPORT_FILE = "migration_report.json"

NEW_COLUMNS = [
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
    # Rating-history summaries (initialised empty; populated by tracker/replay
    # as upgraded/downgraded events fire over time).
    "rating_change_count",
    "first_rating_change_date",
    "last_rating_change_date",
]


def _source_path() -> str:
    """If a backup already exists, prefer it as the source (re-runnable)."""
    return BACKUP_FILE if os.path.exists(BACKUP_FILE) else MASTER_FILE


def _ensure_backup() -> None:
    if os.path.exists(BACKUP_FILE):
        logging.info(f"Backup already exists at {BACKUP_FILE}; re-using.")
        return
    if not os.path.exists(MASTER_FILE):
        raise FileNotFoundError(f"No {MASTER_FILE} to back up.")
    shutil.copy2(MASTER_FILE, BACKUP_FILE)
    logging.info(f"Backed up {MASTER_FILE} -> {BACKUP_FILE}")


def _normalise_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """
    Apply normalisation + identity computation to every row.

    Returns:
        (normalised_df, parse_failures) — failures are rows whose Type & Rating
        couldn't be parsed; they are EXCLUDED from the normalised_df and listed
        in the failures report for review.
    """
    failures: list[dict] = []
    out_rows: list[dict] = []

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
        lid = compute_licence_id(name_n, town_n, county_n, route_n, type_str)

        out_rows.append(
            {
                "licence_id": lid,
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
                "first_seen_date": row.get("first_seen", ""),
                "last_updated": row.get("last_updated", ""),
                "removed_date": row.get("removed_date", ""),
            }
        )

    return pd.DataFrame(out_rows), failures


def _consolidate(normalised: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Group by licence_id and merge multiple raw rows into one canonical state.

    Conflict resolution within a licence_id group:
    - first_seen_date  : MIN of the group (earliest known appearance)
    - last_seen_date   : MAX of last_updated (latest known appearance)
    - current_rating   : rating from the row with the LATEST last_updated
                         (most recent state wins)
    - status           : 'active' if ANY row in group has removed_date empty,
                         else 'gone' (with removed_date = max removed_date)
    - removed_date     : same MAX rule; null when status=active
    - raw display fields (org name, town, county): from the latest-last_updated
                         row (most recent gov.uk formatting)
    """
    # Parse date columns so min/max work correctly
    for col in ("first_seen_date", "last_updated", "removed_date"):
        normalised[col] = pd.to_datetime(
            normalised[col], errors="coerce"
        )

    consolidated_rows = []
    consolidation_sizes = Counter()

    for lid, group in normalised.groupby("licence_id", sort=False):
        n_raw = len(group)
        consolidation_sizes[n_raw] += 1

        # Latest row by last_updated (most recent state observation)
        latest = group.sort_values("last_updated", ascending=False).iloc[0]

        first_seen = group["first_seen_date"].min()
        last_seen = group["last_updated"].max()

        # Active if any group row was still active in its snapshot
        any_active = group["removed_date"].isna().any()
        if any_active:
            status = "active"
            removed_date_out = pd.NaT
        else:
            status = "gone"
            removed_date_out = group["removed_date"].max()

        consolidated_rows.append(
            {
                "licence_id": lid,
                "organisation_name_raw": latest["organisation_name_raw"],
                "organisation_name_norm": latest["organisation_name_norm"],
                "town_raw": latest["town_raw"],
                "town_norm": latest["town_norm"],
                "county_raw": latest["county_raw"],
                "county_norm": latest["county_norm"],
                "route": latest["route"],
                "type": latest["type"],
                "current_rating": latest["current_rating"],
                "type_rating_raw": latest["type_rating_raw"],
                "status": status,
                "first_seen_date": first_seen.strftime("%Y-%m-%d") if pd.notna(first_seen) else "",
                "last_seen_date": last_seen.strftime("%Y-%m-%d") if pd.notna(last_seen) else "",
                "removed_date": removed_date_out.strftime("%Y-%m-%d") if pd.notna(removed_date_out) else "",
                # Rating-history summaries start at zero/empty — replay will
                # populate them by scanning events/ once historical data lands.
                "rating_change_count": 0,
                "first_rating_change_date": "",
                "last_rating_change_date": "",
            }
        )

    consolidated = pd.DataFrame(consolidated_rows, columns=NEW_COLUMNS)

    stats = {
        "consolidation_size_distribution": dict(
            sorted(consolidation_sizes.items())
        ),
        "licences_with_single_raw_row": consolidation_sizes.get(1, 0),
        "licences_consolidated": sum(
            v for k, v in consolidation_sizes.items() if k > 1
        ),
    }
    return consolidated, stats


def _emit_report(
    n_raw: int,
    parse_failures: list[dict],
    consolidated: pd.DataFrame,
    consolidation_stats: dict,
    output_path: str,
) -> None:
    status_counts = consolidated["status"].value_counts().to_dict()
    rating_counts = consolidated[consolidated["status"] == "active"][
        "current_rating"
    ].value_counts().to_dict()
    type_counts = consolidated[consolidated["status"] == "active"][
        "type"
    ].value_counts().to_dict()

    report = {
        "input_rows": n_raw,
        "parse_failures_count": len(parse_failures),
        "parse_failures_sample": parse_failures[:20],
        "output_licences_total": len(consolidated),
        "output_status_counts": status_counts,
        "active_licences_rating_breakdown": rating_counts,
        "active_licences_type_breakdown": type_counts,
        **consolidation_stats,
    }

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logging.info(f"Wrote migration report -> {output_path}")


def migrate(dry_run: bool = False) -> None:
    src = _source_path()
    if not os.path.exists(src):
        raise FileNotFoundError(f"No source file at {src}")

    logging.info(f"Reading source: {src}")
    df = pd.read_csv(src)
    logging.info(f"  loaded {len(df)} rows")

    logging.info("Normalising every row and computing licence_id ...")
    normalised, parse_failures = _normalise_rows(df)
    logging.info(
        f"  normalised: {len(normalised)} rows, {len(parse_failures)} parse failures"
    )

    logging.info("Consolidating groups by licence_id ...")
    consolidated, consolidation_stats = _consolidate(normalised)
    logging.info(
        f"  produced {len(consolidated)} licences "
        f"(consolidated {consolidation_stats['licences_consolidated']} multi-row groups)"
    )

    _emit_report(
        n_raw=len(df),
        parse_failures=parse_failures,
        consolidated=consolidated,
        consolidation_stats=consolidation_stats,
        output_path=REPORT_FILE,
    )

    if dry_run:
        logging.info("Dry run -- not writing new master_register.csv")
        return

    _ensure_backup()
    consolidated.to_csv(MASTER_FILE, index=False)
    logging.info(f"Wrote new-schema master_register.csv ({len(consolidated)} rows)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write new master_register.csv; only emit migration_report.json",
    )
    args = parser.parse_args(argv)
    try:
        migrate(dry_run=args.dry_run)
    except Exception as e:
        logging.exception(f"Migration failed: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
