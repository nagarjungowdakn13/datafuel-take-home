"""One sweep: the inventory of every tracked QuickMart store at one timestamp, into SQLite.

    python sweep.py --as-of 2026-09-28T04:30:00Z

The portal misbehaves on purpose (see OBSERVATIONS.md). The design rule throughout is
the brief's golden rule: when we can't be sure a store's listing is complete and real,
we record the store as incomplete with a reason, and save none of its items, rather
than save a guess.
"""
import argparse
import logging
import random
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

import db

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
API_KEY = "dfhire-2026"

TIMEOUT_S = 5.0            # slow responses take ~8 s; abandoning them and retrying is faster
MAX_ATTEMPTS = 6           # per request, for timeouts / network errors / 5xx
MAX_429_WAITS = 10         # 429s don't use up attempts, but we don't wait forever either
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_INVENTORY_PAGES = 50   # ~36 SKUs / 15 per page = 3 pages; 50 means something is looping
MAX_STORE_PAGES = 100
SOFT_BAN_PAUSES_S = (30.0, 60.0, 120.0)

log = logging.getLogger("sweep")


# ---------------------------------------------------------------------------- errors

class FetchError(Exception):
    """A request we gave up on. str(e) is human-readable and is stored as the DB reason."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


class SoftBanned(Exception):
    """A page that came back 200 but is degraded fair-use output, not real data."""


class StoreIncomplete(Exception):
    """The store's data for this sweep can't be trusted. str(e) is the DB reason."""


# ------------------------------------------------------------------------------ HTTP

class Pacer:
    """Spaces requests at least 1/rate seconds apart.

    It's called before every request, retries included, because the soft ban is
    triggered by sustained volume, and retries count toward volume just like
    first attempts.
    """

    def __init__(self, rate: float, sleep: Callable[[float], None],
                 clock: Callable[[], float]):
        self.interval = 1.0 / rate
        self.sleep = sleep
        self.clock = clock
        self._next_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._next_at is not None and now < self._next_at:
            self.sleep(self._next_at - now)
            now = self._next_at
        self._next_at = now + self.interval


class Portal:
    """Thin client for the QuickMart portal. Every request goes through get_json().

    session, sleep, clock and rng can be injected so tests can drive the retry and
    soft-ban logic with fake responses and without real waiting.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, rate: float = 2.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: random.Random | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.pacer = Pacer(rate, sleep, clock)
        self.rng = rng or random.Random()

    def get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET a JSON object, retrying only what retrying can fix.

        Timeouts, network errors and 5xx are transient (we saw a random 503 and
        MUM-007's 500, 500, 200), so they're retried with exponential backoff and
        jitter. 429 means "wait", not "broken", so it doesn't use up an attempt.
        4xx means our request is wrong, and retrying would just repeat it.
        """
        url = f"{self.base_url}{path}"
        attempts = 0
        waits_429 = 0
        while True:
            self.pacer.wait()
            try:
                r = self.session.get(url, params=params, headers={"X-Api-Key": API_KEY},
                                     timeout=TIMEOUT_S)
            except requests.Timeout:
                problem = f"timed out after {TIMEOUT_S:g}s"
            except requests.RequestException as e:
                problem = f"network error ({type(e).__name__})"
            else:
                if r.status_code == 200:
                    try:
                        body = r.json()
                    except ValueError:
                        problem = "HTTP 200 with invalid JSON"
                    else:
                        if isinstance(body, dict):
                            return body
                        problem = "HTTP 200 with a non-object JSON body"
                elif r.status_code == 429:
                    waits_429 += 1
                    if waits_429 > MAX_429_WAITS:
                        raise FetchError(f"rate limited (429) {waits_429} times", 429)
                    delay = _retry_after(r)
                    log.debug("429 on %s, waiting %.1fs (Retry-After)", path, delay)
                    self.sleep(delay)
                    continue
                elif r.status_code in RETRYABLE_STATUS:
                    problem = f"HTTP {r.status_code}"
                else:
                    raise FetchError(f"HTTP {r.status_code}: {_error_text(r)}", r.status_code)

            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                raise FetchError(f"gave up after {attempts} attempts (last: {problem})")
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempts - 1)) * (0.5 + self.rng.random())
            log.debug("%s on %s (attempt %d/%d), retrying in %.1fs",
                      problem, path, attempts, MAX_ATTEMPTS, delay)
            self.sleep(delay)

    def store_page(self, page: str) -> dict[str, Any]:
        return self.get_json("/v1/stores", {"page": page})

    def inventory_page(self, store_id: str, as_of: str, cursor: str) -> dict[str, Any]:
        return self.get_json(f"/v1/stores/{store_id}/inventory",
                             {"as_of": as_of, "cursor": cursor})


def _retry_after(r: requests.Response) -> float:
    """Retry-After in seconds. Falls back to 2s when it's missing or malformed, and
    is capped so a bad header can't stall the sweep."""
    try:
        return min(60.0, max(0.0, float(r.headers.get("Retry-After", "2"))))
    except ValueError:
        return 2.0


def _error_text(r: requests.Response) -> str:
    try:
        return str(r.json().get("error", r.text[:100]))
    except (ValueError, AttributeError):
        return r.text[:100]


# ---------------------------------------------------------------------------- stores

def fetch_roster(portal: Portal) -> list[dict[str, Any]]:
    """All stores from every /v1/stores page. The roster spans 3 pages; reading only
    the first would silently drop Bengaluru."""
    stores: list[dict[str, Any]] = []
    page: Any = 1
    seen: set[Any] = set()
    while page is not None:
        if page in seen or len(seen) >= MAX_STORE_PAGES:
            raise FetchError(f"store list pagination did not end (page {page!r})")
        seen.add(page)
        body = portal.store_page(str(page))
        if not isinstance(body.get("stores"), list):
            raise FetchError("store list response has no 'stores' list")
        stores.extend(body["stores"])
        page = body.get("next_page")
    for s in stores:
        if not isinstance(s.get("store_id"), str) or not isinstance(s.get("city"), str) \
                or type(s.get("is_active")) is not bool or type(s.get("is_serviceable")) is not bool:
            raise FetchError(f"malformed store in roster: {s!r}")
    return stores


def tracked_stores(stores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Track every store with is_active, whatever is_serviceable says.

    is_serviceable means "accepting orders right now" and flips during the day (rain,
    maintenance). The shelf, and so on-shelf availability, still exists while a store
    is briefly unserviceable, and the roster only gives today's flag, not the flag at
    the sweep time. is_active=false means the store is shut down. An inactive store
    that claims to be "serviceable" (MUM-009, BLR-004) has a stale flag, not live shelves.
    """
    return [s for s in stores if s["is_active"]]


# ------------------------------------------------------------------------- inventory

def normalise_item(raw: Any) -> dict[str, Any]:
    """Turn one portal item into a DB row, or raise StoreIncomplete if it can't be trusted.

    in_stock decides OSA, so it must be a real JSON boolean. A string "false" or a 0/1
    could be read either way, and reading it is a guess. qty is only kept as information
    (it disagrees with in_stock: in_stock true with qty 0). price arrives as a string for
    Bengaluru. An unreadable price becomes NULL, since OSA doesn't use it.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("sku_id"), str) or not raw["sku_id"]:
        raise StoreIncomplete(f"item without a valid sku_id: {raw!r:.80}")
    sku = raw["sku_id"]
    if type(raw.get("in_stock")) is not bool:
        raise StoreIncomplete(f"{sku}: in_stock is not a boolean ({raw.get('in_stock')!r})")
    try:
        observed_at = db.utc_str(db.to_utc(str(raw.get("observed_at"))))
    except ValueError:
        raise StoreIncomplete(f"{sku}: unreadable observed_at ({raw.get('observed_at')!r})") from None
    qty = raw.get("qty")
    return {
        "sku_id": sku,
        "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
        "in_stock": int(raw["in_stock"]),
        "qty": qty if type(qty) is int else None,
        "price": _price(raw.get("price")),
        "observed_at": observed_at,
    }


def _price(v: Any) -> float | None:
    if type(v) in (int, float):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def check_page(body: dict[str, Any], store_id: str, page_no: int) -> None:
    """Raise SoftBanned if this 200 response is degraded output rather than real data.

    A degraded page still looks final (next_cursor null, partial false), so the signs
    are: it wasn't served by origin, it's for another store, or it's an empty page
    partway through a listing (a real listing never ends with an empty page; it just
    returns next_cursor null on its last full page).
    """
    source = (body.get("meta") or {}).get("source")
    if source != "origin":
        raise SoftBanned(f"meta.source={source!r} on page {page_no + 1}")
    if body.get("store_id") != store_id:
        raise SoftBanned(f"response is for store {body.get('store_id')!r} on page {page_no + 1}")
    if page_no > 0 and body.get("items") == []:
        raise SoftBanned(f"empty page {page_no + 1} in the middle of a listing")


def fetch_store_once(portal: Portal, store_id: str, as_of: str) -> list[dict[str, Any]]:
    """Read every page of one store's inventory, start to finish, once.

    Items are keyed by sku_id because pages repeat the previous page's last item. A
    repeat that disagrees on in_stock means we can't know which is right, so the
    store is incomplete.
    """
    items: dict[str, dict[str, Any]] = {}
    cursor = "0"
    used: set[str] = set()
    for page_no in range(MAX_INVENTORY_PAGES):
        body = portal.inventory_page(store_id, as_of, cursor)
        check_page(body, store_id, page_no)
        if body.get("partial") is not False:
            if body.get("partial") is True:
                raise StoreIncomplete("portal returned partial snapshot")
            raise StoreIncomplete(f"unexpected partial value {body.get('partial')!r}")
        if not isinstance(body.get("items"), list):
            raise StoreIncomplete(f"page {page_no + 1} has no 'items' list")
        for raw in body["items"]:
            item = normalise_item(raw)
            prev = items.get(item["sku_id"])
            if prev is not None and prev["in_stock"] != item["in_stock"]:
                raise StoreIncomplete(f"{item['sku_id']} listed twice with different in_stock")
            items.setdefault(item["sku_id"], item)
        nxt = body.get("next_cursor")
        if nxt is None:
            return list(items.values())
        used.add(cursor)
        cursor = str(nxt)
        if cursor in used:
            raise StoreIncomplete(f"cursor did not advance (got {cursor!r} again)")
    raise StoreIncomplete(f"more than {MAX_INVENTORY_PAGES} pages")


@dataclass
class StoreResult:
    status: str                      # 'complete' or 'incomplete'
    reason: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)


def scrape_store(portal: Portal, store_id: str, as_of: str) -> StoreResult:
    """One store, with soft-ban recovery.

    On a soft ban, everything read for the store is thrown away. Earlier pages may
    have been fine, but we can't prove where the degradation started. Then we send
    nothing for 30s / 60s / 120s, since requests made while degraded may extend it.
    Then we restart the store from cursor 0. The pause also runs after the last
    failed round, so the next store doesn't start inside a ban.
    """
    last = ""
    for round_no, pause in enumerate(SOFT_BAN_PAUSES_S, start=1):
        try:
            return StoreResult("complete", items=fetch_store_once(portal, store_id, as_of))
        except SoftBanned as e:
            last = str(e)
            log.warning("WARNING %s soft-banned (%s); discarding its data, no requests for %.0fs "
                        "(round %d/%d)", store_id, e, pause, round_no, len(SOFT_BAN_PAUSES_S))
            portal.sleep(pause)
        except StoreIncomplete as e:
            return StoreResult("incomplete", str(e))
        except FetchError as e:
            if e.status in (401, 403):
                raise  # wrong key/permissions: every store would fail the same way
            return StoreResult("incomplete", str(e))
    return StoreResult("incomplete", f"soft_ban: degraded responses on {len(SOFT_BAN_PAUSES_S)} "
                                     f"attempts (last: {last})")


# -------------------------------------------------------------------------- database

def upsert_stores(conn: sqlite3.Connection, stores: list[dict[str, Any]]) -> None:
    """Keep the roster as last seen, so reports can map stores to cities."""
    now = db.now_utc_str()
    with conn:
        conn.executemany(
            """INSERT INTO stores (store_id, city, name, is_active, is_serviceable, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (store_id) DO UPDATE SET city = excluded.city, name = excluded.name,
                   is_active = excluded.is_active, is_serviceable = excluded.is_serviceable,
                   updated_at = excluded.updated_at""",
            [(s["store_id"], s["city"], s.get("name"), int(s["is_active"]),
              int(s["is_serviceable"]), now) for s in stores])


def start_sweep(conn: sqlite3.Connection, as_of: str, store_ids: list[str]) -> None:
    """Register the sweep and a 'pending' row for every tracked store before any fetching.

    If the run dies, the unfinished stores remain visible as pending (= not complete)
    instead of missing. Existing rows are left alone, so a re-run doesn't wipe
    results it hasn't replaced yet.
    """
    with conn:
        conn.execute(
            """INSERT INTO sweeps (as_of, ist_date, started_at, finished_at) VALUES (?, ?, ?, NULL)
               ON CONFLICT (as_of) DO UPDATE SET started_at = excluded.started_at,
                   finished_at = NULL""",
            (as_of, db.ist_date(db.to_utc(as_of)), db.now_utc_str()))
        conn.executemany(
            """INSERT INTO store_sweeps (as_of, store_id, status) VALUES (?, ?, 'pending')
               ON CONFLICT (as_of, store_id) DO NOTHING""",
            [(as_of, sid) for sid in store_ids])


def save_store_result(conn: sqlite3.Connection, as_of: str, store_id: str,
                      result: StoreResult) -> bool:
    """Replace one store's data for one sweep in a single transaction.

    Delete-then-insert makes re-runs idempotent (no duplicates, no leftovers from an
    earlier run), and one transaction means a crash never leaves half a store. A
    failed re-run must not erase a good earlier result: returns False and keeps the
    old one when an incomplete result would overwrite a complete one.
    """
    with conn:
        row = conn.execute("SELECT status FROM store_sweeps WHERE as_of = ? AND store_id = ?",
                           (as_of, store_id)).fetchone()
        if result.status == "incomplete" and row is not None and row["status"] == "complete":
            return False
        conn.execute(
            """INSERT INTO store_sweeps (as_of, store_id, status, reason, item_count, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (as_of, store_id) DO UPDATE SET status = excluded.status,
                   reason = excluded.reason, item_count = excluded.item_count,
                   fetched_at = excluded.fetched_at""",
            (as_of, store_id, result.status, result.reason,
             len(result.items) if result.status == "complete" else None, db.now_utc_str()))
        conn.execute("DELETE FROM observations WHERE as_of = ? AND store_id = ?", (as_of, store_id))
        conn.executemany(
            """INSERT INTO observations (as_of, store_id, sku_id, name, in_stock, qty, price,
                                         observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(as_of, store_id, it["sku_id"], it["name"], it["in_stock"], it["qty"], it["price"],
              it["observed_at"]) for it in result.items])
    return True


def finish_sweep(conn: sqlite3.Connection, as_of: str) -> None:
    with conn:
        conn.execute("UPDATE sweeps SET finished_at = ? WHERE as_of = ?", (db.now_utc_str(), as_of))


# ----------------------------------------------------------------------------- sweep

def run_sweep(conn: sqlite3.Connection, portal: Portal, as_of: str) -> None:
    """Roster → pending rows → each tracked store in turn, one at a time.

    Stores run sequentially on purpose. The rate limits are per API key, so
    parallelism would only get us banned sooner.
    """
    roster = fetch_roster(portal)
    upsert_stores(conn, roster)            # the whole roster, so untracked stores are explainable
    tracked = tracked_stores(roster)
    start_sweep(conn, as_of, [s["store_id"] for s in tracked])

    width = len(str(len(tracked)))
    for i, store in enumerate(tracked, start=1):
        sid = store["store_id"]
        result = scrape_store(portal, sid, as_of)
        prefix = f"[{i:>{width}}/{len(tracked)}] {sid}"
        if not save_store_result(conn, as_of, sid, result):
            log.warning("WARNING %s incomplete this run (%s); keeping the earlier complete result",
                        prefix, result.reason)
        elif result.status == "complete":
            log.info("%s complete (%d items)", prefix, len(result.items))
        else:
            log.info("%s incomplete: %s", prefix, result.reason)
    finish_sweep(conn, as_of)


def summary(conn: sqlite3.Connection, as_of: str, elapsed_s: float) -> str:
    """The final line, read back from the DB so it describes what's stored (including
    complete results kept from an earlier run), not just what this run did."""
    rows = conn.execute(
        "SELECT store_id, status, reason FROM store_sweeps WHERE as_of = ? ORDER BY store_id",
        (as_of,)).fetchall()
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("complete", "incomplete", "pending")}
    parts = [f"{counts['complete']} complete"]
    bad = [f"{r['store_id']}: {r['reason']}" for r in rows if r["status"] == "incomplete"]
    parts.append(f"{counts['incomplete']} incomplete" + (f" ({'; '.join(bad)})" if bad else ""))
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    m, s = divmod(int(round(elapsed_s)), 60)
    return f"{len(rows)} stores: {', '.join(parts)} · {m}m{s:02d}s"


# ------------------------------------------------------------------------------- CLI

class _Formatter(logging.Formatter):
    """Store lines print as-is (warnings carry their own "WARNING" word); -v debug lines
    (retries, 429s) are indented so the per-store lines stay easy to scan."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return f"  {msg}" if record.levelno == logging.DEBUG else msg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one QuickMart inventory sweep into SQLite.")
    p.add_argument("--as-of", required=True,
                   help="sweep timestamp, ISO-8601 with timezone, e.g. 2026-09-28T04:30:00Z")
    p.add_argument("--db", default=None, help="SQLite file (default: $OSA_DB or osa.db)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--rate", type=float, default=2.0, help="max requests per second (default 2)")
    p.add_argument("-v", "--verbose", action="store_true", help="also log every retry and 429")
    args = p.parse_args(argv)
    try:
        # Canonical UTC: 04:30Z and 10:00+05:30 are the same sweep, and must be the same key.
        args.as_of = db.utc_str(db.to_utc(args.as_of))
    except ValueError as e:
        p.error(f"--as-of: {e}")
    if args.rate <= 0:
        p.error("--rate must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Some Windows consoles can't encode "·"; a summary line must never crash the sweep.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    conn = db.connect(args.db)
    portal = Portal(args.base_url, rate=args.rate)
    started = time.monotonic()
    log.info("Sweep %s (IST day %s)", args.as_of, db.ist_date(db.to_utc(args.as_of)))
    try:
        run_sweep(conn, portal, args.as_of)
        log.info(summary(conn, args.as_of, time.monotonic() - started))
        return 0
    except KeyboardInterrupt:
        # Each store is saved in its own transaction, so stopping here loses at most the
        # store in progress. Unfinished stores stay 'pending' and count as not complete.
        log.info("Interrupted. Stores not finished are left 'pending'.")
        log.info(summary(conn, args.as_of, time.monotonic() - started))
        return 130
    except FetchError as e:
        log.error("Sweep aborted: %s", e)
        return 1
    finally:
        portal.session.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
