# Notes

Every portal quirk mentioned here is documented, with the request that showed it, in
`OBSERVATIONS.md`. Numbers come from the probes, the soft-ban demo, the six sweeps and the
saved `/osa` results in this repo.

## 1. Main decisions

### Which stores we track: `is_active`, ignoring `is_serviceable` (26 of 30)
- The roster has 30 stores (10 per city). We track the 26 with `is_active: true`.
- `is_serviceable` means "accepting orders right now" and can flip during the day (rain,
  maintenance). The shelf still exists while a store is briefly unserviceable. The roster
  also has no `as_of`, so today's flag says nothing about the flag at a past sweep time.
  **DEL-006** (active, not serviceable) is tracked. It returned 32 items in every sweep.
- `is_active: false` means shut down. **MUM-009** and **BLR-004** are inactive but claim to be
  serviceable. We read that as a stale flag on a closed store, so they're not tracked.
  **DEL-010** and **BLR-010** (both flags false) aren't tracked either.
- The whole roster is still saved in `stores`, so the excluded stores stay visible.

### How each problem is handled (`sweep.py`)
| Problem | Handling |
|---|---|
| 429 | Sleep for `Retry-After` (default 2 s, cap 60 s) and retry without using up an attempt; give up after 10 such waits. **Never seen**, not even at ~7 req/s; tested with a fake session. |
| 500 / 503 | Exponential backoff with jitter, at most 6 attempts per request. Seen: a random 503, and MUM-007 returning 500, 500, then 200. |
| Slow responses | 5 s timeout, then retry like a 5xx. **Never seen** (every timed response was under 0.02 s); tested with fakes. |
| 400 / 401 / 403 / 404 | Never retried. 401/403 abort the whole sweep, since every store would fail the same way. |
| `partial: true` | Store-sweep is incomplete ("portal returned partial snapshot") and none of its items are saved. Seen once: DEL-004 @ 2026-09-28T10:30Z. |
| Mixed timezones | Every timestamp is parsed with its offset and stored as UTC `...Z`. Timestamps without a timezone are rejected, never assumed. (Delhi sends `+05:30`, the others `Z`.) |
| Pages overlap | Items are kept in a dict keyed by `sku_id`, and the `observations` primary key `(as_of, store_id, sku_id)` blocks duplicates. A repeat with a different `in_stock` marks the store incomplete. |
| `price` as a string (Bengaluru) | Converted to float; an unreadable price becomes NULL (OSA doesn't use it). |
| Ghost stock (`in_stock: true, qty: 0`) | `in_stock` decides, as the brief says; `qty` is stored for information only. 184 of 4,376 saved rows are like this. |
| SKU-0001 renamed on 28 Sep | `sku_id` is the identity; `/osa` shows the latest name seen that day. |
| BLR-007 empty before launch | An empty, `origin`, non-partial response is a real empty store: complete with 0 items (2026-09-27T04:30Z and 10:30Z). `/osa` lists these under `coverage.complete_but_empty` so they don't look like normal data. |
| `in_stock` not a real boolean | Store incomplete. We don't guess. |

### Soft ban: detection and recovery
- **What it looks like** (`tools/soft_ban_demo.py`): 36 identical requests at ~7 req/s, under
  the 8/s burst limit. The first 30 (about 4.2 s) were normal: 15 items, `next_cursor: "15"`.
  From request 31 on, every response was still **HTTP 200**, but `meta.source: "edge"`,
  5, 5, 2, 0, 2, 1 items, `next_cursor: null`, `partial: false`. No error and no 429. It looks
  like a complete last page.
- **Detection**, checked on every page. A page is degraded if:
  - `meta.source` isn't `"origin"`,
  - or the body's `store_id` isn't the store we asked for,
  - or a page after the first is empty (a real listing never ends with an empty page).
- **Recovery:** throw away everything read for that store, even earlier pages that looked
  fine, because we can't tell where the degradation started. Send nothing for 30 s, then
  60 s, then 120 s, and restart the store from cursor 0 each time. After three degraded
  attempts, mark the store incomplete with a reason starting `soft_ban`. The pause also
  runs after the last attempt, so the next store doesn't start inside the ban.
- **Why 2 req/s:**
  - The limit that bites is fair use. It's silent, and its thresholds aren't published.
    At 7 req/s it tripped within seconds.
  - The pacer runs before every request, retries included, and stores are swept one at a
    time, so 2 req/s is the real peak.
  - It's a safety margin, not a measured threshold. At this rate:
    - each of the six sweeps took 38–56 s;
    - no store was marked `soft_ban`;
    - every store's item count was identical across the six sweeps, apart from BLR-007's
      launch and DEL-004's partial snapshot. A truncated `edge` listing would have shown up
      as a random drop.

### IST day: grouping by the sweep's `as_of`
- Each sweep's IST date (UTC+05:30) is computed once from its `as_of` and stored in
  `sweeps.ist_date`. `/osa` selects sweeps by that.
- Two of the six sweeps fall on a different IST day from their UTC date:
  - 2026-09-27T19:00Z is 00:30 IST on 28 Sep;
  - 2026-09-28T18:40Z is 00:10 IST on 29 Sep.
- **Why not `observed_at`:** it lags `as_of` by 0–20 min, varies by store, and arrives in
  different formats by city. Grouping by it could split one sweep across two days, and
  taking its first 10 characters gives the UTC date for some cities and the IST date for
  Delhi. A sweep is one snapshot, so it belongs to one day.
- Checked independently: selecting sweeps by the UTC window `[2026-09-27T18:30Z,
  2026-09-28T18:30Z)` gives the same three sweeps and the same Mumbai and Delhi totals.

### Re-runs, pending rows, no_data
- **Pending first:** a sweep writes a `pending` row for every tracked store before fetching.
  If it crashes or is stopped, the unfinished stores stay visible as not complete instead
  of disappearing (which would make coverage look better than it is).
- **One transaction per store:** delete the store-sweep's old rows, insert the new ones,
  update its status. Re-running gives the same rows.
- **Incomplete never overwrites complete:** if a re-run fails for a store that already has a
  complete result, the old result is kept and a WARNING is printed.
- **Counting:** `/osa` only counts observations whose store-sweep is `complete`, enforced in
  the SQL join. Everything else is listed in `coverage.incomplete` with its reason.
- **City OSA** is total in-stock ÷ total observations, rounded half-up with `Decimal`.
- **No data is never 0%:** no sweeps or no counted observations gives `status: "no_data"`,
  `osa_pct: null` and `available_dates`. The default date is yesterday in IST.

### Known limitations
- Store flags are only known as of today. We track stores by today's `is_active`, not
  their status at sweep time.
- Retries and soft-ban rounds are printed to the console but not stored in the database.
- 429s and slow responses were never seen live; those paths are only tested with fakes.
  How long a soft ban lasts was never measured.
- If the whole portal is down, each store spends 6 attempts (5 s timeout plus backoff)
  before giving up, so a full outage makes a sweep very slow. A circuit breaker would fix it.
- Excluding an incomplete store is honest but can bias a city's OSA if failures cluster in
  certain stores. `coverage` shows it; Delhi on 28 Sep is `partial` (26 of 27 store-sweeps).
- A complete re-run replaces the earlier complete result (latest wins), even if it differs.

## 2. Questions

**"Your dashboard shows our Delhi availability fell from 92% to 41% yesterday."**
Before replying, check whether the drop is real or comes from our data:
- **Coverage for that day:** how many sweeps ran, and any `incomplete` or `pending`
  store-sweeps (especially `soft_ban` or partial). Compare the observation count with a
  normal day (Delhi on 28 Sep had 761).
- **Is it concentrated?** Look at `sweeps[]` and the per-store rows. One sweep or one store
  doing all the damage points to a scraping or portal problem, or a store that was down.
- **Day boundary:** did a sweep land on the wrong IST day, or one day have fewer sweeps?
- **Raw rows:** check them for `edge`/truncated listings, and re-fetch one store's
  snapshot from the portal.

Only then reply: either "confirmed, here's which SKUs and stores", or "our data for that day
was incomplete, the corrected number is X, or we don't have one".

**₹4.20 lakh (app dashboard) vs ₹4.61 lakh (sum of stores).**
I'd show the app's ₹4.20 lakh as the headline, labelled with its source, plus a note that
the store-level figures don't add up to it. A store-level sum is where over-counting creeps
in. This project hit exactly those causes: repeated rows across pages, re-runs appending
copies (`review_me.py` doubled its rows), and day boundaries (UTC vs IST) putting sales
into the wrong day. A city total from the platform is also likely to be net of
cancellations or refunds. Then I'd reconcile the ₹0.41 lakh gap before showing any
store-level breakdown. A number that can't be reconciled isn't shown as fact.

**Where I would not use an AI/LLM.**
Anywhere on the path from portal response to published number:
- deciding `in_stock`;
- parsing timestamps and mapping to IST days;
- detecting soft bans;
- marking stores complete or incomplete;
- computing OSA.

These must be deterministic, testable and repeatable: the same input gives the same output
every time, and every number can be traced back to rows. An LLM can confidently produce a
plausible wrong number, and here a wrong number is worse than none. I also wouldn't let an
LLM fill gaps or "estimate" missing stores. AI is fine for drafting code, which the tests
and our own checks then verify. While building this, AI-written code was wrong more than
once and was caught by checks: see `AI_LOG.md`.

## 3. Bonus: 20,000 stores every 30 minutes
- **Request budget first.** Stores here took 2–3 inventory pages each (22–33 SKUs at 15 per
  page). 20,000 stores is then about 40,000–60,000 requests per 30 minutes, or 22–33 req/s
  sustained. At the 2 req/s we use, one API key manages about 3,600 requests per 30 minutes.
  So this needs the platform's cooperation: higher limits, more keys, or a bulk or
  changes-only feed. Scraping harder just gets banned.
- **A queue of store-sweep jobs** keyed by `(as_of, store_id)`, so retries and re-runs stay
  idempotent. Workers share a per-key rate limiter, and each key gets its own soft-ban
  detector and circuit breaker.
- **Postgres**, partitioned by day, with bulk inserts. Keep raw responses in object storage
  so any number can be audited or recomputed.
- **Precompute daily OSA** per city and SKU, instead of computing it on each request.
- **Monitor coverage, not just errors:** % of store-sweeps complete, soft-ban rate,
  sweeps that didn't finish within their 30 minutes, with alerts. A sweep that runs into the
  next slot is marked incomplete, never stretched.
