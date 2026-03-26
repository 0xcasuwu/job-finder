# job-finder

Extreme multi-source job scraper. Aggregates listings from 13+ sources, filters by location + mile radius, deduplicates, and exports to CSV/JSON.

## Sources

| # | Source | Type | API Key? |
|---|--------|------|----------|
| 1 | Adzuna | API | Yes (free tier) |
| 2 | JSearch (RapidAPI) | API | Yes (free tier) |
| 3 | Jooble | API | Yes (free) |
| 4 | Remotive | API | No |
| 5 | USAJobs | API | No |
| 6 | Indeed | Google dork | No |
| 7 | LinkedIn | Google dork | No |
| 8 | Glassdoor | Google dork | No |
| 9 | ZipRecruiter | Google dork | No |
| 10 | Dice | Google dork | No |
| 11 | SimplyHired | Google dork | No |
| 12 | BuiltIn | Google dork | No |
| 13 | WeWorkRemotely | Google dork | No |

## Setup

```bash
pip install -r requirements.txt

# Optional: add API keys for more results
cp config.example.json config.json
# Edit config.json with your keys
```

### Getting API keys (all free)

- **Adzuna**: https://developer.adzuna.com — sign up, get app_id + api_key
- **JSearch**: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch — subscribe to free tier
- **Jooble**: https://jooble.org/api/about — request API key via email

## Usage

```bash
# Basic — all jobs near Austin within 25 miles
python job_finder.py -l "Austin, TX" -r 25

# With query
python job_finder.py -l "San Francisco, CA" -r 50 -q "software engineer"

# JSON output only
python job_finder.py -l "New York, NY" -r 10 -q "data scientist" -o json

# Remote jobs (skip radius filter)
python job_finder.py -l "Remote" -r 9999 -q "python developer" --no-filter
```

## Output

Results are saved to `results/` as CSV and/or JSON, sorted by distance from your location. A rich table preview is shown in the terminal.

## How it works

1. Geocodes your location to lat/lon
2. Fires all 13 sources concurrently (async)
3. Deduplicates by title + company + location fingerprint
4. Geocodes each job's location and filters by mile radius
5. Exports sorted by distance
