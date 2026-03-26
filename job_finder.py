#!/usr/bin/env python3
"""
job_finder.py — Extreme multi-source job scraper.

Aggregates listings from multiple APIs and job board scrapers,
filtered by location and mile radius. Deduplicates and exports to CSV/JSON.

Sources:
  1. Adzuna API (free tier — 250 req/day)
  2. JSearch via RapidAPI (free tier — 200 req/month)
  3. Jooble API (free)
  4. Remotive API (free, remote jobs)
  5. USAJobs API (free, government jobs)
  6. Indeed scraper (Google dorking fallback)
  7. LinkedIn scraper (Google dorking fallback)
  8. Glassdoor scraper (Google dorking fallback)
  9. SimplyHired / ZipRecruiter / Dice (Google dorking fallback)

Usage:
  python job_finder.py --location "Austin, TX" --radius 25
  python job_finder.py --location "San Francisco, CA" --radius 50 --query "software engineer"
  python job_finder.py --location "New York, NY" --radius 10 --query "data scientist" --output json
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import requests
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

console = Console()

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    salary: str = ""
    date_posted: str = ""
    job_type: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_miles: Optional[float] = None

    @property
    def fingerprint(self) -> str:
        raw = f"{self.title.lower().strip()}|{self.company.lower().strip()}|{self.location.lower().strip()}"
        return hashlib.md5(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

_geocoder = Nominatim(user_agent="job-finder-extreme-v1", timeout=10)
_geocache: dict[str, tuple[float, float]] = {}


def geocode(location: str) -> tuple[float, float] | None:
    if location in _geocache:
        return _geocache[location]
    try:
        result = _geocoder.geocode(location)
        if result:
            coords = (result.latitude, result.longitude)
            _geocache[location] = coords
            return coords
    except Exception:
        pass
    return None


def within_radius(job_loc: str, center: tuple[float, float], radius_miles: float) -> tuple[bool, float | None]:
    """Check if a job location is within radius of center. Returns (in_range, distance)."""
    if not job_loc or not job_loc.strip():
        return True, None  # keep jobs with no location (remote, etc.)
    coords = geocode(job_loc)
    if not coords:
        return True, None  # keep if we can't geocode
    dist = geodesic(center, coords).miles
    return dist <= radius_miles, round(dist, 1)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Source 1: Adzuna API
# ---------------------------------------------------------------------------

async def fetch_adzuna(session: aiohttp.ClientSession, query: str, location: str, radius_km: int, config: dict) -> list[Job]:
    app_id = config.get("adzuna_app_id", "")
    api_key = config.get("adzuna_api_key", "")
    if not app_id or not api_key:
        console.print("  [dim]Adzuna: skipped (no API keys in config.json)[/dim]")
        return []

    jobs = []
    for page in range(1, 6):  # up to 5 pages
        url = f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": api_key,
            "results_per_page": 50,
            "what": query,
            "where": location,
            "distance": radius_km,
            "sort_by": "date",
            "content-type": "application/json",
        }
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    break
                for r in results:
                    jobs.append(Job(
                        title=r.get("title", "").strip(),
                        company=r.get("company", {}).get("display_name", "Unknown"),
                        location=r.get("location", {}).get("display_name", ""),
                        url=r.get("redirect_url", ""),
                        source="Adzuna",
                        description=r.get("description", "")[:500],
                        salary=_format_salary(r.get("salary_min"), r.get("salary_max")),
                        date_posted=r.get("created", "")[:10],
                        job_type=r.get("contract_time", ""),
                        lat=r.get("latitude"),
                        lon=r.get("longitude"),
                    ))
        except Exception as e:
            console.print(f"  [dim]Adzuna page {page} error: {e}[/dim]")
            break
    return jobs


# ---------------------------------------------------------------------------
# Source 2: JSearch (RapidAPI)
# ---------------------------------------------------------------------------

async def fetch_jsearch(session: aiohttp.ClientSession, query: str, location: str, radius_miles: int, config: dict) -> list[Job]:
    api_key = config.get("rapidapi_key", "")
    if not api_key:
        console.print("  [dim]JSearch: skipped (no rapidapi_key in config.json)[/dim]")
        return []

    jobs = []
    for page in range(1, 4):
        url = "https://jsearch.p.rapidapi.com/search"
        params = {
            "query": f"{query} in {location}",
            "page": str(page),
            "num_pages": "1",
            "date_posted": "month",
            "radius": str(radius_miles),
        }
        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        }
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                results = data.get("data", [])
                if not results:
                    break
                for r in results:
                    jobs.append(Job(
                        title=r.get("job_title", ""),
                        company=r.get("employer_name", "Unknown"),
                        location=f"{r.get('job_city', '')}, {r.get('job_state', '')}".strip(", "),
                        url=r.get("job_apply_link", "") or r.get("job_google_link", ""),
                        source="JSearch",
                        description=r.get("job_description", "")[:500],
                        salary=_js_salary(r),
                        date_posted=r.get("job_posted_at_datetime_utc", "")[:10],
                        job_type=r.get("job_employment_type", ""),
                        lat=r.get("job_latitude"),
                        lon=r.get("job_longitude"),
                    ))
        except Exception as e:
            console.print(f"  [dim]JSearch page {page} error: {e}[/dim]")
            break
    return jobs


# ---------------------------------------------------------------------------
# Source 3: Jooble API
# ---------------------------------------------------------------------------

async def fetch_jooble(session: aiohttp.ClientSession, query: str, location: str, radius_miles: int, config: dict) -> list[Job]:
    api_key = config.get("jooble_api_key", "")
    if not api_key:
        console.print("  [dim]Jooble: skipped (no jooble_api_key in config.json)[/dim]")
        return []

    jobs = []
    for page in range(1, 4):
        url = f"https://jooble.org/api/{api_key}"
        payload = {
            "keywords": query,
            "location": location,
            "radius": str(radius_miles),
            "page": str(page),
        }
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                results = data.get("jobs", [])
                if not results:
                    break
                for r in results:
                    jobs.append(Job(
                        title=r.get("title", ""),
                        company=r.get("company", "Unknown"),
                        location=r.get("location", ""),
                        url=r.get("link", ""),
                        source="Jooble",
                        description=r.get("snippet", "")[:500],
                        salary=r.get("salary", ""),
                        date_posted=r.get("updated", "")[:10],
                        job_type=r.get("type", ""),
                    ))
        except Exception as e:
            console.print(f"  [dim]Jooble page {page} error: {e}[/dim]")
            break
    return jobs


# ---------------------------------------------------------------------------
# Source 4: Remotive (free, no key)
# ---------------------------------------------------------------------------

async def fetch_remotive(session: aiohttp.ClientSession, query: str) -> list[Job]:
    url = "https://remotive.com/api/remote-jobs"
    params = {"search": query, "limit": 100}
    jobs = []
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            for r in data.get("jobs", []):
                jobs.append(Job(
                    title=r.get("title", ""),
                    company=r.get("company_name", "Unknown"),
                    location=r.get("candidate_required_location", "Remote"),
                    url=r.get("url", ""),
                    source="Remotive",
                    description=_strip_html(r.get("description", ""))[:500],
                    salary=r.get("salary", ""),
                    date_posted=r.get("publication_date", "")[:10],
                    job_type=r.get("job_type", ""),
                ))
    except Exception as e:
        console.print(f"  [dim]Remotive error: {e}[/dim]")
    return jobs


# ---------------------------------------------------------------------------
# Source 5: USAJobs (free, government)
# ---------------------------------------------------------------------------

async def fetch_usajobs(session: aiohttp.ClientSession, query: str, location: str, radius_miles: int) -> list[Job]:
    url = "https://data.usajobs.gov/api/search"
    params = {
        "Keyword": query,
        "LocationName": location,
        "Radius": str(radius_miles),
        "ResultsPerPage": 100,
    }
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": "job-finder-extreme/1.0 (contact@example.com)",
        "Authorization-Key": "",  # USAJobs works without a key for basic searches
    }
    jobs = []
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            items = data.get("SearchResult", {}).get("SearchResultItems", [])
            for item in items:
                m = item.get("MatchedObjectDescriptor", {})
                locs = m.get("PositionLocation", [])
                loc_str = locs[0].get("LocationName", "") if locs else ""
                lat = locs[0].get("Latitude", None) if locs else None
                lon = locs[0].get("Longitude", None) if locs else None
                sal = m.get("PositionRemuneration", [{}])
                sal_str = ""
                if sal:
                    sal_str = f"${sal[0].get('MinimumRange', '')} - ${sal[0].get('MaximumRange', '')} {sal[0].get('RateIntervalCode', '')}"
                jobs.append(Job(
                    title=m.get("PositionTitle", ""),
                    company=m.get("OrganizationName", "US Government"),
                    location=loc_str,
                    url=m.get("PositionURI", ""),
                    source="USAJobs",
                    description=_strip_html(m.get("UserArea", {}).get("Details", {}).get("MajorDuties", [""])[0] if m.get("UserArea", {}).get("Details", {}).get("MajorDuties") else "")[:500],
                    salary=sal_str,
                    date_posted=m.get("PublicationStartDate", "")[:10],
                    job_type=m.get("PositionSchedule", [{}])[0].get("Name", "") if m.get("PositionSchedule") else "",
                    lat=float(lat) if lat else None,
                    lon=float(lon) if lon else None,
                ))
    except Exception as e:
        console.print(f"  [dim]USAJobs error: {e}[/dim]")
    return jobs


# ---------------------------------------------------------------------------
# Sources 6-9: Google dorking scrapers (Indeed, LinkedIn, Glassdoor, etc.)
# ---------------------------------------------------------------------------

DORK_TARGETS = [
    ("Indeed", 'site:indeed.com/viewjob OR site:indeed.com/rc'),
    ("LinkedIn", 'site:linkedin.com/jobs/view'),
    ("Glassdoor", 'site:glassdoor.com/job-listing'),
    ("ZipRecruiter", 'site:ziprecruiter.com/c'),
    ("Dice", 'site:dice.com/job-detail'),
    ("SimplyHired", 'site:simplyhired.com/job'),
    ("BuiltIn", 'site:builtin.com/job'),
    ("WeWorkRemotely", 'site:weworkremotely.com/remote-jobs'),
]

GOOGLE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def fetch_google_dorks(session: aiohttp.ClientSession, query: str, location: str, radius_miles: int) -> list[Job]:
    """Scrape Google search results for job listings across major boards."""
    all_jobs = []

    for site_name, dork in DORK_TARGETS:
        full_query = f'{query} {dork} "{location}" jobs'
        encoded = urllib.parse.quote_plus(full_query)

        for start in range(0, 40, 10):  # 4 pages per site
            url = f"https://www.google.com/search?q={encoded}&start={start}&num=10"
            try:
                async with session.get(url, headers=GOOGLE_HEADERS) as resp:
                    if resp.status == 429:
                        console.print(f"  [dim]{site_name}: rate limited at page {start // 10 + 1}[/dim]")
                        break
                    if resp.status != 200:
                        break
                    html = await resp.text()
                    soup = BeautifulSoup(html, "lxml")

                    for g in soup.select("div.g, div[data-hveid]"):
                        link_el = g.select_one("a[href]")
                        title_el = g.select_one("h3")
                        snippet_el = g.select_one("div.VwiC3b, span.aCOpRe, div[data-sncf]")

                        if not link_el or not title_el:
                            continue

                        href = link_el.get("href", "")
                        if href.startswith("/url?q="):
                            href = href.split("/url?q=")[1].split("&")[0]
                            href = urllib.parse.unquote(href)

                        title_text = title_el.get_text(strip=True)
                        snippet_text = snippet_el.get_text(strip=True) if snippet_el else ""

                        # Extract company from title patterns like "Job Title - Company"
                        company = "Unknown"
                        for sep in [" - ", " | ", " at ", " — "]:
                            if sep in title_text:
                                parts = title_text.split(sep)
                                if len(parts) >= 2:
                                    company = parts[-1].strip()
                                    title_text = sep.join(parts[:-1]).strip()
                                break

                        all_jobs.append(Job(
                            title=title_text,
                            company=company,
                            location=_extract_location(snippet_text, location),
                            url=href,
                            source=site_name,
                            description=snippet_text[:500],
                        ))

                    # Don't hammer Google
                    await asyncio.sleep(1.5)

            except Exception as e:
                console.print(f"  [dim]{site_name} page {start // 10 + 1} error: {e}[/dim]")
                break

    return all_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_salary(min_sal, max_sal) -> str:
    if min_sal and max_sal:
        return f"${int(min_sal):,} - ${int(max_sal):,}"
    if min_sal:
        return f"${int(min_sal):,}+"
    if max_sal:
        return f"Up to ${int(max_sal):,}"
    return ""


def _js_salary(r: dict) -> str:
    min_s = r.get("job_min_salary")
    max_s = r.get("job_max_salary")
    period = r.get("job_salary_period", "")
    if min_s and max_s:
        return f"${int(min_s):,} - ${int(max_s):,} {period}".strip()
    return ""


def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(separator=" ", strip=True)


def _extract_location(text: str, fallback: str) -> str:
    """Try to pull a city/state from snippet text."""
    patterns = [
        r'(?:in|at|near)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return fallback


# ---------------------------------------------------------------------------
# Deduplication & filtering
# ---------------------------------------------------------------------------

def deduplicate(jobs: list[Job]) -> list[Job]:
    seen = set()
    unique = []
    for job in jobs:
        fp = job.fingerprint
        if fp not in seen:
            seen.add(fp)
            unique.append(job)
    return unique


def filter_by_radius(jobs: list[Job], center: tuple[float, float], radius_miles: float) -> list[Job]:
    filtered = []
    for job in jobs:
        # If we already have lat/lon from the API, use it
        if job.lat and job.lon:
            dist = geodesic(center, (job.lat, job.lon)).miles
            if dist <= radius_miles:
                job.distance_miles = round(dist, 1)
                filtered.append(job)
        else:
            in_range, dist = within_radius(job.location, center, radius_miles)
            if in_range:
                job.distance_miles = dist
                filtered.append(job)
    return filtered


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def export_csv(jobs: list[Job], filepath: str):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "title", "company", "location", "salary", "job_type",
            "date_posted", "source", "distance_miles", "url", "description",
        ])
        writer.writeheader()
        for j in sorted(jobs, key=lambda x: x.distance_miles or 9999):
            d = asdict(j)
            del d["lat"]
            del d["lon"]
            del d["fingerprint"]  # not a real field, but asdict won't include properties
            writer.writerow({k: d.get(k, "") for k in writer.fieldnames})


def export_json(jobs: list[Job], filepath: str):
    data = []
    for j in sorted(jobs, key=lambda x: x.distance_miles or 9999):
        d = asdict(j)
        d.pop("lat", None)
        d.pop("lon", None)
        data.append(d)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def display_table(jobs: list[Job], limit: int = 50):
    table = Table(title=f"Found {len(jobs)} Jobs", show_lines=False, expand=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold cyan", max_width=40)
    table.add_column("Company", style="green", max_width=25)
    table.add_column("Location", max_width=25)
    table.add_column("Salary", style="yellow", max_width=20)
    table.add_column("Dist", style="magenta", width=6)
    table.add_column("Source", style="dim", width=12)

    for i, j in enumerate(sorted(jobs, key=lambda x: x.distance_miles or 9999)[:limit], 1):
        dist = f"{j.distance_miles}mi" if j.distance_miles is not None else "?"
        table.add_row(str(i), j.title[:40], j.company[:25], j.location[:25], j.salary[:20], dist, j.source)

    console.print(table)
    if len(jobs) > limit:
        console.print(f"  [dim]...and {len(jobs) - limit} more (see export file)[/dim]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args):
    config = load_config()

    console.print(Panel.fit(
        f"[bold]Job Finder[/bold] — Extreme Multi-Source Scraper\n"
        f"Query: [cyan]{args.query}[/cyan]\n"
        f"Location: [cyan]{args.location}[/cyan]\n"
        f"Radius: [cyan]{args.radius} miles[/cyan]",
        border_style="blue",
    ))

    # Geocode center point
    with console.status("Geocoding location..."):
        center = geocode(args.location)
        if not center:
            console.print(f"[red]Could not geocode '{args.location}'. Try a more specific location.[/red]")
            sys.exit(1)
        console.print(f"  Center: {center[0]:.4f}, {center[1]:.4f}")

    radius_km = int(args.radius * 1.60934)
    all_jobs: list[Job] = []

    async with aiohttp.ClientSession() as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            # Launch all sources concurrently
            tasks = {}

            t = progress.add_task("Adzuna API...", total=None)
            tasks[t] = fetch_adzuna(session, args.query, args.location, radius_km, config)

            t = progress.add_task("JSearch API...", total=None)
            tasks[t] = fetch_jsearch(session, args.query, args.location, args.radius, config)

            t = progress.add_task("Jooble API...", total=None)
            tasks[t] = fetch_jooble(session, args.query, args.location, args.radius, config)

            t = progress.add_task("Remotive (remote jobs)...", total=None)
            tasks[t] = fetch_remotive(session, args.query)

            t = progress.add_task("USAJobs (government)...", total=None)
            tasks[t] = fetch_usajobs(session, args.query, args.location, args.radius)

            t = progress.add_task("Google dorking (8 job boards)...", total=None)
            tasks[t] = fetch_google_dorks(session, args.query, args.location, args.radius)

            results = await asyncio.gather(*tasks.values(), return_exceptions=True)

            for task_id, result in zip(tasks.keys(), results):
                if isinstance(result, Exception):
                    progress.update(task_id, description=f"[red]Error: {result}[/red]")
                else:
                    progress.update(task_id, description=f"[green]Done ({len(result)} results)[/green]")
                    all_jobs.extend(result)

    console.print(f"\n  Raw results: [bold]{len(all_jobs)}[/bold] jobs from all sources")

    # Deduplicate
    all_jobs = deduplicate(all_jobs)
    console.print(f"  After dedup: [bold]{len(all_jobs)}[/bold] unique jobs")

    # Filter by radius
    if not args.no_filter:
        with console.status("Filtering by radius (geocoding locations)..."):
            all_jobs = filter_by_radius(all_jobs, center, args.radius)
        console.print(f"  In radius:   [bold]{len(all_jobs)}[/bold] jobs within {args.radius} miles\n")

    if not all_jobs:
        console.print("[yellow]No jobs found. Try a broader radius or different query.[/yellow]")
        return

    # Display
    display_table(all_jobs)

    # Export
    os.makedirs("results", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r'[^a-z0-9]+', '_', args.query.lower())[:30]

    if args.output in ("csv", "both"):
        csv_path = f"results/{slug}_{timestamp}.csv"
        export_csv(all_jobs, csv_path)
        console.print(f"\n  Exported: [green]{csv_path}[/green]")

    if args.output in ("json", "both"):
        json_path = f"results/{slug}_{timestamp}.json"
        export_json(all_jobs, json_path)
        console.print(f"  Exported: [green]{json_path}[/green]")


def main():
    parser = argparse.ArgumentParser(
        description="Extreme multi-source job scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python job_finder.py --location "Austin, TX" --radius 25
  python job_finder.py -l "San Francisco, CA" -r 50 -q "software engineer"
  python job_finder.py -l "Remote" -r 9999 -q "python developer" --no-filter
  python job_finder.py -l "NYC" -r 10 -q "data scientist" -o json
        """,
    )
    parser.add_argument("-l", "--location", required=True, help="City, State or address")
    parser.add_argument("-r", "--radius", type=int, default=25, help="Search radius in miles (default: 25)")
    parser.add_argument("-q", "--query", default="", help="Job title or keywords (default: all jobs)")
    parser.add_argument("-o", "--output", choices=["csv", "json", "both"], default="both", help="Output format")
    parser.add_argument("--no-filter", action="store_true", help="Skip radius filtering (keep all results)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
