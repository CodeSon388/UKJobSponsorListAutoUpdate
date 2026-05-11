"""
Normalisation primitives for the UK Sponsor Licence Churn Tracker.

This module turns raw gov.uk CSV strings into canonical forms so that
upstream formatting drift (case, whitespace, "Ltd"/"Limited", "DOWN"/"Co. Down",
etc.) does not produce false licence-churn events.

Public API:
    normalise_name(s)            -> str
    normalise_town(s)            -> str
    normalise_county(s)          -> str
    normalise_route(s)           -> str
    parse_type_rating(s)         -> (type, rating)
    compute_licence_id(...)      -> str   (16-hex sha1)
    rating_rank(rating)          -> int   (higher = better; raises on unknown)
    RATING_RANK                  -> dict

All normalisers return "" for None/NaN/placeholder inputs.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


# ---------------------------------------------------------------------------
# Layer 1 — always-safe transforms
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_PUNCT_KEEP_SPACE = re.compile(r"[^a-z0-9\s]")

# Placeholders that mean "missing" — collapsed to empty string in Layer 2.
_PLACEHOLDERS = {
    "",
    "nan",
    "none",
    "null",
    "n/a",
    "na",
    "select one",
    "-",
    ".",
    "tbd",
    "tba",
}


def _layer1(s) -> str:
    """Lowercase, NFKC, collapse whitespace. Never raises."""
    if s is None:
        return ""
    try:
        s = unicodedata.normalize("NFKC", str(s)).lower()
    except Exception:
        s = str(s).lower()
    return _WS.sub(" ", s).strip()


def _layer2(s: str) -> str:
    """Placeholder rejection."""
    return "" if s in _PLACEHOLDERS else s


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

# Company numbers embedded as "(15444604)" or "- 14648180" suffixes.
_COMPANY_NUMBER_PAREN = re.compile(r"\s*[\(\-]\s*\d{6,}\s*\)?")

# Trailing standalone 7+ digit numbers ("ACME LTD 12345678").
_TRAILING_NUMBER = re.compile(r"\s+\d{7,}\s*$")

# Legal-form suffix tokens — standardised to "ltd" so all entity types
# end up with the same canonical suffix. (Decision: keep, don't strip.)
_LEGAL_SUFFIX = re.compile(
    r"\b(uk\s+)?(limited|ltd|llp|llc|plc|inc|corp|company|co|pvt|s\.coop|ag|lp)\b\.?"
)


def normalise_name(s) -> str:
    """
    Canonical form of an organisation name.

    Order of operations matters:
      1. lowercase + whitespace + placeholder
      2. strip parenthesised company numbers like "(15444604)" / "-14648180"
      3. strip trailing standalone 7+ digit numbers
      4. strip remaining punctuation -> spaces  (so "(UK) Limited" becomes " UK  Limited")
      5. collapse whitespace
      6. standardise legal suffix tokens to "ltd"  (now "uk limited" matches the
         "(uk\\s+)?(limited)" pattern; we couldn't catch this before step 4)
      7. final whitespace collapse

    Examples:
        "ACME Ltd."             -> "acme ltd"
        "Acme Limited"          -> "acme ltd"
        "Acme (UK) Limited"     -> "acme ltd"      (uk stripped via legal-suffix prefix)
        "ABC Ltd (15444604)"    -> "abc ltd"       (company number stripped)
        "ABC Ltd 12345678"      -> "abc ltd"       (trailing number stripped)
        "&SISTERS LTD"          -> "sisters ltd"   (& stripped as punct)
    """
    s = _layer2(_layer1(s))
    if not s:
        return ""
    s = _COMPANY_NUMBER_PAREN.sub("", s)
    s = _TRAILING_NUMBER.sub("", s)
    s = _PUNCT_KEEP_SPACE.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    s = _LEGAL_SUFFIX.sub("ltd", s)
    return _WS.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# Town normalisation
# ---------------------------------------------------------------------------

# Various hyphen/dash characters → space (handles "Newcastle-Upon-Tyne").
_HYPHENS = re.compile(r"[-‐-―]")


def normalise_town(s) -> str:
    """
    Canonical form of a town/city.

    Examples:
        "Newcastle-Upon-Tyne"  -> "newcastle upon tyne"
        "LONDON"               -> "london"
        "London "              -> "london"
    """
    s = _layer2(_layer1(s))
    if not s:
        return ""
    s = _HYPHENS.sub(" ", s)
    return _WS.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# County normalisation — Layer 1+2 plus alias dictionary
# ---------------------------------------------------------------------------

# Seeded from real data variants observed in master_register.csv. Grows over
# time as `suspected_false_churn.json` surfaces new variants.
COUNTY_ALIASES: dict[str, str] = {
    # Northern Ireland — "Down" / "DOWN" / "Co. Down" all mean County Down.
    "down": "county down",
    "co down": "county down",
    "co. down": "county down",
    # Tyne & Wear ↔ Tyne and Wear
    "tyne and wear": "tyne & wear",
    # Staffordshire
    "staffs": "staffordshire",
    # Greater London / London
    "greater london": "london",
    "greater lon": "london",
    # Regions filed as counties — too vague, treat as missing.
    "northern ireland": "",
    "england": "",
    "eng": "",
    "scotland": "",
    "uk": "",
    "united kingdom": "",
    # Notts → Nottinghamshire
    "notts": "nottinghamshire",
}

# Regex: "co." / "Co" / "co" as a standalone token → "county "
# Note: \b after \.? doesn't match between "." and space (both non-word),
# so we match "co" with a word boundary then greedily consume an optional dot.
_CO_DOT = re.compile(r"\bco\b\.?")


def normalise_county(s) -> str:
    """
    Canonical form of a county.

    Examples:
        "DOWN"         -> "county down"
        "Co. Down"     -> "county down"
        "County Down"  -> "county down"
        "Tyne and Wear"-> "tyne & wear"   (alias)
        "Staffs"       -> "staffordshire" (alias)
        "NULL"         -> ""              (placeholder)
        "Northern Ireland" -> ""          (too vague — region not county)
    """
    s = _layer2(_layer1(s))
    if not s:
        return ""
    s = _CO_DOT.sub("county", s)
    s = _WS.sub(" ", s).strip()
    return COUNTY_ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# Route normalisation — Layer 1+2 only
# ---------------------------------------------------------------------------


def normalise_route(s) -> str:
    """Canonical form of a route. Routes from gov.uk are fairly clean."""
    return _layer2(_layer1(s))


# ---------------------------------------------------------------------------
# Type & Rating parsing
# ---------------------------------------------------------------------------

# All Type & Rating values we've seen in real data:
#   "Worker (A rating)"
#   "Worker (B rating)"
#   "Worker (A (Premium))"
#   "Worker (A (SME+))"
#   "Worker (UK Expansion Worker: Provisional)"
#   "Worker (UK Expansion Worker: Provisional )"   <- trailing space variant
#   "Temporary Worker (A rating)"
#   "Temporary Worker (B rating)"
#   "Temporary Worker (A (Premium))"
#   "Temporary Worker (A (SME+))"
#
# Structure: "<Type> (<inner>)" where Type ∈ {Worker, Temporary Worker}
# and <inner> determines the rating tier.

_TYPE_RATING_OUTER = re.compile(
    r"^\s*(temporary\s+worker|worker)\s*\((.+?)\)\s*$",
    flags=re.IGNORECASE,
)


def parse_type_rating(s) -> tuple[str, str, str]:
    """
    Split "Type & Rating" into (type, rating, service).

    The Home Office only operates three rating tiers (A / Provisional / B).
    Premium and SME+ are 12-month *support services* layered on top of an A
    rating, NOT rating tiers themselves — so they go into `service`, not
    `rating`. This matters because rating-change events (upgrade/downgrade)
    should only fire on real rating transitions, not on a sponsor opting in
    or out of premium support.

    Returns:
        (type, rating, service) where:
            type     ∈ {"worker", "temporary worker"}
            rating   ∈ {"A", "Provisional", "B"}
            service  ∈ {"Premium", "SME+", ""}  (empty string = no service)

    Raises:
        ValueError if the input doesn't match a known pattern. Callers should
        catch and route the row to parse_failures.json — never silently coerce.
    """
    if s is None:
        raise ValueError("parse_type_rating: input is None")
    raw = str(s).strip()
    if not raw:
        raise ValueError("parse_type_rating: input is empty")

    m = _TYPE_RATING_OUTER.match(raw)
    if not m:
        raise ValueError(f"Unrecognised Type & Rating: {raw!r}")

    type_str = m.group(1).lower().strip()
    type_str = _WS.sub(" ", type_str)
    if type_str not in ("worker", "temporary worker"):
        raise ValueError(f"Unrecognised type in Type & Rating: {raw!r}")

    inner = m.group(2).strip()
    inner_lower = inner.lower()

    # Provisional — UK Expansion Worker scheme (initial state for new sponsors
    # whose Authorising Officer is outside the UK).
    if "provisional" in inner_lower:
        return type_str, "Provisional", ""

    # "A (Premium)" / "A (SME+)" — A-rating + a support service.
    if "premium" in inner_lower:
        return type_str, "A", "Premium"
    if "sme+" in inner_lower or "sme +" in inner_lower:
        return type_str, "A", "SME+"

    # Plain "A rating" / "B rating" — rating tier, no support service.
    if re.search(r"\ba\s*rating\b", inner_lower):
        return type_str, "A", ""
    if re.search(r"\bb\s*rating\b", inner_lower):
        return type_str, "B", ""

    raise ValueError(f"Unrecognised rating inside Type & Rating: {raw!r}")


# ---------------------------------------------------------------------------
# Rating rank (for upgrade/downgrade detection)
# ---------------------------------------------------------------------------

# Higher rank = better standing.  Only three real tiers per Home Office:
#   A           — Active sponsor, can issue Certificates of Sponsorship (CoS).
#   Provisional — Initial state for new sponsors whose Authorising Officer
#                 is outside the UK; can issue 1 CoS to bring the AO in.
#                 Must be upgraded to A once the business is established.
#   B           — Warning/Downgraded; can't issue new CoS until duties are
#                 met.  An A-rated sponsor that fails compliance drops to B.
#
# Premium and SME+ are NOT rating tiers — they are 12-month support services
# that can sit on top of an A rating. parse_type_rating() returns them in a
# separate `service` field, so they do not appear here.
#
# Conceptual lifecycle: Provisional -> A -> (compliance failure) -> B.
RATING_RANK: dict[str, int] = {
    "A": 2,
    "Provisional": 1,
    "B": 0,
}


def rating_rank(rating: str) -> int:
    """Return numeric rank for a rating. Raises on unknown ratings."""
    if rating not in RATING_RANK:
        raise ValueError(f"Unknown rating tier: {rating!r}")
    return RATING_RANK[rating]


# ---------------------------------------------------------------------------
# Licence identity
# ---------------------------------------------------------------------------


def compute_licence_id(
    name_norm: str,
    town_norm: str,
    county_norm: str,
    route_norm: str,
    type_str: str,
) -> str:
    """
    Stable per-licence identity. Independent of rating (which is mutable state).

    Returns the first 16 hex chars of sha1(joined) — 64 bits of identity, low
    collision risk at our scale (~10^5 licences).
    """
    joined = "|".join([name_norm, town_norm, county_norm, route_norm, type_str])
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return digest[:16]


def compute_loose_id(
    name_norm: str,
    town_norm: str,
    route_norm: str,
    type_str: str,
) -> str:
    """
    Identity WITHOUT county — used in the two-phase match to absorb county
    fill-ins / drops by gov.uk without firing false churn events.
    """
    joined = "|".join([name_norm, town_norm, route_norm, type_str])
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Self-test — run `python normalisation.py` to verify all cases pass.
# ---------------------------------------------------------------------------


def _run_self_test() -> None:
    """Acceptance scenarios from the plan. Raises AssertionError on failure."""

    # --- normalise_name --------------------------------------------------
    assert normalise_name("ACME Ltd.") == "acme ltd"
    assert normalise_name("Acme Limited") == "acme ltd"
    assert normalise_name("ACME LTD") == "acme ltd"
    assert normalise_name("abc ltd") == "abc ltd"
    assert normalise_name("Acme  Ltd") == "acme ltd"  # double space
    assert normalise_name("ABC Ltd (15444604)") == "abc ltd"  # company number
    assert normalise_name("ABC Ltd 12345678") == "abc ltd"  # trailing number
    assert normalise_name(None) == ""
    assert normalise_name("") == ""
    assert normalise_name("&SISTERS LTD") == "sisters ltd"  # & stripped by punct
    assert normalise_name("K Line Energy Shipping (UK) Limited") == "k line energy shipping ltd"

    # --- normalise_town --------------------------------------------------
    assert normalise_town("LONDON") == "london"
    assert normalise_town("London ") == "london"
    assert normalise_town("Newcastle-Upon-Tyne") == "newcastle upon tyne"
    assert normalise_town("Newcastle upon Tyne") == "newcastle upon tyne"
    assert normalise_town(None) == ""

    # --- normalise_county ------------------------------------------------
    assert normalise_county("DOWN") == "county down"
    assert normalise_county("Down") == "county down"
    assert normalise_county("Co. Down") == "county down"
    assert normalise_county("County Down") == "county down"
    assert normalise_county("Tyne and Wear") == "tyne & wear"
    assert normalise_county("Tyne & Wear") == "tyne & wear"
    assert normalise_county("Staffs") == "staffordshire"
    assert normalise_county("Staffordshire") == "staffordshire"
    assert normalise_county("Greater London") == "london"
    assert normalise_county("NORTHERN IRELAND") == ""  # too vague
    assert normalise_county("NULL") == ""
    assert normalise_county("Select One") == ""
    assert normalise_county(None) == ""
    assert normalise_county("nan") == ""

    # --- parse_type_rating -----------------------------------------------
    # Plain ratings (no service)
    assert parse_type_rating("Worker (A rating)") == ("worker", "A", "")
    assert parse_type_rating("Worker (B rating)") == ("worker", "B", "")
    assert parse_type_rating("Temporary Worker (A rating)") == ("temporary worker", "A", "")
    assert parse_type_rating("Temporary Worker (B rating)") == ("temporary worker", "B", "")
    # A-rating with a support service tier
    assert parse_type_rating("Worker (A (Premium))") == ("worker", "A", "Premium")
    assert parse_type_rating("Worker (A (SME+))") == ("worker", "A", "SME+")
    assert parse_type_rating("Temporary Worker (A (Premium))") == ("temporary worker", "A", "Premium")
    assert parse_type_rating("Temporary Worker (A (SME+))") == ("temporary worker", "A", "SME+")
    # Provisional (UK Expansion Worker scheme)
    assert parse_type_rating("Worker (UK Expansion Worker: Provisional)") == ("worker", "Provisional", "")
    assert parse_type_rating("Worker (UK Expansion Worker: Provisional )") == ("worker", "Provisional", "")  # trailing space

    try:
        parse_type_rating("Garbage input")
    except ValueError:
        pass
    else:
        raise AssertionError("parse_type_rating should raise on garbage")

    try:
        parse_type_rating(None)
    except ValueError:
        pass
    else:
        raise AssertionError("parse_type_rating should raise on None")

    # --- rating_rank -----------------------------------------------------
    # Lifecycle: Provisional -> A -> (failure) -> B.  Only three tiers.
    assert rating_rank("A") > rating_rank("Provisional")
    assert rating_rank("Provisional") > rating_rank("B")
    assert rating_rank("A") > rating_rank("B")

    for unknown in ("Premium", "SME+", "Mystery"):
        try:
            rating_rank(unknown)
        except ValueError:
            pass
        else:
            raise AssertionError(f"rating_rank should raise on {unknown!r}")

    # --- compute_licence_id stability ------------------------------------
    id_a = compute_licence_id("acme ltd", "london", "", "skilled worker", "worker")
    id_b = compute_licence_id("acme ltd", "london", "", "skilled worker", "worker")
    assert id_a == id_b, "Same inputs must produce same licence_id"
    assert len(id_a) == 16

    id_c = compute_licence_id("acme ltd", "london", "", "skilled worker", "temporary worker")
    assert id_a != id_c, "Different type must produce different licence_id"

    id_d = compute_licence_id("acme ltd", "london", "", "creative worker", "worker")
    assert id_a != id_d, "Different route must produce different licence_id"

    # --- Identity stability under normalisation: real-world scenarios ----
    # "ACME LTD." / "Acme Limited" / "abc ltd" — but with DIFFERENT raw inputs
    # that should collapse to the same licence_id once normalised.
    raw_variants = [
        ("ACME LTD.",     "LONDON",  "Greater London", "Skilled Worker", "Worker (A rating)"),
        ("Acme Limited",  "London",  "London",         "Skilled Worker", "Worker (B rating)"),
        ("ACME  ltd",     "london ", "GREATER LON",    "Skilled Worker", "Worker (A (Premium))"),
    ]
    ids = []
    for raw_name, raw_town, raw_county, raw_route, raw_tr in raw_variants:
        t, _r, _s = parse_type_rating(raw_tr)
        ids.append(compute_licence_id(
            normalise_name(raw_name),
            normalise_town(raw_town),
            normalise_county(raw_county),
            normalise_route(raw_route),
            t,
        ))
    assert len(set(ids)) == 1, f"All three variants should collapse to one licence_id; got {ids}"

    print("normalisation.py: all self-tests passed.")


if __name__ == "__main__":
    _run_self_test()
