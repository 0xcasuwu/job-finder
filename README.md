# job-finder

Exhaustive multi-source job scraper. Hits **every conceivable source** — 60+ channels across 7 categories — filters by location + mile radius, deduplicates, and exports to CSV/JSON.

## Sources (~160+ endpoints)

### Category A — Free REST APIs (12)
| # | Source | Key? | Notes |
|---|--------|------|-------|
| A1 | Adzuna | Yes (free) | 250 req/day, paginated |
| A2 | JSearch (RapidAPI) | Yes (free) | 200 req/month |
| A3 | Jooble | Yes (free) | Unlimited |
| A4 | Remotive | No | Remote jobs only |
| A5 | USAJobs | No | US government positions |
| A6 | Arbeitnow | No | EU + remote |
| A7 | FindWork.dev | No | Dev/tech jobs |
| A8 | Jobicy | No | Remote jobs |
| A9 | The Muse | No | All industries |
| A10 | Reed.co.uk | Yes (free) | UK-focused |
| A11 | Careerjet | No | Global aggregator |
| A12 | Talent.com | No | Global aggregator |

### Category B — Direct Site Scrapers (10)
Indeed, LinkedIn, Glassdoor, ZipRecruiter, Monster, Dice, SimplyHired, BuiltIn, Wellfound (AngelList)

### Category C — ATS Board Crawlers (~94 companies)
| ATS | Companies | Examples |
|-----|-----------|----------|
| Greenhouse | 57 | Airbnb, Stripe, Coinbase, OpenAI, Discord, Figma, Reddit |
| Lever | 27 | Netflix, Anthropic, Webflow, Snyk, Tailscale, dbt Labs |
| Ashby | 10 | Ramp, Notion, Linear, Vercel, Supabase |

### Category D — Google Dorking (44 sites)
Indeed, LinkedIn, Glassdoor, ZipRecruiter, Dice, SimplyHired, BuiltIn, WeWorkRemotely, StackOverflow, Wellfound, Arc.dev, Otta, Triplebyte, Hired, FlexJobs, WorkingNomads, Himalayas, DailyRemote, NoDesk, RemoteOK, JustRemote, Idealist, HigherEdJobs, ClearanceJobs, eFinancialCareers, Mediabistro, Dribbble, PowerToFly, AuthenticJobs, CyberSecJobs, GovernmentJobs, Robert Half, Kelly Services, Adecco, Randstad, ManpowerGroup, Hays, Greenhouse, Lever, Workday, iCIMS, SmartRecruiters, Talent.com, Jora, Jobrapido

### Category E — Niche/Remote Boards (4)
WeWorkRemotely, RemoteOK, Himalayas, WorkingNomads

### Category F — Community/Social (3)
Hacker News "Who is Hiring", Reddit (12 job subreddits), Craigslist (40+ city subdomains)

### Category G — Government (1)
GovernmentJobs.com (state/local government)

## Setup

```bash
pip install -r requirements.txt

# Optional: add API keys for more results
cp config.example.json config.json
# Edit config.json with your keys
```

### Getting API keys (all free)

- **Adzuna**: https://developer.adzuna.com
- **JSearch**: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
- **Jooble**: https://jooble.org/api/about
- **Reed**: https://www.reed.co.uk/developers/jobseeker

All keys are optional. The scraper works with zero keys (direct scrapers, ATS crawlers, Google dorks, community sources all require no auth).

## Usage

```bash
# All jobs near Austin within 25 miles
python job_finder.py -l "Austin, TX" -r 25 -q "software engineer"

# Broader search
python job_finder.py -l "San Francisco, CA" -r 50 -q "data scientist"

# Verbose — show per-source breakdown
python job_finder.py -l "NYC" -r 10 -q "product manager" -v

# Remote jobs (skip radius filter)
python job_finder.py -l "Remote" -r 9999 -q "python developer" --no-filter

# JSON output only
python job_finder.py -l "Chicago, IL" -r 30 -q "nurse" -o json
```

## How it works

1. Geocodes your location to lat/lon
2. Fires all ~160 endpoints concurrently (async, rate-limited per domain)
3. Deduplicates by title+company+location fingerprint AND by URL
4. Geocodes each job's location and filters by mile radius
5. Exports sorted by distance, with rich terminal table preview
