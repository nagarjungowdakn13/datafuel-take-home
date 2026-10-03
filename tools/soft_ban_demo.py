"""Deliberate demo of the portal's fair-use "soft ban".

This script intentionally breaks the project's normal pacing rule (~2 requests/second).
It sends 36 inventory requests for MUM-001 at about 7 requests/second: under the
documented 8/s burst limit, so we never get a 429, but fast enough to be "sustained high
request volume" in the sense of API.md's Fair use section. The point is to see what a
degraded response looks like when the portal gives no error.

After running it, NOBODY may send the portal any request for 60 seconds. API.md doesn't
say how long the degradation lasts. A request made while degraded may extend it, and
then it would spoil the next sweep or probe.

Run once, on purpose:  .\\venv\\Scripts\\python.exe tools\\soft_ban_demo.py
"""
import time

import requests

PORTAL = "http://127.0.0.1:8765"
HEADERS = {"X-Api-Key": "dfhire-2026"}
STORE_ID = "MUM-001"
AS_OF = "2026-09-28T12:00:00Z"  # not a sweep timestamp, so no sweep data is involved
N_REQUESTS = 36
RATE_PER_SEC = 7.0
TIMEOUT_S = 5.0


def fetch_once(session: requests.Session) -> tuple[int | str, dict | None]:
    """Fetch page 0 once. Return the status plus the JSON body when there is one.

    Errors are returned instead of raised. The demo is about the shape of each
    response, so one failure must not stop the run.
    """
    try:
        r = session.get(
            f"{PORTAL}/v1/stores/{STORE_ID}/inventory",
            params={"as_of": AS_OF, "cursor": "0"},
            headers=HEADERS,
            timeout=TIMEOUT_S,
        )
    except requests.RequestException as e:
        return type(e).__name__, None
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


def describe(n: int, elapsed: float, status: int | str, body: dict | None) -> str:
    """Format one result line. Flag a non-origin source loudly: a degraded
    response still returns HTTP 200, so the status alone won't show it."""
    if body is None or "items" not in body:
        return f"#{n:02d} t={elapsed:5.2f}s status={status} body={body}"
    source = (body.get("meta") or {}).get("source")
    line = (f"#{n:02d} t={elapsed:5.2f}s status={status} items={len(body['items']):2d} "
            f"next_cursor={body.get('next_cursor')!r:6} partial={body.get('partial')} "
            f"source={source}")
    if source != "origin":
        line += "   <<<<<<<<<< NOT ORIGIN: DEGRADED RESPONSE >>>>>>>>>>"
    return line


def main() -> None:
    """Send requests one at a time on a fixed schedule. Pacing from the start time
    (not by sleeping after each response) keeps the rate near 7/s even if a
    response is slow."""
    interval = 1.0 / RATE_PER_SEC
    start = time.monotonic()
    with requests.Session() as session:
        for i in range(N_REQUESTS):
            wait = start + i * interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            status, body = fetch_once(session)
            print(describe(i + 1, time.monotonic() - start, status, body), flush=True)
    print(f"\nDone at {time.strftime('%H:%M:%S')}. Send NO requests to the portal for 60 seconds.")


if __name__ == "__main__":
    main()
