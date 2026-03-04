# Stay Scanner Agent Guide

## Purpose
Maintain and extend the `stay_scanner` app.
This app searches hotels and vacation rentals and applies ranking/filter rules.

## Architecture
- Frontend: static files (`index.html`, `styles.css`, `app.js`)
- Backend: Python HTTP server (`server.py`)
- Runner: `run.sh` (starts backend + static server)
- Cache: SQLite (`stay_cache.sqlite3`)

Browser calls backend only:
- `POST /search`
- `GET /health`

Never call scraper/provider services directly from browser code.

## Provider Logic
Provider-chain execution with retries and fallback.
Priority is controlled by `PROVIDER_PRIORITY` in `api.txt`.
Current intended order:
- `scraperapi`
- `browserless`
- `rapidapi`
- `serpapi`
- `amadeus` (optional)

Response includes:
- `providers_used`
- `provider_errors`
- `execution_status`
- `results`

## Filtering and Sorting Rules
Keep these semantics unless explicitly changed:
- Distance <= 25 miles
- Review score >= 7
- Family-friendly required
- Safe-area required
- Sort by:
  1. free cancellation first
  2. price low to high

## Secrets and Config
- Never hardcode keys.
- Use `api.txt` / environment for credentials.
- Keep `api.txt` out of commits.

## Local Run
```bash
cd stay_scanner
./run.sh
```

## Validation Checklist
- `python3 -m py_compile server.py`
- `GET /health` shows configured providers.
- `POST /search` returns execution status and provider errors clearly.
- Verify no secret values are logged or rendered in UI.

## Git Hygiene
- Do not commit cache DB files.
- Commit only source/config templates/docs.
