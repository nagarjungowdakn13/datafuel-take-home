"""Report tests. Each one guards a way the /osa number could come out wrong."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app
import db
import report
import sweep
from conftest import item, page

S_27_1030 = "2026-09-27T10:30:00Z"   # 16:00 IST on 27 Sep
S_27_1900 = "2026-09-27T19:00:00Z"   # 00:30 IST on 28 Sep
S_28_0430 = "2026-09-28T04:30:00Z"   # 10:00 IST on 28 Sep
S_28_1840 = "2026-09-28T18:40:00Z"   # 00:10 IST on 29 Sep


def test_same_sku_twice_in_a_store_sweep_counts_once(conn, make_portal):
    # The portal repeats the previous page's last item on the next page. Scrape it,
    # save it, report on it: SKU-2 must be one observation, not two.
    portal, _, _ = make_portal([
        page("MUM-001", [item("SKU-1"), item("SKU-2", in_stock=False)], next_cursor="2"),
        page("MUM-001", [item("SKU-2", in_stock=False), item("SKU-3")]),
    ])
    sweep.upsert_stores(conn, [{"store_id": "MUM-001", "city": "Mumbai", "name": "x",
                                "is_active": True, "is_serviceable": True}])
    sweep.start_sweep(conn, S_28_0430, ["MUM-001"])
    sweep.save_store_result(conn, S_28_0430, "MUM-001",
                            sweep.scrape_store(portal, "MUM-001", S_28_0430))

    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert r["observations"] == 3          # not 4
    assert r["in_stock"] == 2
    assert r["osa_pct"] == 66.67           # 2/3, not 2/4 = 50.00


def test_incomplete_store_is_excluded_and_listed_in_coverage(seed, conn):
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True), ("SKU-2", True)])
    # Even if rows for an incomplete store-sweep somehow exist (all out of stock),
    # the report must ignore them rather than drag OSA down.
    seed.store_sweep(S_28_0430, "MUM-002", status="incomplete",
                     reason="portal returned partial snapshot",
                     items=[("SKU-1", False), ("SKU-2", False)])
    seed.store_sweep(S_28_0430, "MUM-003", status="pending")

    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert r["osa_pct"] == 100.0           # would be 50.0 if MUM-002's rows counted
    assert r["observations"] == 2
    assert r["status"] == "partial"
    assert r["coverage"]["stores_expected"] == 3
    assert r["coverage"]["stores_complete"] == 1
    assert r["coverage"]["incomplete"] == [
        {"store_id": "MUM-002", "sweep": S_28_0430, "reason": "portal returned partial snapshot"},
        {"store_id": "MUM-003", "sweep": S_28_0430, "reason": "pending"},
    ]


def test_sweeps_are_assigned_to_their_ist_day(seed, conn):
    # One SKU, a different stock state per sweep, so a wrong day assignment changes the number.
    seed.store_sweep(S_27_1030, "MUM-001", items=[("SKU-1", False)])
    seed.store_sweep(S_27_1900, "MUM-001", items=[("SKU-1", True)])
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True)])
    seed.store_sweep(S_28_1840, "MUM-001", items=[("SKU-1", False)])

    day27 = report.build_report(conn, "Mumbai", "2026-09-27")
    day28 = report.build_report(conn, "Mumbai", "2026-09-28")
    day29 = report.build_report(conn, "Mumbai", "2026-09-29")
    assert [s["as_of"] for s in day27["sweeps"]] == [S_27_1030]
    assert [s["as_of"] for s in day28["sweeps"]] == [S_27_1900, S_28_0430]
    assert [s["as_of"] for s in day29["sweeps"]] == [S_28_1840]
    assert (day27["osa_pct"], day28["osa_pct"], day29["osa_pct"]) == (0.0, 100.0, 0.0)


def test_date_without_sweeps_is_no_data_not_zero(seed, conn):
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True)])
    r = report.build_report(conn, "Mumbai", "2026-09-30")
    assert r["status"] == "no_data"
    assert r["osa_pct"] is None
    assert r["observations"] == 0
    assert r["available_dates"] == ["2026-09-28"]


def test_day_where_every_store_failed_is_no_data_not_zero(seed, conn):
    seed.store_sweep(S_28_0430, "MUM-001", status="incomplete", reason="soft_ban: x")
    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert r["status"] == "no_data"
    assert r["osa_pct"] is None
    assert r["coverage"]["incomplete"][0]["reason"] == "soft_ban: x"


def test_ghost_stock_counts_as_in_stock(seed, conn):
    # in_stock true with qty 0 was seen 184 times in the real data. The flag decides, not qty.
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True, 0), ("SKU-2", True, 7)])
    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert r["osa_pct"] == 100.0           # qty > 0 would give 50.0
    assert r["skus"][0]["in_stock"] == 1


def test_city_osa_is_total_over_total_not_average_of_percentages(seed, conn):
    # SKU-1: seen once, in stock (100%). SKU-2: seen 3 times, never in stock (0%).
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True), ("SKU-2", False)])
    seed.store_sweep(S_28_0430, "MUM-002", items=[("SKU-2", False)])
    seed.store_sweep(S_28_0430, "MUM-003", items=[("SKU-2", False)])

    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert {s["sku_id"]: s["osa_pct"] for s in r["skus"]} == {"SKU-1": 100.0, "SKU-2": 0.0}
    assert r["osa_pct"] == 25.0            # 1/4; the mean of SKU percentages would be 50.0
                                           # and the mean of store percentages 33.33


def test_rounding_is_half_up_to_two_decimals(seed, conn):
    # 1/32 = 3.125 %: half-up gives 3.13; Python's round() (half-to-even) gives 3.12.
    assert report.pct(1, 32) == 3.13
    assert report.pct(1, 160) == 0.63      # 0.625
    assert report.pct(2, 3) == 66.67
    assert report.pct(0, 0) is None

    seed.store_sweep(S_28_0430, "MUM-001",
                     items=[(f"SKU-{i:02d}", i == 0) for i in range(32)])
    assert report.build_report(conn, "Mumbai", "2026-09-28")["osa_pct"] == 3.13


def test_sku_shows_latest_name(seed, conn):
    seed.store_sweep(S_27_1900, "MUM-001", items=[("SKU-1", True, 5, "Milk 500ml")])
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True, 5, "Milk 500 ml Pouch")])
    r = report.build_report(conn, "Mumbai", "2026-09-28")
    assert r["skus"] == [{"sku_id": "SKU-1", "name": "Milk 500 ml Pouch",
                          "observations": 2, "in_stock": 2, "osa_pct": 100.0}]


@pytest.fixture
def client(db_path, conn, monkeypatch):
    monkeypatch.setenv("OSA_DB", db_path)
    return TestClient(app.app)


@pytest.mark.parametrize("params", [
    {"city": "Pune", "date": "2026-09-28"},
    {"city": "mumbai", "date": "2026-09-28"},
    {"date": "2026-09-28"},
])
def test_api_rejects_unknown_city(client, params):
    r = client.get("/osa", params=params)
    assert r.status_code == 400
    assert "Mumbai, Delhi, Bengaluru" in r.json()["error"]


@pytest.mark.parametrize("bad", ["28-09-2026", "2026/09/28", "20260928", "2026-9-28", "2026-02-30"])
def test_api_rejects_badly_formatted_date(client, bad):
    r = client.get("/osa", params={"city": "Mumbai", "date": bad})
    assert r.status_code == 400
    assert bad in r.json()["error"]


def test_api_returns_503_when_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OSA_DB", str(tmp_path / "missing.db"))
    r = TestClient(app.app).get("/osa", params={"city": "Mumbai"})
    assert r.status_code == 503
    assert not (tmp_path / "missing.db").exists()


def test_default_date_is_yesterday_in_ist(seed, conn):
    seed.store_sweep(S_28_0430, "MUM-001", items=[("SKU-1", True)])
    # 01:00 IST on 29 Sep is still 28 Sep in UTC: a UTC-based "yesterday" would say 27 Sep.
    now = datetime(2026, 9, 29, 1, 0, tzinfo=db.IST)
    r = report.build_report(conn, "Mumbai", now=now)
    assert r["date"] == "2026-09-28"
    assert r["status"] == "ok"
    assert db.yesterday_in_ist(now.astimezone(timezone.utc)) == "2026-09-28"
    assert db.yesterday_in_ist(datetime(2026, 9, 29, 23, 0, tzinfo=db.IST)) == "2026-09-28"
