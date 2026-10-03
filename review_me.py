"""
review_me.py: written by an AI coding assistant in one shot and merged without review.

Your job (write it in REVIEW.md):
  1. Find at least 5 real problems, most serious first. For each, say what goes wrong,
     with a concrete example (not just "bad practice").
  2. Fix the 2-3 most serious ones in this file.
Don't rewrite it from scratch. Reviewing is the skill being tested.
"""
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import requests

PORTAL = "http://127.0.0.1:8765"
HEADERS = {"X-Api-Key": "dfhire-2026"}
TIMEOUT_S = 5
MAX_ATTEMPTS = 5


class IncompleteData(Exception):
    """This store's data can't be trusted for this run, so it must not be saved."""


def get_page(store_id, as_of, cursor):
    """One inventory page with bounded retries (FIX 2: this used to retry forever,
    0.1s apart, on every error, including 400/401 that can never succeed)."""
    for attempt in range(MAX_ATTEMPTS):
        time.sleep(0.5)  # stay well under the portal's burst and fair-use limits
        try:
            r = requests.get(
                f"{PORTAL}/v1/stores/{store_id}/inventory",
                params={"as_of": as_of, "cursor": cursor},
                headers=HEADERS,
                timeout=TIMEOUT_S,
            )
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            continue
        if 400 <= r.status_code < 500:
            r.raise_for_status()  # our request is wrong; retrying can't fix it
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        return r.json()
    raise IncompleteData(f"{store_id}: gave up after {MAX_ATTEMPTS} attempts")


def fetch_inventory(store_id, as_of):
    """Fetch every inventory page for a store.

    FIX 1: items go into a fresh dict per call. The old shared default list made
    MUM-002's rows include MUM-001's. A loop replaces the recursion that relied on it.
    FIX 3: a 200 isn't trusted blindly. A degraded (edge) or partial page means the
    store is incomplete, and repeated items across pages are kept once (by sku_id).
    """
    items = {}
    cursor = "0"
    while cursor is not None:
        body = get_page(store_id, as_of, cursor)
        if (body.get("meta") or {}).get("source") != "origin":
            raise IncompleteData(f"{store_id}: degraded response (meta.source != 'origin')")
        if body.get("partial"):
            raise IncompleteData(f"{store_id}: portal returned partial snapshot")
        for it in body["items"]:
            items.setdefault(it["sku_id"], it)
        cursor = body["next_cursor"]
    return list(items.values())


def save(conn, store_id, items):
    for it in items:
        conn.execute(
            f"INSERT INTO inventory VALUES ('{store_id}', '{it['sku_id']}', '{it['name']}', "
            f"{int(it['in_stock'])}, {it['qty']}, '{it['observed_at']}')"
        )
    conn.commit()


def city_osa(conn, city, day=None):
    """On-shelf availability for a city on a day. Defaults to yesterday."""
    day = day or (date.today() - timedelta(days=1)).isoformat()
    stores = [r[0] for r in conn.execute(
        "SELECT store_id FROM stores WHERE city = ?", (city,))]
    per_store = []
    for s in stores:
        rows = conn.execute(
            "SELECT qty FROM inventory WHERE store_id = ? AND substr(observed_at, 1, 10) = ?",
            (s, day),
        ).fetchall()
        in_stock = sum(1 for (qty,) in rows if qty > 0)
        per_store.append(in_stock / len(rows) if rows else 0.0)
    return round(100 * sum(per_store) / len(per_store), 2)


if __name__ == "__main__":
    conn = sqlite3.connect("osa.db")
    conn.execute("CREATE TABLE IF NOT EXISTS stores (store_id TEXT, city TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS inventory (store_id TEXT, sku_id TEXT, name TEXT, "
                 "in_stock INT, qty INT, observed_at TEXT)")
    as_of = datetime.now(timezone.utc).isoformat(timespec="seconds")  # FIX 2: was naive -> 400
    for sid in ["MUM-001", "MUM-002"]:
        try:
            save(conn, sid, fetch_inventory(sid, as_of))
        except IncompleteData as e:
            print(f"not saved: {e}")
    print(city_osa(conn, "Mumbai"))
