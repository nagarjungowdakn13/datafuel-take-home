# DataFuel take-home: QuickMart OSA scraper and report API

A scraper (`sweep.py`) that collects store inventory from the QuickMart mock portal into
SQLite, and a FastAPI endpoint (`GET /osa`) that reports on-shelf availability per city per
IST day. The original brief is in [ASSIGNMENT.md](ASSIGNMENT.md); design decisions and
answers are in **[NOTES.md](NOTES.md)**.

Requires Python 3.10+. Commands are for Windows PowerShell; the macOS/Linux equivalent
follows where it differs.

## 1. Install

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```
macOS/Linux: `python3 -m venv venv && venv/bin/python -m pip install -r requirements.txt`

## 2. Start the mock portal (own terminal, leave it running)

```powershell
.\venv\Scripts\python.exe mock_portal.py
```
macOS/Linux: `venv/bin/python mock_portal.py`

It serves `http://127.0.0.1:8765`. If that port is taken: `$env:PORT=9000; .\venv\Scripts\python.exe mock_portal.py`
(macOS/Linux: `PORT=9000 venv/bin/python mock_portal.py`), then add `--base-url http://127.0.0.1:9000`
to every sweep command.

## 3. Run the six sweeps (second terminal)

Each sweep takes about a minute at the default 2 requests/second. Run them one after another,
never in parallel: the portal silently degrades data under sustained load.

```powershell
foreach ($t in "2026-09-27T04:30:00Z","2026-09-27T10:30:00Z","2026-09-27T19:00:00Z",
               "2026-09-28T04:30:00Z","2026-09-28T10:30:00Z","2026-09-28T18:40:00Z") {
    .\venv\Scripts\python.exe sweep.py --as-of $t
}
```
macOS/Linux: `for t in 2026-09-27T04:30:00Z 2026-09-27T10:30:00Z 2026-09-27T19:00:00Z 2026-09-28T04:30:00Z 2026-09-28T10:30:00Z 2026-09-28T18:40:00Z; do venv/bin/python sweep.py --as-of "$t"; done`

- Each run ends with a summary line, e.g. `26 stores: 25 complete, 1 incomplete (DEL-004: portal returned partial snapshot) · 0m56s`.
- Re-running a sweep is safe: it replaces that sweep's rows and never overwrites a complete store with an incomplete result.
- Options:
  - `--db PATH` (default `$OSA_DB` or `osa.db`)
  - `--base-url URL`
  - `--rate N` (requests/second, default 2)
  - `-v` (also log every retry and 429)

## 4. Start the API

```powershell
.\venv\Scripts\python.exe -m uvicorn app:app --port 8000
```
macOS/Linux: `venv/bin/python -m uvicorn app:app --port 8000`

It reads `osa.db`, or the file named in the `OSA_DB` environment variable. It opens the
database read-only and returns 503 if the file doesn't exist yet.

Example calls (quote the URL in PowerShell because of `&`; on macOS/Linux use `curl`):

```powershell
curl.exe "http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-28"
curl.exe "http://127.0.0.1:8000/osa?city=Delhi&date=2026-09-28"
curl.exe "http://127.0.0.1:8000/osa?city=Bengaluru&date=2026-09-28"
curl.exe "http://127.0.0.1:8000/osa?city=Mumbai"           # no date = yesterday in IST
curl.exe "http://127.0.0.1:8000/osa?city=Pune"             # 400: unknown city
```

Interactive docs: `http://127.0.0.1:8000/docs`. The outputs we got for 2026-09-28 and for
the default date are saved in `results/`.

## 5. Run the tests

```powershell
.\venv\Scripts\python.exe -m pytest -q
```
macOS/Linux: `venv/bin/python -m pytest -q`

The tests use a temp database and a scripted fake HTTP session, so the portal does **not**
need to be running.

## How it works

1. `sweep.py` reads every roster page and tracks the 26 active stores. Before fetching, it writes a `pending` row for each store in the sweep.
2. It reads each store's pages one at a time, at ≤2 requests/second. Transient errors are retried and 429s honour `Retry-After`. Repeated items across pages are de-duplicated, and timestamps are normalised to UTC.
3. A soft-banned page (`meta.source` not `origin`) makes it discard the store and pause 30/60/120 s before retrying. Unrecoverable or `partial` data marks the store `incomplete` with a reason, and none of its items are saved.
4. Each sweep gets an IST date from its `as_of` (UTC+05:30). `/osa` counts only observations from `complete` store-sweeps, using the `in_stock` flag.
5. City OSA is total in-stock ÷ total observations, rounded half-up to 2 decimals. Missing data is listed in `coverage` and is never reported as 0%.

## File map

| Path | What it is |
|---|---|
| `sweep.py` | The scraper: HTTP client with pacing and retries, soft-ban handling, saving. |
| `db.py` | SQLite schema plus UTC/IST time helpers, shared by everything. |
| `report.py` | Pure OSA logic over the database (never calls the portal). |
| `app.py` | Thin FastAPI layer: `GET /osa`. |
| `tests/` | pytest suite: OSA number guards (`test_osa.py`) and scraper failure handling (`test_sweep.py`). |
| `tools/soft_ban_demo.py` | One-off demo that triggers the portal's soft ban on purpose. Afterwards, send no requests for 60 s. |
| `results/` | Saved `/osa` outputs for 2026-09-28 (all three cities) and for the default date. |
| `NOTES.md` | Decisions, known limitations, answers to the brief's questions. |
| `OBSERVATIONS.md` | Every portal quirk we saw, with the request that showed it. |
| `REVIEW.md` | Review of `review_me.py`; its three most serious bugs are fixed in that file. |
| `AI_LOG.md`, `RECORDING.md` | AI usage log and screen-recording links. |
| `ASSIGNMENT.md`, `API.md`, `mock_portal.py` | The original brief, portal contract and mock portal (unchanged). |
| `CLAUDE.md` | Working rules for the AI assistant used on this project. |
