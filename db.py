"""SQLite schema and time helpers shared by sweep.py, report.py and app.py.

Every timestamp is stored as a canonical UTC string ("YYYY-MM-DDTHH:MM:SSZ"). The portal
mixes formats: Delhi sends "+05:30" offsets, other cities send "Z". Normalising once, at
the edge, lets SQL compare and group timestamps as plain strings.
"""
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")
DEFAULT_DB = "osa.db"

SCHEMA = """
-- stores: the portal's roster as we last saw it. Its flags are "now" only (the portal
-- has no as_of for stores), so they explain our tracking decision but aren't
-- historical truth.
CREATE TABLE IF NOT EXISTS stores (
    store_id       TEXT PRIMARY KEY,
    city           TEXT NOT NULL,
    name           TEXT,
    is_active      INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    is_serviceable INTEGER NOT NULL CHECK (is_serviceable IN (0, 1)),
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stores_city ON stores (city);

-- sweeps: one row per sweep run (one as_of). ist_date is computed once, when the
-- sweep is recorded, so every report groups sweeps by the IST day the same way.
-- Report queries never re-derive it from row timestamps.
CREATE TABLE IF NOT EXISTS sweeps (
    as_of       TEXT PRIMARY KEY,
    ist_date    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sweeps_ist_date ON sweeps (ist_date);

-- store_sweeps: the coverage ledger. One row per (sweep, tracked store) says whether
-- that store's data for that sweep is usable. This is how "we don't know" is stored:
-- an observation with no complete store_sweeps row behind it is never counted.
--
-- 'pending' rows exist because the sweep writes a row for every tracked store BEFORE
-- fetching anything. If the sweep crashes, hangs or is killed half-way, the stores it
-- never finished stay 'pending'. They are then still visible as expected but not
-- complete, rather than silently missing (which would shrink stores_expected and make
-- the coverage look better than it is).
CREATE TABLE IF NOT EXISTS store_sweeps (
    as_of      TEXT NOT NULL REFERENCES sweeps (as_of),
    store_id   TEXT NOT NULL REFERENCES stores (store_id),
    status     TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'incomplete')),
    reason     TEXT,
    item_count INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (as_of, store_id)
);

-- observations: one row per SKU listed by one store in one sweep, the unit OSA counts.
-- The primary key makes overlapping pages (the portal repeats the previous page's last
-- item) and re-running a sweep idempotent: the same (sweep, store, SKU) can only exist
-- once. A SKU a store doesn't list simply has no row, so it can never become "out of stock".
CREATE TABLE IF NOT EXISTS observations (
    as_of       TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    sku_id      TEXT NOT NULL,
    name        TEXT,
    in_stock    INTEGER NOT NULL CHECK (in_stock IN (0, 1)),
    qty         INTEGER,
    price       REAL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (as_of, store_id, sku_id),
    FOREIGN KEY (as_of, store_id) REFERENCES store_sweeps (as_of, store_id)
);
"""

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open the database and make sure the schema exists.

    The path comes from OSA_DB when not given, so tests and the API can point at a
    different file without code changes. Foreign keys are off by default in SQLite;
    we turn them on so an observation can't exist without its store_sweeps row.
    """
    conn = sqlite3.connect(path or os.environ.get("OSA_DB", DEFAULT_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def to_utc(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp that carries a timezone and return it in UTC.

    A naive timestamp is rejected, not assumed to be UTC or IST: guessing the zone
    could move a reading to the wrong IST day. "Z" is rewritten to "+00:00" because
    datetime.fromisoformat only accepts "Z" from Python 3.11 on.
    """
    s = ts.strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp has no timezone: {ts!r}")
    return dt.astimezone(timezone.utc)


def _require_aware(dt: datetime) -> None:
    """Refuse naive datetimes. The same mistake as to_utc guards against, made in code."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime has no timezone: {dt!r}")


def utc_str(dt: datetime) -> str:
    """Canonical storage form. One fixed format means string equality is time equality,
    so "2026-09-28T10:00:00+05:30" and "2026-09-28T04:30:00Z" end up as the same key."""
    _require_aware(dt)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_date(dt: datetime) -> str:
    """The Indian calendar day (YYYY-MM-DD) a moment falls on. /osa's `date` means this
    day, and a UTC sweep at 19:00Z already belongs to the next IST day."""
    _require_aware(dt)
    return dt.astimezone(IST).date().isoformat()


def now_utc_str() -> str:
    """Current wall-clock time in the canonical form, for started_at/fetched_at columns."""
    return utc_str(datetime.now(timezone.utc))


def yesterday_in_ist(now: datetime | None = None) -> str:
    """/osa's default date. Uses "today" in IST, not the machine's local zone or UTC:
    between 00:00 and 05:30 IST, UTC still says yesterday, which would make
    "yesterday" two days ago. `now` is injectable so tests can pin the clock."""
    now = now or datetime.now(timezone.utc)
    _require_aware(now)
    return (now.astimezone(IST).date() - timedelta(days=1)).isoformat()


def parse_day(s: str) -> str:
    """Validate a YYYY-MM-DD day string and return it unchanged.

    Strict on purpose: fromisoformat would also accept "20260928", and newer Pythons
    accept other forms too. Days are compared as strings in SQL, so any other spelling
    would quietly match nothing.
    """
    if not _DAY_RE.fullmatch(s):
        raise ValueError(f"date must be YYYY-MM-DD, got {s!r}")
    return date.fromisoformat(s).isoformat()  # also rejects 2026-02-30
