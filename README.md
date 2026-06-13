# UK Sponsor Licence Churn Tracker 🇬🇧

A fully automated tool that tracks the **UK Government's Register of Licensed
Sponsors** and produces a daily licence-churn 軌跡 (trajectory) covering four
event types per licence:

| Event | When it fires |
|---|---|
| `added` | A licence_id appears for the first time — completely new org, or an existing org getting a new route or type |
| `upgraded` | Same licence_id, valid rating-tier increase (Provisional → A, or B → A) |
| `downgraded` | Same licence_id, rating-tier decrease (A → B). The only legitimate downgrade — see lifecycle below. |
| `gone` | Licence_id no longer in today's CSV. Revoked and silently-disappeared cannot be split — the register provides no signal to distinguish them, so both produce `gone`. |
| `service_changed` | Customer-service tier change (Premium / SME+ ↔ ""). Not a rating change — the licence stays A-rated. |

🚀 **Live Dashboard:** the static dashboard renders directly from the committed
`stats.json` / `history.json` / `master_register.csv` files. Enable GitHub Pages
on the `main` branch and point it at `index.html`.

---

## Design — identity vs state

The gov.uk CSV publishes 5 fields per row:
`Organisation Name, Town/City, County, Type & Rating, Route`.

The tracker splits these into **immutable identity** and **mutable state** so
that a rating change is detected as a *change*, not as a remove+add pair:

- **Licence identity** = `sha1(name_norm | town_norm | county_norm | route | type)`
  — five components, stable across the lifetime of one licence.
- **State** = `current_rating`, `status`, `last_seen_date`, `removed_date`
  — updated each daily run; rating changes emit upgraded/downgraded events.

`Type & Rating` is parsed into:
- `type` ∈ {`worker`, `temporary worker`} — part of identity (the licence's nature)
- `rating` ∈ {`A`, `Provisional`, `B`} — mutable
- `service` ∈ {`Premium`, `SME+`, ``} — mutable, separate from rating

**Rating lifecycle** (per UK Home Office rules):
- `Provisional` — Initial state for UK Expansion Worker scheme sponsors
  (Authorising Officer outside the UK). One CoS allowed to bring the AO over.
  Must be upgraded to A once the business is established.
- `A` — Active sponsor, can issue Certificates of Sponsorship.
- `B` — Warning. An A-rated sponsor that fails compliance drops to B; can't
  issue new CoS until duties are met.

**Valid transitions only**:
- `Provisional → A` (upgraded — UKEW graduation)
- `A → B` (downgraded — compliance failure)
- `B → A` (upgraded — compliance restored)

Provisional is initial-only — you can't drop to it from A/B, and you can't
downgrade *from* Provisional. Anything outside these three arcs is logged
to `rating_anomalies.json` and not emitted as a regular event.

`Premium` and `SME+` are NOT rating tiers — they are paid 12-month
*support services* that sit on top of an A rating. Stored in the separate
`current_service` field.

**UKVI terminated the Premium Customer Service program on 2025-11-11.**
Going forward:
- `A → Premium` and `A → SME+` are no longer possible.
- `Premium → ""` and `SME+ → ""` are the typical transitions (legacy
  agreements expire and the account drops back to plain A).

These transitions are tracked as a fifth event type, **`service_changed`**,
distinct from rating up/downgrades. The licence stays A-rated and fully
able to sponsor; only the customer-service tier is lost.

---

## Normalisation contract

`normalisation.py` collapses upstream formatting drift to a canonical form so
the diff doesn't fire false churn:

- **Names**: lowercase, NFKC, strip embedded company numbers like
  `(15444604)`, strip trailing 8+ digit numbers, standardise legal-form suffix
  (`Limited` / `Ltd.` / `PLC` / `Inc` / `Corp` / `Co` / `LLP` / `LLC` → `ltd`),
  strip remaining punctuation, collapse whitespace.
- **Town**: lowercase, collapse whitespace, replace hyphens/dashes with spaces.
- **County**: lowercase, collapse whitespace, `Co.` / `Co` → `county`, then
  alias lookup (e.g. `down` → `county down`, `tyne and wear` → `tyne & wear`,
  `staffs` → `staffordshire`, `northern ireland` → empty because it's a region
  not a county).
- **Placeholders**: `NULL`, `Select One`, `nan`, `N/A`, `-`, `.`, etc. → empty.

A **two-phase match** absorbs county fill-ins/drops by gov.uk without firing
false churn:
1. Strict match on `licence_id`.
2. For unmatched rows on either side, fall back to a loose id that drops county.
3. A unique loose match means "same licence, county metadata changed" — update
   the stored county silently and log to `county_corrections.json`.

---

## Files in this repo

### Code
- `tracker.py` — daily pipeline: fetch → normalise → diff → emit events → write outputs
- `normalisation.py` — pure normalisation primitives + `parse_type_rating()` + `compute_licence_id()`
- `licence_history.py` — rating-history summary recomputation + per-licence month index
- `migrate_master.py` — one-shot migration from the old composite-key schema to the new one
- `replay.py` — one-shot replay: walks a directory of historical CSVs to backfill `events/`

### State
- `master_register.csv` — identity + current state + rating-history summary, one row per licence (~144k rows). Schema:
  ```
  licence_id, organisation_name_raw, organisation_name_norm,
  town_raw, town_norm, county_raw, county_norm,
  route, type, current_rating, type_rating_raw, status,
  first_seen_date, last_seen_date, removed_date,
  rating_change_count, first_rating_change_date, last_rating_change_date
  ```
  The last three columns are *cached summaries* derived from `events/`. They
  let the dashboard sort by "most-churned licences" and surface first/last
  rating-change dates without scanning the events log on every request.
- `master_register.csv.bak` — pre-migration backup (kept for one release)

### Outputs
- `events/events-YYYY-MM.json` — append-only churn log, one file per month
- `events/events-index.json` — metadata index (months, counts, date ranges)
- `events/licence-month-index.json` — `{licence_id: [months_with_events]}`
  mapping used by the dashboard to fetch only relevant month files when
  rendering a single licence's history
- `daily_delta.json` — today's events grouped by type (with backward-compat `removed` alias)
- `stats.json` — dashboard metrics (daily counts, totals, top-N, per-type recency lists)
- `history.json` — per-day counts (new fields: `upgraded`, `downgraded`, `gone`; legacy `removed` alias kept)
- `migration_report.json` — one-time migration audit (counts, consolidation breakdown)
- `replay_report.json` — one-time replay audit (snapshots processed, events emitted per type)

### Diagnostics
- `county_corrections.json` — silent county updates from two-phase match
- `parse_failures.json` — rows whose `Type & Rating` couldn't be parsed (run continues; row excluded)
- `rating_anomalies.json` — rating transitions that aren't in the valid set (e.g. `A → Provisional`). Logged for review, not emitted as upgrade/downgrade events.
- `suspected_false_churn.json` — daily diagnostic: Levenshtein-near add/gone pairs within ±3 days, same `(town, route, type)`. Review periodically and seed new aliases into `normalisation.COUNTY_ALIASES` etc.

---

## Local usage

```bash
pip install -r requirements.txt

# Run the daily diff once (uses live gov.uk):
python tracker.py

# Verify normalisation primitives:
python normalisation.py

# Re-run migration (idempotent — uses .bak as source on subsequent runs):
python migrate_master.py            # writes new master + backup
python migrate_master.py --dry-run  # report only
```

### Replay historical snapshots

Place gov.uk CSVs with the original filename convention into `historical/`:

```
historical/
  2026-01-15_-_Worker_and_Temporary_Worker.csv
  2026-02-01_-_Worker_and_Temporary_Worker.csv
  ...
```

Then:

```bash
python replay.py                  # populates events/ from oldest → newest
python replay.py --emit-baseline  # also emits 'added' events for the first
                                  # snapshot (default skips ~140k baseline noise)
```

**Important — coordinate with the cron**: replay rebuilds `events/` from
scratch and runs locally. To avoid races with GitHub Actions, comment out the
`cron:` line in `.github/workflows/daily_update.yml` before running replay
locally, then restore it once the rebuilt `events/` is pushed.

---

## Automation

The GitHub Actions workflow `.github/workflows/daily_update.yml` runs once per
weekday at 19:00 UTC (7pm), Monday–Friday (`cron: '0 19 * * 1-5'`). It:

1. Checks out the repo, sets up Python 3.9, installs `requirements.txt`.
2. Runs `python tracker.py`.
3. Commits and pushes any changes to `master_register.csv`, `stats.json`,
   `history.json`, `daily_delta.json`, `events/`, `daily_csv/`, and the
   diagnostic JSON files.

`tracker.py` also archives the raw gov.uk source CSV under `daily_csv/`
(named by publication date), keeping only the most recent
`CSV_RETENTION` (10) copies and pruning older ones. `daily_csv/manifest.json`
lists the retained files and powers the dashboard's public download section.

---

## Known limits

- **撤銷 vs 消失 cannot be split** from the published register alone. The CSV
  has no `revocation_reason` column, no separate "lost their licence" feed is
  linked from the gov.uk page. Both end up in `gone`.
- **Replay loses events for dates with no snapshot.** If you don't have a
  CSV for a given date, the diff from yesterday → tomorrow merges any
  rating changes that happened in between.
- **Pure name changes fire false churn.** If `"ACME LTD"` rebrands to
  `"ACME GROUP LTD"`, the normaliser produces different name_norm, so the
  licence shows up as gone + added. `suspected_false_churn.json` flags these
  for manual review.
- **Pure relocations fire false churn.** Same logic: if a sponsor moves from
  one town/county to another, identity changes → gone + added.

---

## Disclaimer

This tool uses data sourced from the UK Government's
[Register of Worker and Temporary Worker licensed sponsors](https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers).
It is an independent project, **not affiliated, endorsed, or connected** with
GOV.UK or the UK Home Office. Data provided as-is.
