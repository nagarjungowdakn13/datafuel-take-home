# Screen recordings

All videos are unedited and at normal speed. Each link was opened in an incognito window to
check that it plays without signing in.

## Videos

| Part         | Link                                                                               |
| ------------ | ---------------------------------------------------------------------------------- |
| part-1       | https://drive.google.com/file/d/17aD1KBBdH8V64M4gL_J13HwLdxed_hUq/view?usp=sharing |
| part-2       | https://drive.google.com/file/d/1SoQoUNP0dLhbYdfuKvEj_YpLN-hfZACa/view?usp=sharing |
| Talk-through | https://drive.google.com/file/d/1YQ7gw7JCbn0NgC8oWoSJL3PUMjTIc_xX/view?usp=sharing |

## Key moments

Timestamps are positions in each video (mm:ss). Each one is computed from the recording's
start time and the logged clock time of the event (command output, sweep start times and
git commits), so they are accurate to within a few seconds.

| Part | Timestamp | Moment |
| ---- | --------- | ------ |
| part-1 | 06:34 | Project setup committed (venv, requirements, .gitignore); brief renamed to ASSIGNMENT.md |
| part-1 | 11:30 | First portal probes with `curl.exe` (health, store pages, inventory) |
| part-1 | 11:43 | DEL-004 @ 2026-09-28T10:30Z returns `partial: true` with no items |
| part-1 | 11:45 | MUM-007 returns 500, 500, then 200 |
| part-1 | 11:51 | Random 503 on MUM-001, then 200 on retry |
| part-1 | 12:06 | Page overlap found: MUM-001 `cursor=30` repeats SKU-0033 |
| part-1 | 15:48 | Soft-ban demo starts (36 requests at ~7 req/s) |
| part-1 | 15:52 | Request 31: HTTP 200 but `meta.source: "edge"`, items cut to 0-5 |
| part-1 | 17:48 | Portal quirks documented in OBSERVATIONS.md (commit) |
| part-1 | 19:19 | `db.py`: schema, pending rows, UTC/IST helpers (commit) |
| part-1 | 26:02 | `sweep.py` written and committed |
| part-2 | 00:27 | First real sweep starts (2026-09-27T04:30Z) |
| part-2 | 03:42 | Sweep 2026-09-28T10:30Z: DEL-004 marked incomplete (partial snapshot) |
| part-2 | 05:26 | All six sweeps finished |
| part-2 | 08:01 | Re-run of sweep 2026-09-28T04:30Z (safe-to-run-twice check) |
| part-2 | 11:26 | `/osa` API and saved results for 2026-09-28 (commit) |
| part-2 | 20:01 | 36 tests passing (commit) |
| part-2 | 31:01 | `review_me.py` fixed and REVIEW.md written (commit) |
| part-2 | 36:16 | NOTES.md and README.md (commit) |

## Talk-through outline (5 minutes)

- **What I built:** the sweep scraper, the SQLite schema, and the `/osa` API with coverage.
- **Hardest bug:** _TODO: e.g. the silent soft ban, which returns HTTP 200 with truncated
  data, or the IST day boundary._
- **What I'd improve:** _TODO: e.g. store retry/soft-ban history in the DB, a circuit
  breaker for full outages, and the 20,000-store design in NOTES.md._
