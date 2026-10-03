# Review of `review_me.py`

## How this is ranked

Problems are ordered by **how likely each is to put a wrong number in front of a brand**,
not by how bad the code looks. A bug that silently inflates every run ranks above one that
crashes: a crash is noticed, a plausible wrong number isn't. Problems that cause no wrong
number today, only fragility, are at the bottom.

Each item says how it was checked:
- **Verified by run**: seen in the experiments below.
- **Verified against the portal**: the portal behaviour was seen with a real request
  (see `OBSERVATIONS.md`). What the script would do with it follows from reading the code.
- **Code reading only**: not demonstrated in this session.

## The experiments

All runs used a temp folder (the script writes `osa.db` wherever it's run, and would
otherwise add a table to our real database). The portal was running.

**Before: original file with only the timezone fix.** Unmodified, its naive `as_of` gets a
400 and the script retries forever (#7, #12). It was run twice through a wrapper that held
`requests.get` to 2/s (the original has no pacing; our project rule) and capped requests at 40.

| | Requests (cursor → status) | MUM-001 rows (distinct SKUs) | MUM-002 rows (distinct SKUs) | Ending |
|---|---|---|---|---|
| Run 1 | 0, 15, 30 → 200; 0, 15 → 200 | 32 (32) | 59 (35) | `ZeroDivisionError` |
| Run 2 | 0 → 503, 0 → 200, 15, 30 → 200; 0 → 200; 15 → 503, 15 → 200 | 65 (32) | 120 (35) | `ZeroDivisionError` |

Then, with no further requests, the two Mumbai stores were inserted into its empty `stores`
table and `city_osa` was called on the 185 rows. It returned **75.61**. All `observed_at`
values were dated `2026-10-03` (UTC), while the run happened at about 04:00 IST on 2026-10-04.
Over the same 185 rows, the `in_stock` flag says 160 in stock and `qty > 0` says 140.

**After: the three fixes below, run once.** About 3 s, ending in `ZeroDivisionError` from #14,
which was deliberately left unfixed:

| | MUM-001 rows (distinct) | MUM-002 rows (distinct) |
|---|---|---|
| After fixes | 32 (32) | 27 (27) |

## Problems, most serious first

### 1. Shared mutable default `results=[]` leaks one store's products into the next
- **What goes wrong:** the default list is created once and reused by every call. Each store's
  result also contains every earlier store's items, and they're saved under the later store's id.
- **Example:** MUM-002's saved rows also contain MUM-001's products. Run 1 saved **59 rows /
  35 distinct SKUs** for MUM-002, which carries 27. After the fix: **27 / 27**.
- **Fix:** no default list. A fresh dict per call, and a loop instead of the recursion that
  passed the list along.
- **Verified by run.** **Fixed in review_me.py: yes.**

### 2. No primary key and no sweep id: re-runs duplicate everything
- **What goes wrong:** every run appends another copy, and since rows carry no `as_of`, the
  copies can't even be told apart. Any re-run multiplies the observations.
- **Example:** MUM-001 went from **32 rows** after run 1 to **65** after run 2.
- **Fix:** store `as_of`; `PRIMARY KEY (as_of, store_id, sku_id)`; delete-then-insert per
  store-sweep in one transaction (as `sweep.py` does).
- **Verified by run.** **Fixed in review_me.py: no.**

### 3. Page overlaps are not de-duplicated
- **What goes wrong:** the portal sometimes starts a page with the previous page's last item.
  The script saves it twice, so that SKU is counted twice.
- **Example:** in run 2, MUM-001 saved **33 rows for 32 distinct SKUs**. Manual probes saw MUM-001
  @ 2026-09-28T04:30Z page `cursor=30` repeat `SKU-0033` from page `cursor=15`.
- **Fix:** keep items in a dict keyed by `sku_id` per store-sweep.
- **Verified by run.** **Fixed in review_me.py: yes.**

### 4. `qty > 0` used instead of `in_stock`
- **What goes wrong:** the portal reports ghost stock, `in_stock: true` with `qty: 0`. Counting by
  `qty` turns those into out-of-stock and understates OSA. The brief says to use `in_stock`.
- **Example:** on the 185 rows from the runs, `in_stock` says **160** in stock and `qty > 0`
  says **140**. In our real sweeps, **184 of 4,376** rows are ghost stock (e.g. MUM-001 SKU-0002
  @ 2026-09-28T04:30Z: `in_stock: true, qty: 0`).
- **Fix:** count the `in_stock` flag; never use `qty` for availability.
- **Verified by run and against the portal.** **Fixed in review_me.py: no.**

### 5. Dates are taken from `observed_at` as UTC, and "yesterday" is laptop time: today's data gets reported as yesterday's
- **What goes wrong:** `substr(observed_at, 1, 10)` is the UTC date for Mumbai and Bengaluru
  (they send `Z`) but the IST date for Delhi (it sends `+05:30`). `date.today()` is whatever
  zone the machine is in. Between 00:00 and 05:30 IST these two errors line up and present
  today's data as yesterday's. Sweeps such as 2026-09-27T19:00Z (00:30 IST on 28 Sep) land on
  the wrong IST day.
- **Example:** at about 04:00 IST on 4 Oct, the rows were dated `2026-10-03` (UTC), and the
  default "yesterday" on this IST laptop was also `2026-10-03`. So `city_osa` returned **75.61**
  for 3 Oct from data taken on 4 Oct.
- **Fix:** store each sweep's `as_of`, compute its IST date once (UTC+05:30), and group by that.
  Take the default date as yesterday in IST.
- **Verified by run** (the 4 Oct case) **and against the portal** (Delhi `+05:30` vs `Z`).
  **Fixed in review_me.py: no.**

### 6. Degraded (`edge`) responses are trusted
- **What goes wrong:** under sustained load the portal soft-bans silently. Responses stay 200
  but come from `meta.source: "edge"` with 0–5 items and `next_cursor: null`, so they look like
  a complete last page. The script would save that fragment as the store's whole inventory.
- **Example:** in `tools/soft_ban_demo.py`, requests 31–36 of an identical MUM-001 request
  returned 5, 5, 2, 0, 2, 1 items from `edge` (it had been 15 items and `next_cursor: "15"`).
- **Fix:** any page whose `meta.source` isn't `"origin"` means the store's data is incomplete;
  save nothing for it.
- **Verified against the portal** (the script itself wasn't run under a ban). **Fixed in review_me.py: yes.**

### 7. Retries forever, 0.1 s apart, on every error, ignoring `Retry-After`
- **What goes wrong:** the bare `except Exception` retries 400/401/404 (which can never succeed)
  without end. It also retries 5xx and 429 about 10 times a second. That breaks the 8/s burst
  limit, and the sustained volume is exactly what triggers the soft ban in #6.
- **Example:** an `as_of` without a timezone gets `400 {"error": "as_of must include a timezone"}`
  (manual probe #15), and the script would loop on it indefinitely (see #12). In run 2, two 503s
  were retried. They succeeded here only because our wrapper paced them.
- **Fix:** retry only timeouts, network errors and 5xx, with exponential backoff and a cap
  (5 attempts); sleep `Retry-After` on 429; raise immediately on other 4xx; pace every request.
- **400 verified against the portal; the infinite loop is code reading only** (not run, to
  avoid hammering the portal). **Fixed in review_me.py: yes.** One limitation remains: a 429
  still uses up one of the 5 attempts.

### 8. `partial: true` is trusted
- **What goes wrong:** the portal says the snapshot is incomplete, the script saves it anyway,
  and the next problem turns it into 0%.
- **Example:** DEL-004 @ 2026-09-28T10:30Z returns `{"items": [], "partial": true}`.
- **Fix:** treat `partial: true` as incomplete and save nothing.
- **Verified against the portal.** **Fixed in review_me.py: yes.**

### 9. A store with no data counts as 0%
- **What goes wrong:** `per_store.append(... if rows else 0.0)` turns "we have no data for this
  store" into "nothing was in stock", which breaks the brief's golden rule. A store that wasn't
  swept, a partial store (#8) or a store before launch each drag the city average down.
- **Example:** BLR-007 legitimately returns no items before launch (`items: []`, `partial: false`,
  `source: origin` at 2026-09-27T04:30Z), and DEL-004 above returns none. Each would add a 0.0.
- **Fix:** leave stores without observations out of the calculation. With no observations at
  all, return "no data" (`None`), never 0.
- **Portal behaviour verified; the 0.0 is code reading only.** **Fixed in review_me.py: no.**

### 10. City OSA is an average of store percentages
- **What goes wrong:** the brief defines city OSA as total in-stock ÷ total seen. Averaging
  per-store percentages gives a 22-SKU store the same weight as a 33-SKU one (both sizes exist
  in our sweeps), so the result differs whenever stores carry different numbers of SKUs.
- **Example:** this contributed to the 75.61 above, but its effect wasn't isolated. Our test
  `test_city_osa_is_total_over_total_not_average_of_percentages` builds a case where total ÷ total
  is 25.0 and the per-store average is 16.67.
- **Fix:** `SUM(in_stock) / COUNT(*)` over all counted rows.
- **Code reading, demonstrated by our test (not run against this file).** **Fixed in review_me.py: no.**

### 11. The `stores` table is never filled, so `city_osa` divides by zero
- **What goes wrong:** `__main__` creates `stores` but never writes to it. `city_osa` finds no
  stores and computes `sum([]) / len([])`.
- **Example:** every run, before and after the fixes, ended in `ZeroDivisionError` at the final
  `print`.
- **Fix:** fill `stores` (with city) from `/v1/stores`. With nothing to divide, return "no data".
- **Verified by run.** **Fixed in review_me.py: no**, deliberately: making it "finish" would only
  print a number still wrong because of #2, #4, #5, #9 and #10.

### 12. `datetime.utcnow().isoformat()` has no timezone, and is "now", not a sweep time
- **What goes wrong:** the portal rejects an `as_of` without a timezone with 400, and #7 then
  loops forever. Even with a timezone, "now" isn't one of the sweep timestamps, and
  `utcnow()` is deprecated in Python 3.12.
- **Example:** manual probe #15: `as_of=2026-09-28T04:30:00` returned
  `400 {"error": "as_of must include a timezone"}`.
- **Fix:** take `--as-of` from the command line with a timezone. At minimum use
  `datetime.now(timezone.utc)`.
- **Verified against the portal.** **Fixed in review_me.py: yes** (timezone-aware "now"; the
  "now vs sweep time" part is not fixed).

### 13. No request timeout
- **What goes wrong:** one slow or hung response blocks the whole run with no limit.
- **Example:** API.md says some requests are slow. **Not observed in this session**: every
  response we timed came back in under 0.02 s.
- **Fix:** `timeout=5` and retry.
- **Code reading only.** **Fixed in review_me.py: yes.**

### 14. Only two hardcoded stores
- **What goes wrong:** `["MUM-001", "MUM-002"]` is passed off as "Mumbai". The roster is never read.
- **Example:** `/v1/stores` lists 10 Mumbai stores, 9 of them active.
- **Fix:** read every roster page and track the active stores.
- **Roster verified against the portal.** **Fixed in review_me.py: no.**

### 15. No completeness, city or reason is saved
- **What goes wrong:** the tables can't say "MUM-002 for this sweep is incomplete because…".
  Missing data can only become a number (#9), and there's nothing to build a `coverage` block from.
- **Fix:** a per-store-sweep table with status (`pending`/`complete`/`incomplete`) and reason.
- **Code reading only.** **Fixed in review_me.py: no.**

### 16. SQL built with f-strings
- **What goes wrong:** values are pasted into the SQL. A product name with an apostrophe
  (e.g. `Haldiram's Bhujia 200g`) would break the INSERT and crash the run partway through.
  It also lets third-party data inject SQL.
- **Example:** none of the 37 product names in our sweeps (36 SKUs; SKU-0001 was renamed)
  contains an apostrophe, so this is latent.
- **Fix:** `?` placeholders.
- **Code reading only.** **Fixed in review_me.py: no.**

### 17. `observed_at` stored as received, in mixed formats
- **What goes wrong:** one column holds both `...Z` and `...+05:30` strings. Comparing or
  grouping them as text (as #5 does) gives different answers per city.
- **Example:** DEL-001 `2026-09-28T10:15:00+05:30` vs MUM-001 `2026-09-28T04:37:00Z`.
- **Fix:** convert to UTC before storing.
- **Verified against the portal.** **Fixed in review_me.py: no.**

### 18. Paging by recursion
- **What goes wrong:** harmless at 3 pages, but a cursor that doesn't advance would recurse
  until Python's recursion limit and crash. There was also no page cap.
- **Fix:** a loop. (A page cap and a cursor-must-advance check are in `sweep.py`.)
- **Code reading only.** **Fixed in review_me.py: yes** (replaced by a loop as part of #1;
  no page cap was added).

## What the fixes changed (and didn't)

The three fixes are deliberately minimal (`git diff review_me.py`):
1. **#1**: fresh dict per call; loop instead of recursion.
2. **#7, #12, #13**: `get_page()` with a 5 s timeout, at most 5 attempts, exponential backoff on
   network errors and 5xx, `Retry-After` on 429, immediate failure on other 4xx, a 0.5 s pause
   before every request, and a timezone-aware `as_of`.
3. **#3, #6, #8**: reject non-`origin` and `partial` pages (`IncompleteData`, store not saved,
   run continues); de-duplicate by `sku_id`.

**Still broken:** the reporting half (#2, #4, #5, #9, #10, #11) and the data model (#14–#17).
The script still ends in `ZeroDivisionError`. `sweep.py`, `db.py` and `report.py` are the full fix.
