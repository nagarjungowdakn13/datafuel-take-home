# Rules for this project
- ASSIGNMENT.md (the brief) and API.md (the portal contract) are the source of truth.
- Never modify mock_portal.py or API.md.
- Build from API.md and from behaviour we observe with real requests. Reading mock_portal.py
  to confirm something is allowed, but every behaviour we rely on must also be shown with a real request.
- Golden rule: a wrong number is worse than no number. Never store a guess. Never turn missing,
  partial or degraded data into "out of stock".
- Windows + PowerShell. Use .\venv\Scripts\python.exe for everything (python -m pytest,
  python -m uvicorn). Use curl.exe, not curl.
- The portal runs at http://127.0.0.1:8765 in another terminal (API key dfhire-2026).
  Never start, stop or restart it.
- Never send the portal more than about 2 requests per second, and never send requests in parallel.
- Never run sweep.py yourself unless I ask; I run the sweeps in my own terminal.
- Code: small functions, docstrings that explain WHY, parameterised SQL only (no f-string SQL),
  type hints, no unnecessary dependencies.
- Never write a number or result into a .md file unless you ran the command that produced it in this session.
- At the end of each phase: run the tests (once they exist), show git status and git diff --stat,
  then commit with a short imperative message and no Co-Authored-By trailer. One commit per phase.
- Keep OBSERVATIONS.md up to date with every portal quirk we see (what, where, example, why it matters for OSA).
