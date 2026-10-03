# Portal observations

Everything here was seen with real `curl.exe` requests against the running portal on
2026-10-04 (IST), one request at a time, ≥1.2 s apart. Probe IDs (`#05` etc.) refer to the
request list at the bottom.

## Quirks

| # | What | Where | Example (as returned) | Why it matters for OSA |
|---|---|---|---|---|
| 1 | **Pages overlap**: a page can start with the last item of the previous page | MUM-001 @ 2026-09-28T04:30Z cursor 30 (#18); BLR-007 @ same ts cursor 15 (#21) | cursor 15 ends with `SKU-0033`, cursor 30 starts with `SKU-0033` (identical row). BLR-007 cursor 0 ends `SKU-0015`, cursor 15 starts `SKU-0015` | Counting the duplicate inflates observations (and in-stock count) for that SKU. Dedupe by `(store, sweep, sku_id)`. |
| 2 | **Page size is not fixed**: 15 or 16 items | BLR-007 cursor 15 (#21) has 16 items (15 + the repeated one) | `next_cursor` goes `"15"` → `null` | Don't infer "end of data" or completeness from page size. Follow `next_cursor`. |
| 3 | **`next_cursor` is a string** | every paged response | `"next_cursor": "15"` | Pass it back as-is; don't do arithmetic on it. |
| 4 | **`in_stock: true` with `qty: 0`** | MUM-001 SKU-0002 and SKU-0019 (#05, #06); DEL-001 SKU-0034 (#19); BLR-007 SKU-0032 (#21) | `{"sku_id": "SKU-0002", "in_stock": true, "qty": 0}` | Using `qty > 0` instead of `in_stock` would count these as out of stock. The brief says use `in_stock`. (Not seen: `in_stock: false` with `qty > 0`.) |
| 5 | **`price` type differs by city**: string in Bengaluru, number elsewhere | BLR-001, BLR-007 (#08, #10) vs MUM/DEL | BLR `"price": "443.00"`; MUM `"price": 443.0` | Not used for OSA, but storing it naively gives mixed types. Parse to a number. |
| 6 | **`observed_at` timezone differs by city**: Delhi uses `+05:30`, Mumbai/Bengaluru use `Z` | DEL-001 (#07) vs MUM-001 (#05) | DEL `"2026-09-28T10:15:00+05:30"` (= 04:45Z); MUM `"2026-09-28T04:37:00Z"` | Taking the first 10 characters as "the date" mixes IST and UTC dates across cities. Always parse with the offset, then convert. |
| 7 | **`observed_at` lags `as_of` by a few minutes** (varies per store/sweep); all items in one snapshot share it | MUM-001 +7 min (#05), +2 min (#17b); DEL-001 +15 (#07); BLR-001 +11 (#08); BLR-007 +16 (#10); MUM-007 +19 (#14) | as_of `04:30Z` → observed_at `04:46:00Z` | Decide explicitly whether a row belongs to its sweep's `as_of` or its `observed_at` when assigning an IST day. |
| 8 | **`meta.generated_at` is wall-clock "now"**, not snapshot time | every inventory response | as_of 2026-09-28 → `"generated_at": "2026-10-03T21:54:04Z"` | Never use it to bucket data by day. |
| 9 | **SKU renamed between sweeps** | SKU-0001 at MUM-001 (#17b vs #05) | 27 Sep 04:30Z: `"Amul Taaza Toned Milk 500ml"`; 28 Sep 04:30Z: `"Amul Taaza Toned Milk 500 ml Pouch"` | Grouping by name splits one product into two. Group by `sku_id`, pick one display name. |
| 10 | **Stores don't carry every SKU** | MUM-001 has no SKU-0013, 0015, 0020, 0036 (#05, #06, #18) | listing jumps `SKU-0012` → `SKU-0014` | An absent SKU is "not listed", not "out of stock". It must not add to observations. |
| 11 | **Legitimately empty store**: 200, `items: []`, `partial: false`, `source: origin` | BLR-007 @ 2026-09-27T04:30Z (#09); same store has items at 2026-09-28T04:30Z (#10) | `{"items": [], "partial": false, "next_cursor": null, "meta": {"source": "origin"}}` | Contributes zero observations, not 0%. Must be told apart from a degraded (soft-ban) empty page. |
| 12 | **`partial: true` with no items** | DEL-004 @ 2026-09-28T10:30Z (#11) | `{"items": [], "partial": true, "next_cursor": null, "meta": {"source": "origin"}}` | Store/sweep is incomplete. Store nothing as out of stock; report it in `coverage.incomplete`. |
| 13 | **Repeated 500s, then success** | MUM-007 @ 2026-09-28T12:00Z (#12–#14) | `500 {"error": "internal error"}`, `500`, then `200` with items | A retry budget of 1–2 would mark this store failed. Need ≥3 attempts (with backoff). |
| 14 | **Random 503** that clears on retry | MUM-001 @ 2026-09-27T04:30Z (#17 → #17b) | `503 {"error": "upstream unavailable, try again"}`, retry → 200 | Retry 5xx; if retries run out, mark the store/sweep incomplete. |
| 15 | **Contradictory store flags** | `/v1/stores` (#02–#04) | MUM-009, BLR-004: `is_active: false, is_serviceable: true`. DEL-006: `is_active: true, is_serviceable: false`. DEL-010, BLR-010: both false | Which stores count in `stores_expected` changes the denominator and coverage. Needs a written decision. |
| 16 | **Store flags are "now" only** | `/v1/stores` has no `as_of` | — | We can't know a store's serviceability at a past sweep time. |
| 17 | Store roster: 30 stores, 10 per city, 12 per page, 3 pages | #02–#04 | page 3: 6 stores, `next_page: null` | Must follow `next_page`; one page would miss Bengaluru. |
| 18 | `as_of` without a timezone → 400 | #15 | `400 {"error": "as_of must include a timezone"}` | Always send an explicit `Z`/offset. |
| 19 | Wrong API key → 401; `/v1/health` needs no key | #16, #01 | `401 {"error": "missing or invalid X-Api-Key"}` | A 401 is a config error. Fail fast, don't retry. |
| 20 | **Soft ban**: after sustained load, responses are still HTTP 200 but come from `meta.source: "edge"`, truncated or empty, with `next_cursor: null` and `partial: false` | MUM-001 @ 2026-09-28T12:00Z, `tools/soft_ban_demo.py` (see below) | #31: `200, items=5, next_cursor=None, partial=False, source=edge` (the same request had returned 15 items and `next_cursor: "15"` 30 times) | The response looks like a complete, final page. A scraper that checks only status/`partial`/`next_cursor` stores 0–5 SKUs as the store's whole inventory. Every SKU it drops vanishes from that store's observations, which skews OSA and makes coverage look complete when it isn't. |

### Soft-ban demo (`tools/soft_ban_demo.py`, run once at 03:28 IST on 2026-10-04)

36 identical requests (MUM-001, `as_of=2026-09-28T12:00:00Z`, cursor 0) one at a time at ~7 req/s, 5 s timeout:

| Requests | Elapsed | Status | items | next_cursor | partial | meta.source |
|---|---|---|---|---|---|---|
| #01–#30 | 0.00–4.17 s | 200 | 15 each | `"15"` | false | origin |
| #31 | 4.31 s | 200 | 5 | null | false | **edge** |
| #32 | 4.45 s | 200 | 5 | null | false | **edge** |
| #33 | 4.59 s | 200 | 2 | null | false | **edge** |
| #34 | 4.74 s | 200 | 0 | null | false | **edge** |
| #35 | 4.86 s | 200 | 2 | null | false | **edge** |
| #36 | 5.02 s | 200 | 1 | null | false | **edge** |

What we can say from this run:
- **No error at all.** No 429 (we stayed under 8/s), no 5xx; every response was 200.
- The switch came after **30 requests in ~4.2 s**. One run doesn't tell us the exact window or threshold, only that sustained ~7/s trips it within seconds.
- Degraded responses **vary** (5, 5, 2, 0, 2, 1 items) for an identical request, and always claim to be the last page (`next_cursor: null`).
- **`meta.source == "edge"` was the only field that reliably marked them.** `partial` stayed `false`, and an empty `items` is also what a legitimately empty store returns (quirk 11).
- Not measured: **how long** the degradation lasts, and whether requests made during it extend it. We sent nothing for 60 s after the run.

**Rule for the scraper:** any inventory response whose `meta.source` isn't `"origin"` is not data. Discard the whole store/sweep (never keep the partial items), back off, retry later at a slow rate. If it still isn't `origin`, mark the store/sweep incomplete with reason `degraded (edge)`. Also pace well below the rate that tripped it: ~2 req/s sustained, per CLAUDE.md.

### Not yet observed (documented, but no real request has shown it yet)
- **429 / `Retry-After`**: not even at ~7 req/s in the soft-ban demo.
- **Slow requests**: every response so far came back in under 0.02 s.
- **How long the soft ban lasts**, and whether requests during it extend it.

## Things API.md does not tell us

1. That pages can **overlap** (repeat the previous page's last item), or that page size can be 16.
2. That `price` is a **string** for some stores.
3. That `observed_at` uses **different timezone offsets** per city.
4. That `in_stock` and `qty` can **disagree** (`in_stock: true, qty: 0`). It only says `qty` is "Informational".
5. That SKU **names change** over time for the same `sku_id`.
6. Which stores to track when `is_active` and `is_serviceable` **contradict** each other. Also, whether an inactive store still returns inventory.
7. How to tell a **legitimately empty** store from a degraded empty response. The only documented hint is `meta.source`.
8. What `partial: true` contains (here: no items at all), and whether retrying ever fixes it.
9. That some failures are **persistent for the first few attempts** (MUM-007), not just random.
10. The **fair-use thresholds** (window, request count, ban length), and whether requests that get a 429 still count toward them.
11. That an `edge` response is **degraded data**: truncated or empty, while `partial: false` and `next_cursor: null` make it look complete (quirk 20).
12. Whether IST day bucketing should use the sweep's `as_of` or the row's `observed_at`.
13. The burst limit is "across all clients", so another process hitting the portal eats our budget.

## Probe log

| # | Request | Status |
|---|---|---|
| 01 | `/v1/health` (no key) | 200 |
| 02–04 | `/v1/stores?page=1..3` | 200 ×3 |
| 05, 06, 18 | MUM-001 @ 2026-09-28T04:30Z, cursor 0 / 15 / 30 | 200 ×3 |
| 07, 19 | DEL-001 @ 2026-09-28T04:30Z, cursor 0 / 15 | 200 ×2 |
| 08, 20 | BLR-001 @ 2026-09-28T04:30Z, cursor 0 / 15 | 200 ×2 |
| 09 | BLR-007 @ 2026-09-27T04:30Z | 200 (empty) |
| 10, 21 | BLR-007 @ 2026-09-28T04:30Z, cursor 0 / 15 | 200 ×2 |
| 11 | DEL-004 @ 2026-09-28T10:30Z | 200 (`partial: true`) |
| 12–14 | MUM-007 @ 2026-09-28T12:00Z ×3 | 500, 500, 200 |
| 15 | MUM-001 @ `2026-09-28T04:30:00` (no tz) | 400 |
| 16 | `/v1/stores?page=1`, wrong key | 401 |
| 17, 17b | MUM-001 @ 2026-09-27T04:30Z, cursor 0 | 503, then 200 |
