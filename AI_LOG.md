# AI log

## Tools and what I used them for

**Claude Code** (Claude Opus 5.5, in the terminal) for the whole project:
- reading the brief, API.md, `review_me.py` and `mock_portal.py`, and summarising requirements;
- probing the portal with `curl.exe` (one request at a time) and writing `OBSERVATIONS.md`;
- drafting `db.py`, `sweep.py`, `report.py`, `app.py` and the tests, then running them;
- reviewing `review_me.py` and running before/after experiments on it;
- querying `osa.db` to sanity-check the sweeps, and committing each phase.

I set the rules up front in `CLAUDE.md` and checked the AI's output with independent
queries, experiments and planted bugs (below), not by reading it and trusting it.

## Prompts that mattered

1. **The project rules (`CLAUDE.md`).** The key lines were:
   - "Build from API.md and from behaviour we observe with real requests. Reading
     mock_portal.py to confirm something is allowed, but every behaviour we rely on must also
     be shown with a real request."
   - "Never send the portal more than about 2 requests per second."
   - "Never write a number or result into a .md file unless you ran the command that produced it."

   These kept the AI from building on what it had read in the portal's source. For example,
   it listed 429s and slow responses as *not observed* instead of claiming them.

2. **The deliberate soft-ban demo:** *"36 inventory requests for MUM-001 … at about 7
   requests/second … print … meta.source, and a loud marker when meta.source is not 'origin'."*
   This turned the vaguest part of API.md into a measured fact. After 30 requests in about
   4.2 s, every response was still HTTP 200 but came from `edge` with 0–5 items and
   `next_cursor: null`. The whole detection design in `sweep.py` came from that output.

3. **Independent verification:** *"Independently verify Mumbai and Delhi for 2026-09-28
   WITHOUT using report.py … If anything differs, explain which one is wrong before changing
   code."* This was followed by *"Do these one at a time, showing the failing test each time,
   and revert"* (planting four bugs). Together they checked that the numbers are right
   (plain SQL over a UTC window matched exactly: Mumbai 753/648, Delhi 761/606) and that the
   tests catch the mistakes they're meant to.

## Times the AI was wrong, and how it was caught

1. **A wrong prediction in the `review_me.py` review.** The AI predicted that, once its
   `stores` table was filled, `review_me.py` would print **0%** for the default "yesterday".
   Running the experiment printed **75.61**. The run was at about 04:00 IST on 4 Oct, which is
   still 3 Oct in UTC. The script's `substr(observed_at, 1, 10)` (a UTC date) and its
   `date.today() - 1` (laptop time) both gave `2026-10-03`, so it reported *today's* data as
   *yesterday's*. **Caught by:** insisting on running the experiment instead of accepting
   the reasoning. The real failure was worse than the predicted one, and REVIEW.md now
   describes both.

2. **Wrong arithmetic in a test comment.** In `test_city_osa_is_total_over_total_not_average_of_percentages`,
   the AI wrote that the mean of store percentages would be **33.33**. The stores are 1/2,
   0/1 and 0/1, so it's (50 + 0 + 0) / 3 = **16.67**. The assertion (25.0) was right; the
   comment and the matching line in REVIEW.md weren't. **Caught by:** recomputing the
   numbers while checking REVIEW.md's claims before committing.

3. **An overstated claim in NOTES.md.** The first draft said every store returned the same
   item count in every sweep. The database query from earlier in the session showed two
   exceptions: BLR-007 had 0 items before its launch, and DEL-004 was a partial snapshot.
   **Caught by:** re-reading the draft against the actual query output.

4. **Smaller ones, caught before they mattered:**
   - The first draft of `sweep.py` ended `run_sweep` with a meaningless placeholder line
     instead of saving the roster. Caught on review before anything ran.
   - A smoke-test command contained a stray `cat >` with no input. It waited on stdin until
     the 120 s timeout and had to be killed.
   - A verification one-liner failed with a `SyntaxError`: Python 3.12 doesn't allow
     backslashes inside f-string expressions.
   - The API's error for `date=2026-02-30` didn't include the bad value. Caught while
     exercising the error cases, and fixed.

## Where I pushed back on, or added to, what was asked

- When asked to run `review_me.py` because "it's slow-paced anyway", the AI pointed out
  that it isn't: it requests pages back to back and retries every 0.1 s. It ran the script
  through a wrapper that held it to 2 req/s, and in a temp folder so it couldn't write into
  our real `osa.db`.
- All 36 tests passed on the first run. Rather than take that as proof, 11 bugs were
  planted in a scratch copy of the code, and each one made at least one test fail.
