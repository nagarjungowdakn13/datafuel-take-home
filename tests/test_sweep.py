"""Scraper tests with a scripted fake session and fake sleep: instant, no portal needed."""
import pytest
import requests

import sweep
from conftest import Resp, item, page

AS_OF = "2026-09-28T04:30:00Z"


def _setup(conn, store_id: str, city: str = "Mumbai") -> None:
    sweep.upsert_stores(conn, [{"store_id": store_id, "city": city, "name": store_id,
                                "is_active": True, "is_serviceable": True}])
    sweep.start_sweep(conn, AS_OF, [store_id])


def _rows(conn, store_id: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT sku_id, in_stock FROM observations WHERE as_of = ? AND store_id = ? ORDER BY sku_id",
        (AS_OF, store_id))]


def _status(conn, store_id: str) -> tuple:
    r = conn.execute("SELECT status, reason, item_count FROM store_sweeps WHERE as_of = ? AND store_id = ?",
                     (AS_OF, store_id)).fetchone()
    return tuple(r)


def _long_sleeps(sleeps: list[float]) -> list[float]:
    """Drop the pacer's sub-second gaps; keep backoffs, Retry-After and soft-ban pauses."""
    return [s for s in sleeps if s >= 1.0]


# --------------------------------------------------------------------- soft ban

def test_edge_page_mid_store_discards_everything_and_restarts(conn, make_portal):
    portal, session, sleeps = make_portal([
        page("MUM-001", [item("SKU-1"), item("SKU-2")], next_cursor="2"),
        page("MUM-001", [item("SKU-EDGE", in_stock=False)], source="edge"),   # degraded
        page("MUM-001", [item("SKU-1"), item("SKU-2")], next_cursor="2"),    # restart from 0
        page("MUM-001", [item("SKU-3")]),
    ])
    _setup(conn, "MUM-001")
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    sweep.save_store_result(conn, AS_OF, "MUM-001", result)

    assert [c[1]["cursor"] for c in session.calls] == ["0", "2", "0", "2"]
    assert _long_sleeps(sleeps) == [30.0]
    assert _rows(conn, "MUM-001") == [("SKU-1", 1), ("SKU-2", 1), ("SKU-3", 1)]   # no SKU-EDGE
    assert _status(conn, "MUM-001") == ("complete", None, 3)


def test_soft_ban_that_never_ends_marks_store_incomplete(conn, make_portal):
    banned = page("MUM-001", [item("SKU-1")], source="edge")
    portal, session, sleeps = make_portal([banned, banned, banned])
    _setup(conn, "MUM-001")
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    sweep.save_store_result(conn, AS_OF, "MUM-001", result)

    assert result.status == "incomplete"
    assert result.reason.startswith("soft_ban")
    assert _long_sleeps(sleeps) == [30.0, 60.0, 120.0]
    assert len(session.calls) == 3
    assert _rows(conn, "MUM-001") == []


@pytest.mark.parametrize("bad_page", [
    page("MUM-001", [item("SKU-3")], source=None),                    # meta.source missing
    page("MUM-002", [item("SKU-3")]),                                 # someone else's store
    page("MUM-001", []),                                              # empty page mid-listing
])
def test_other_soft_ban_signs_on_later_pages(make_portal, bad_page):
    portal, _, sleeps = make_portal([
        page("MUM-001", [item("SKU-1")], next_cursor="1"), bad_page,
        page("MUM-001", [item("SKU-1")], next_cursor="1"), page("MUM-001", [item("SKU-3")]),
    ])
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    assert result.status == "complete"
    assert _long_sleeps(sleeps) == [30.0]


# ------------------------------------------------------------- incomplete data

def test_partial_snapshot_is_incomplete_with_no_rows(conn, make_portal):
    portal, _, _ = make_portal([page("DEL-004", [item("SKU-1")], partial=True)])
    _setup(conn, "DEL-004", "Delhi")
    result = sweep.scrape_store(portal, "DEL-004", AS_OF)
    sweep.save_store_result(conn, AS_OF, "DEL-004", result)

    assert _status(conn, "DEL-004") == ("incomplete", "portal returned partial snapshot", None)
    assert _rows(conn, "DEL-004") == []


def test_non_boolean_in_stock_is_not_guessed(make_portal):
    portal, _, _ = make_portal([page("MUM-001", [{**item("SKU-1"), "in_stock": "false"}])])
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    assert result.status == "incomplete"
    assert "in_stock" in result.reason


def test_conflicting_duplicate_makes_store_incomplete(make_portal):
    portal, _, _ = make_portal([
        page("MUM-001", [item("SKU-1", in_stock=True)], next_cursor="1"),
        page("MUM-001", [item("SKU-1", in_stock=False)]),
    ])
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    assert result.status == "incomplete"
    assert "SKU-1" in result.reason


def test_real_empty_store_is_complete_with_zero_items(make_portal):
    portal, _, _ = make_portal([page("BLR-007", [])])
    result = sweep.scrape_store(portal, "BLR-007", AS_OF)
    assert (result.status, result.items) == ("complete", [])


# -------------------------------------------------------------------- retries

def test_500_500_200_recovers(make_portal):
    portal, session, sleeps = make_portal([
        Resp(500, {"error": "internal error"}), Resp(500, {"error": "internal error"}),
        page("MUM-007", [item("SKU-1")]),
    ])
    result = sweep.scrape_store(portal, "MUM-007", AS_OF)
    assert result.status == "complete"
    assert len(session.calls) == 3
    assert _long_sleeps(sleeps) == [1.0, 2.0]          # exponential backoff (jitter fixed)


def test_400_is_not_retried(make_portal):
    portal, session, _ = make_portal([Resp(400, {"error": "as_of must include a timezone"})])
    with pytest.raises(sweep.FetchError) as e:
        portal.inventory_page("MUM-001", AS_OF, "0")
    assert e.value.status == 400
    assert len(session.calls) == 1
    assert "as_of must include a timezone" in str(e.value)


def test_429_waits_retry_after_and_does_not_use_an_attempt(make_portal):
    # Five 5xx failures plus two 429s: if a 429 used up an attempt, that's 7 > 6 and we'd give up.
    rl = Resp(429, {"error": "rate limited"}, {"Retry-After": "7"})
    portal, session, sleeps = make_portal(
        [rl, Resp(503), Resp(503), rl, Resp(503), Resp(503), Resp(503), page("MUM-001", [item("SKU-1")])])
    body = portal.inventory_page("MUM-001", AS_OF, "0")
    assert body["items"][0]["sku_id"] == "SKU-1"
    assert len(session.calls) == 8
    assert _long_sleeps(sleeps).count(7.0) == 2


def test_gives_up_after_max_attempts_on_timeouts(make_portal):
    portal, session, _ = make_portal([requests.Timeout()] * sweep.MAX_ATTEMPTS)
    result = sweep.scrape_store(portal, "MUM-001", AS_OF)
    assert result.status == "incomplete"
    assert "timed out" in result.reason
    assert len(session.calls) == sweep.MAX_ATTEMPTS


def test_pacer_spaces_every_request_including_retries(make_portal):
    portal, _, sleeps = make_portal([Resp(503), page("MUM-001", []), page("MUM-001", [])], rate=2.0)
    portal.inventory_page("MUM-001", AS_OF, "0")
    portal.inventory_page("MUM-001", AS_OF, "0")
    # 503 -> backoff 1.0 s (already longer than the 0.5 s slot, so no extra wait); then
    # two back-to-back requests must still be 0.5 s apart.
    assert sleeps == [1.0, 0.5]


# ------------------------------------------------------------------ normalising

def test_dedupe_and_normalise_timestamps_and_prices(make_portal):
    portal, _, _ = make_portal([
        page("DEL-001", [item("SKU-1", observed_at="2026-09-28T10:15:00+05:30"),
                         item("SKU-2", observed_at="2026-09-28T10:15:00+05:30")], next_cursor="2"),
        page("DEL-001", [item("SKU-2", observed_at="2026-09-28T10:15:00+05:30"),
                         item("SKU-3", price="443.00", observed_at="2026-09-28T04:41:00Z")]),
    ])
    result = sweep.scrape_store(portal, "DEL-001", AS_OF)
    by_sku = {it["sku_id"]: it for it in result.items}
    assert sorted(by_sku) == ["SKU-1", "SKU-2", "SKU-3"]
    assert by_sku["SKU-1"]["observed_at"] == "2026-09-28T04:45:00Z"
    assert by_sku["SKU-3"]["price"] == 443.0 and isinstance(by_sku["SKU-3"]["price"], float)


# ------------------------------------------------------------------ idempotency

def test_saving_twice_gives_same_rows_and_incomplete_rerun_keeps_complete(conn):
    _setup(conn, "MUM-001")
    good = sweep.StoreResult("complete", items=[
        sweep.normalise_item(item("SKU-1")), sweep.normalise_item(item("SKU-2", in_stock=False))])
    assert sweep.save_store_result(conn, AS_OF, "MUM-001", good)
    assert sweep.save_store_result(conn, AS_OF, "MUM-001", good)
    assert _rows(conn, "MUM-001") == [("SKU-1", 1), ("SKU-2", 0)]

    sweep.start_sweep(conn, AS_OF, ["MUM-001"])           # a re-run must not reset to pending
    assert _status(conn, "MUM-001") == ("complete", None, 2)

    bad = sweep.StoreResult("incomplete", "portal returned partial snapshot")
    assert sweep.save_store_result(conn, AS_OF, "MUM-001", bad) is False
    assert _status(conn, "MUM-001") == ("complete", None, 2)
    assert _rows(conn, "MUM-001") == [("SKU-1", 1), ("SKU-2", 0)]


def test_as_of_forms_give_same_key_and_naive_is_rejected():
    z = sweep.parse_args(["--as-of", "2026-09-28T04:30:00Z"]).as_of
    ist = sweep.parse_args(["--as-of", "2026-09-28T10:00:00+05:30"]).as_of
    assert z == ist == AS_OF
    with pytest.raises(SystemExit):
        sweep.parse_args(["--as-of", "2026-09-28T04:30:00"])
