"""Thin HTTP layer over report.py.

    .\\venv\\Scripts\\python.exe -m uvicorn app:app --port 8000
    GET http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-28

All the logic lives in report.build_report. This file only maps errors to HTTP codes.
"""
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import report

app = FastAPI(title="DataFuel OSA report")


def _open_readonly(path: str) -> sqlite3.Connection:
    """Open the DB read-only. The API must never change data, and db.connect would
    silently create an empty osa.db (and then report "no data" instead of an error)."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/osa")
def osa(city: str | None = None, date: str | None = None) -> JSONResponse:
    """OSA for a city on an IST day. city/date are optional at the HTTP level so that a
    missing city gets our own 400 message instead of FastAPI's generic 422."""
    path = os.environ.get("OSA_DB", "osa.db")
    if not Path(path).is_file():
        return JSONResponse(status_code=503, content={
            "error": f"database {path!r} not found; run sweep.py first (or set OSA_DB)"})
    try:
        conn = _open_readonly(path)
        try:
            body = report.build_report(conn, city, date)
        finally:
            conn.close()
    except report.ReportError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except sqlite3.OperationalError as e:
        return JSONResponse(status_code=503, content={
            "error": f"database {path!r} is not usable ({e}); run sweep.py first"})
    return JSONResponse(content=body)
