"""OSA (on-shelf availability) report for one city on one IST day.

Pure logic over the SQLite database that sweep.py fills: it never calls the portal,
so the API's numbers depend only on what was stored, and tests can build a tiny DB.

Counting rule: an observation counts only if its store-sweep is 'complete'. Anything
we couldn't read properly is listed in coverage, never turned into "out of stock".
"""
import sqlite3
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import db

CITIES = ("Mumbai", "Delhi", "Bengaluru")


class ReportError(ValueError):
    """Bad input from the caller. The message is shown to them as-is (HTTP 400)."""


def pct(in_stock: int, observations: int) -> float | None:
    """in_stock / observations as a percentage, rounded half-up to 2 decimals.

    Decimal, because round() on a float rounds half-to-even and is fooled by binary
    representation (e.g. 85.125 can become 85.12). None when there's nothing to
    divide: an empty denominator means "unknown", never 0%.
    """
    if observations == 0:
        return None
    value = Decimal(in_stock) * 100 / Decimal(observations)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# The join that every count goes through. Explained in build_report's docstring.
_COUNTED = """
    FROM observations o
    JOIN store_sweeps ss ON ss.as_of = o.as_of AND ss.store_id = o.store_id
                        AND ss.status = 'complete'
    JOIN sweeps sw       ON sw.as_of = o.as_of
    JOIN stores st       ON st.store_id = o.store_id
    WHERE sw.ist_date = ? AND st.city = ?
"""


def build_report(conn: sqlite3.Connection, city: str | None, day: str | None = None,
                 now: datetime | None = None) -> dict[str, Any]:
    """Build the /osa response for `city` on IST day `day` (default: yesterday in IST).

    How only complete store-sweeps are counted: every observation is joined to its
    own store_sweeps row on (as_of, store_id), and the join requires status =
    'complete'. Rows of incomplete or pending store-sweeps then drop out of the join.
    sweep.py never saves items for them anyway, so this is a second safeguard: the
    report itself refuses them. Sweeps are picked by sweeps.ist_date (the sweep's
    as_of in IST), never by each item's observed_at. The city comes from the stores table.

    stores_expected = the city's stores that the day's sweeps tracked (they have
    store_sweeps rows). A store is in stores_complete only if ALL its store-sweeps that
    day are complete. store_sweeps_expected/complete count (sweep, store) pairs.
    """
    if city not in CITIES:
        raise ReportError(f"city must be one of {', '.join(CITIES)}; got {city!r}")
    if day is None:
        day = db.yesterday_in_ist(now)
    else:
        try:
            day = db.parse_day(day)
        except ValueError as e:
            raise ReportError(str(e)) from None

    sweep_ids = [r["as_of"] for r in conn.execute(
        "SELECT as_of FROM sweeps WHERE ist_date = ? ORDER BY as_of", (day,))]
    ledger = conn.execute(
        """SELECT ss.as_of, ss.store_id, ss.status, ss.reason, ss.item_count
           FROM store_sweeps ss
           JOIN sweeps sw ON sw.as_of = ss.as_of
           JOIN stores st ON st.store_id = ss.store_id
           WHERE sw.ist_date = ? AND st.city = ?
           ORDER BY ss.as_of, ss.store_id""", (day, city)).fetchall()

    skus = _sku_rows(conn, day, city)
    per_sweep = {r["as_of"]: r for r in conn.execute(
        f"SELECT o.as_of, COUNT(*) AS n, SUM(o.in_stock) AS k {_COUNTED} GROUP BY o.as_of",
        (day, city))}
    total_obs = sum(s["observations"] for s in skus)
    total_in = sum(s["in_stock"] for s in skus)

    coverage = _coverage(ledger)
    if not sweep_ids or total_obs == 0:
        status = "no_data"
    elif coverage["incomplete"]:
        status = "partial"
    else:
        status = "ok"

    return {
        "city": city,
        "date": day,
        "status": status,
        "osa_pct": pct(total_in, total_obs),
        "observations": total_obs,
        "in_stock": total_in,
        "coverage": coverage,
        "sweeps": [_sweep_entry(a, ledger, per_sweep.get(a)) for a in sweep_ids],
        "skus": skus,
        "available_dates": [r["ist_date"] for r in conn.execute(
            "SELECT DISTINCT ist_date FROM sweeps ORDER BY ist_date")],
    }


def _sku_rows(conn: sqlite3.Connection, day: str, city: str) -> list[dict[str, Any]]:
    """Per-SKU counts, grouped by sku_id (names change: SKU-0001 was renamed on 28 Sep).
    The display name is the one from the latest sweep that day."""
    names: dict[str, str] = {}
    for r in conn.execute(
            f"SELECT o.sku_id, o.name {_COUNTED} AND o.name IS NOT NULL "
            "ORDER BY o.as_of, o.store_id", (day, city)):
        names[r["sku_id"]] = r["name"]  # later rows overwrite: the latest name wins
    rows = conn.execute(
        f"SELECT o.sku_id, COUNT(*) AS n, SUM(o.in_stock) AS k {_COUNTED} "
        "GROUP BY o.sku_id ORDER BY o.sku_id", (day, city))
    return [{"sku_id": r["sku_id"], "name": names.get(r["sku_id"]), "observations": r["n"],
             "in_stock": r["k"], "osa_pct": pct(r["k"], r["n"])} for r in rows]


def _coverage(ledger: list[sqlite3.Row]) -> dict[str, Any]:
    """How complete the day's data is. Incomplete and pending store-sweeps are listed
    with their reason. Complete-but-empty ones (BLR-007 before launch) are listed too:
    they count as complete yet contribute no observations, which a reader should see."""
    stores = {r["store_id"] for r in ledger}
    bad = [r for r in ledger if r["status"] != "complete"]
    bad_stores = {r["store_id"] for r in bad}
    return {
        "stores_expected": len(stores),
        "stores_complete": len(stores - bad_stores),
        "store_sweeps_expected": len(ledger),
        "store_sweeps_complete": len(ledger) - len(bad),
        "incomplete": [{"store_id": r["store_id"], "sweep": r["as_of"],
                        "reason": r["reason"] or r["status"]} for r in bad],
        "complete_but_empty": [{"store_id": r["store_id"], "sweep": r["as_of"]}
                               for r in ledger
                               if r["status"] == "complete" and r["item_count"] == 0],
    }


def _sweep_entry(as_of: str, ledger: list[sqlite3.Row], counted: sqlite3.Row | None) -> dict[str, Any]:
    rows = [r for r in ledger if r["as_of"] == as_of]
    n = counted["n"] if counted else 0
    k = counted["k"] if counted else 0
    return {
        "as_of": as_of,
        "ist_time": db.to_utc(as_of).astimezone(db.IST).isoformat(timespec="minutes"),
        "stores_expected": len(rows),
        "stores_complete": sum(1 for r in rows if r["status"] == "complete"),
        "observations": n,
        "in_stock": k,
        "osa_pct": pct(k, n),
    }
