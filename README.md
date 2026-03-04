# Stay Scanner

Stay Scanner is a local web app that finds hotels and vacation rentals for a city/date range, then ranks results with business rules focused on practical trip planning.

## Features

- Inputs: city, check-in/check-out dates, travelers, rooms
- Searches multiple providers with fallback strategy:
  - primary providers (RapidAPI/Amadeus, if configured)
  - SerpAPI as fallback when results are insufficient
- 25-mile radius filter from city center
- Quality filters:
  - review score >= 7
  - family-friendly
  - safe-area heuristic
- Sorting:
  - free cancellation first
  - then price low to high
- Unified results across hotels and vacation rentals
- CSV export from UI

## Run

```bash
cd stay_scanner
cp api.example.txt api.txt
# edit api.txt with your API keys
./run.sh
```

Open:

- `http://127.0.0.1:5501/index.html`

Backend API default is `http://127.0.0.1:8790`.

## Credentials

`api.txt` is gitignored by default. Each line should be `KEY=VALUE`.

Recommended setup:

- Configure at least one primary provider (`RAPIDAPI_*` or `AMADEUS_*`)
- Configure `SERPAPI_KEY` as fallback provider
- For `booking-com15.p.rapidapi.com`, set `RAPIDAPI_HOST=booking-com15.p.rapidapi.com` and leave `RAPIDAPI_SEARCH_PATH` empty.
