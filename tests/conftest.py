"""Shared fixtures. Nothing here talks to the real portal: the DB is a temp file and
HTTP goes through a scripted fake session."""
import sqlite3
from collections.abc import Iterable
from typing import Any

import pytest

import db
import sweep


# ------------------------------------------------------------------------------ DB

@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(db_path) -> Iterable[sqlite3.Connection]:
    c = db.connect(db_path)
    yield c
    c.close()


class Seed:
    """Writes test data straight into the schema, so report tests can set up exactly
    the case they guard without going through the scraper."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def store(self, store_id: str, city: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO stores VALUES (?, ?, ?, 1, 1, '2026-10-01T00:00:00Z')",
                (store_id, city, store_id))

    def sweep(self, as_of: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO sweeps VALUES (?, ?, '2026-10-01T00:00:00Z', NULL)",
                (as_of, db.ist_date(db.to_utc(as_of))))

    def store_sweep(self, as_of: str, store_id: str, city: str = "Mumbai",
                    status: str = "complete", reason: str | None = None,
                    items: Iterable[tuple] = ()) -> None:
        """items: (sku_id, in_stock[, qty[, name]]). Rows are written even for a
        non-complete status, so tests can prove the report ignores them."""
        self.store(store_id, city)
        self.sweep(as_of)
        items = list(items)
        with self.conn:
            self.conn.execute("INSERT INTO store_sweeps VALUES (?, ?, ?, ?, ?, ?)",
                              (as_of, store_id, status, reason,
                               len(items) if status == "complete" else None, as_of))
            for it in items:
                sku, in_stock, qty, name = (*it, None, None)[:4]
                self.conn.execute(
                    "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, 10.0, ?)",
                    (as_of, store_id, sku, name or sku, int(in_stock),
                     qty if qty is not None else (5 if in_stock else 0), as_of))


@pytest.fixture
def seed(conn) -> Seed:
    return Seed(conn)


# ---------------------------------------------------------------------------- HTTP

class Resp:
    """Just enough of requests.Response for Portal.get_json."""

    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self) -> Any:
        return self._body


def page(store_id: str, items: list[dict], next_cursor: str | None = None,
         partial: bool = False, source: str = "origin") -> Resp:
    return Resp(200, {"store_id": store_id, "items": items, "partial": partial,
                      "next_cursor": next_cursor,
                      "meta": {"source": source, "generated_at": "2026-10-01T00:00:00Z"}})


def item(sku: str, in_stock: bool = True, qty: int = 5, price: Any = 10.0,
         observed_at: str = "2026-09-28T04:37:00Z", name: str | None = None) -> dict:
    return {"sku_id": sku, "name": name or f"Name {sku}", "in_stock": in_stock, "qty": qty,
            "price": price, "observed_at": observed_at}


class FakeSession:
    """Returns scripted responses in order and records every request. An exception
    instance in the script is raised instead (e.g. requests.Timeout())."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {url} {params}")
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def close(self) -> None:
        pass


class _FixedRng:
    """random() == 0.5 makes the jitter factor exactly 1.0, so backoffs are 1, 2, 4..."""

    def random(self) -> float:
        return 0.5


@pytest.fixture
def make_portal():
    """make_portal(responses) -> (portal, session, sleeps). Sleeping only advances a
    fake clock, so soft-ban pauses of 30-120 s cost nothing."""

    def _make(responses: list[Any], rate: float = 2.0):
        now = [0.0]
        sleeps: list[float] = []

        def sleep(s: float) -> None:
            sleeps.append(s)
            now[0] += s

        session = FakeSession(responses)
        portal = sweep.Portal("http://portal.test", rate=rate, session=session,
                              sleep=sleep, clock=lambda: now[0], rng=_FixedRng())
        return portal, session, sleeps

    return _make
