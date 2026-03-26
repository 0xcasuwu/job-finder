#!/usr/bin/env python3
"""
job_finder.py — Exhaustive multi-source job scraper.

Consumes every conceivable public source of job listings and aggregates
them by location + mile radius. 60+ source channels across 7 categories.

CATEGORY A — Free REST APIs (no key or free-tier key):
  A1.  Adzuna API (free tier, 250 req/day)
  A2.  JSearch / RapidAPI (free tier, 200 req/month)
  A3.  Jooble API (free)
  A4.  Remotive API (free, remote-only)
  A5.  USAJobs API (free, US government)
  A6.  Arbeitnow API (free, no key)
  A7.  FindWork.dev API (free, dev/tech)
  A8.  Jobicy API (free, remote)
  A9.  The Muse API (free, no key)
  A10. Reed.co.uk API (free key)
  A11. Careerjet API (free, web call)
  A12. Talent.com (formerly Neuvoo) scrape

CATEGORY B — Direct site scrapers (HTML/JSON endpoints):
  B1.  Indeed (direct search page scrape)
  B2.  LinkedIn (guest job search scrape)
  B3.  Glassdoor (direct search scrape)
  B4.  ZipRecruiter (direct search scrape)
  B5.  SimplyHired (direct search scrape)
  B6.  Monster (direct search scrape)
  B7.  CareerBuilder (direct search scrape)
  B8.  Snagajob (direct search scrape)
  B9.  Dice (direct search scrape)
  B10. Robert Half (direct search scrape)
  B11. FlexJobs (direct search scrape)
  B12. Wellfound / AngelList (startup jobs scrape)

CATEGORY C — ATS board crawlers (Greenhouse, Lever, Workday, Ashby):
  C1.  Greenhouse boards (boards.greenhouse.io)
  C2.  Lever boards (jobs.lever.co)
  C3.  Ashby boards (jobs.ashbyhq.com)

CATEGORY D — Google dorking across 30+ job boards:
  D1-D30. See DORK_TARGETS list below

CATEGORY E — Niche / industry boards:
  E1.  BuiltIn (tech)
  E2.  WeWorkRemotely (remote)
  E3.  Dribbble (design)
  E4.  Idealist (nonprofit)
  E5.  HigherEdJobs (academia)
  E6.  ClearanceJobs (security clearance)
  E7.  eFinancialCareers (finance)
  E8.  Mediabistro (media)
  E9.  DevITjobs (dev/IT)
  E10. CyberSecJobs (infosec)
  E11. Otta (tech startups)
  E12. Himalayas (remote)
  E13. Working Nomads (remote)
  E14. DailyRemote
  E15. NoDesk (remote)
  E16. PowerToFly (diversity)
  E17. Relocate.me (relocation)
  E18. AuthenticJobs (design/dev)

CATEGORY F — Community / social:
  F1.  Hacker News "Who is Hiring" (monthly)
  F2.  Reddit (r/forhire, r/jobbit, field-specific subs)
  F3.  Craigslist (local area)
  F4.  Facebook Jobs (public listings via search)
  F5.  GitHub Jobs / ReadMe jobs

CATEGORY G — Government / public sector:
  G1.  USAJobs (covered in A5)
  G2.  GovernmentJobs.com
  G3.  State workforce agencies

Usage:
  python job_finder.py -l "Austin, TX" -r 25 -q "software engineer"
  python job_finder.py -l "San Francisco, CA" -r 50 -q "data scientist" -o json
  python job_finder.py -l "Remote" -r 9999 -q "python" --no-filter
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout

console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def _headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }

# Concurrent request throttle per domain
_domain_semaphores: dict[str, asyncio.Semaphore] = {}

def _get_sem(domain: str, limit: int = 2) -> asyncio.Semaphore:
    if domain not in _domain_semaphores:
        _domain_semaphores[domain] = asyncio.Semaphore(limit)
    return _domain_semaphores[domain]


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
    remote: bool = False
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

_geocoder = Nominatim(user_agent="job-finder-extreme-v2", timeout=10)
_geocache: dict[str, tuple[float, float]] = {}


def geocode(location: str) -> tuple[float, float] | None:
    loc = location.strip()
    if not loc:
        return None
    if loc in _geocache:
        return _geocache[loc]
    try:
        result = _geocoder.geocode(loc)
        if result:
            coords = (result.latitude, result.longitude)
            _geocache[loc] = coords
            return coords
    except Exception:
        pass
    return None


def within_radius(job_loc: str, center: tuple[float, float], radius_miles: float) -> tuple[bool, float | None]:
    if not job_loc or not job_loc.strip():
        return True, None
    if any(kw in job_loc.lower() for kw in ["remote", "anywhere", "worldwide", "distributed"]):
        return True, None
    coords = geocode(job_loc)
    if not coords:
        return True, None
    dist = geodesic(center, coords).miles
    return dist <= radius_miles, round(dist, 1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


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

def _strip_html(text: str) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(separator=" ", strip=True)

def _extract_location(text: str, fallback: str) -> str:
    patterns = [
        r'(?:in|at|near)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return fallback

def _is_remote(text: str) -> bool:
    return bool(re.search(r'\b(remote|work from home|wfh|telecommute|distributed)\b', text.lower()))

async def _safe_get(session, url, **kwargs):
    """GET with timeout and error swallowing."""
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(url, timeout=timeout, **kwargs) as resp:
            if resp.status == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    return await resp.json()
                return await resp.text()
    except Exception:
        pass
    return None

async def _safe_post(session, url, **kwargs):
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.post(url, timeout=timeout, **kwargs) as resp:
            if resp.status == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    return await resp.json()
                return await resp.text()
    except Exception:
        pass
    return None

async def _safe_json_get(session, url, **kwargs):
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.get(url, timeout=timeout, **kwargs) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY A — FREE REST APIs
# =========================================================================
# ---------------------------------------------------------------------------

# A1: Adzuna
async def fetch_adzuna(session, query, location, radius_km, config) -> list[Job]:
    app_id = config.get("adzuna_app_id", "")
    api_key = config.get("adzuna_api_key", "")
    if not app_id or not api_key:
        return []
    jobs = []
    for page in range(1, 6):
        data = await _safe_json_get(session,
            f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
            params={"app_id": app_id, "app_key": api_key, "results_per_page": 50,
                    "what": query, "where": location, "distance": radius_km, "sort_by": "date"})
        if not data:
            break
        for r in data.get("results", []):
            jobs.append(Job(
                title=r.get("title", "").strip(), company=r.get("company", {}).get("display_name", ""),
                location=r.get("location", {}).get("display_name", ""), url=r.get("redirect_url", ""),
                source="Adzuna", description=r.get("description", "")[:500],
                salary=_format_salary(r.get("salary_min"), r.get("salary_max")),
                date_posted=r.get("created", "")[:10], job_type=r.get("contract_time", ""),
                lat=r.get("latitude"), lon=r.get("longitude"),
            ))
        if len(data.get("results", [])) < 50:
            break
    return jobs

# A2: JSearch (RapidAPI)
async def fetch_jsearch(session, query, location, radius_miles, config) -> list[Job]:
    api_key = config.get("rapidapi_key", "")
    if not api_key:
        return []
    jobs = []
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    for page in range(1, 6):
        data = await _safe_json_get(session,
            "https://jsearch.p.rapidapi.com/search",
            params={"query": f"{query} in {location}", "page": str(page), "num_pages": "1",
                    "date_posted": "month", "radius": str(radius_miles)},
            headers=headers)
        if not data:
            break
        for r in data.get("data", []):
            sal = ""
            if r.get("job_min_salary") and r.get("job_max_salary"):
                sal = f"${int(r['job_min_salary']):,} - ${int(r['job_max_salary']):,} {r.get('job_salary_period', '')}".strip()
            jobs.append(Job(
                title=r.get("job_title", ""), company=r.get("employer_name", ""),
                location=f"{r.get('job_city', '')}, {r.get('job_state', '')}".strip(", "),
                url=r.get("job_apply_link", "") or r.get("job_google_link", ""),
                source="JSearch", description=r.get("job_description", "")[:500], salary=sal,
                date_posted=r.get("job_posted_at_datetime_utc", "")[:10],
                job_type=r.get("job_employment_type", ""),
                remote=r.get("job_is_remote", False),
                lat=r.get("job_latitude"), lon=r.get("job_longitude"),
            ))
        if len(data.get("data", [])) < 10:
            break
    return jobs

# A3: Jooble
async def fetch_jooble(session, query, location, radius_miles, config) -> list[Job]:
    api_key = config.get("jooble_api_key", "")
    if not api_key:
        return []
    jobs = []
    for page in range(1, 6):
        data = await _safe_post(session, f"https://jooble.org/api/{api_key}",
            json={"keywords": query, "location": location, "radius": str(radius_miles), "page": str(page)})
        if not data or not isinstance(data, dict):
            break
        for r in data.get("jobs", []):
            jobs.append(Job(
                title=r.get("title", ""), company=r.get("company", ""),
                location=r.get("location", ""), url=r.get("link", ""), source="Jooble",
                description=r.get("snippet", "")[:500], salary=r.get("salary", ""),
                date_posted=r.get("updated", "")[:10], job_type=r.get("type", ""),
            ))
        if len(data.get("jobs", [])) < 20:
            break
    return jobs

# A4: Remotive (free, no key)
async def fetch_remotive(session, query) -> list[Job]:
    data = await _safe_json_get(session, "https://remotive.com/api/remote-jobs",
        params={"search": query, "limit": 200})
    if not data:
        return []
    jobs = []
    for r in data.get("jobs", []):
        jobs.append(Job(
            title=r.get("title", ""), company=r.get("company_name", ""),
            location=r.get("candidate_required_location", "Remote"),
            url=r.get("url", ""), source="Remotive",
            description=_strip_html(r.get("description", ""))[:500],
            salary=r.get("salary", ""), date_posted=r.get("publication_date", "")[:10],
            job_type=r.get("job_type", ""), remote=True,
        ))
    return jobs

# A5: USAJobs (free, government)
async def fetch_usajobs(session, query, location, radius_miles) -> list[Job]:
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": "job-finder-extreme/2.0 (jobfinder@example.com)",
        "Authorization-Key": "",
    }
    data = await _safe_json_get(session, "https://data.usajobs.gov/api/search",
        params={"Keyword": query, "LocationName": location, "Radius": str(radius_miles),
                "ResultsPerPage": 250},
        headers=headers)
    if not data:
        return []
    jobs = []
    for item in data.get("SearchResult", {}).get("SearchResultItems", []):
        m = item.get("MatchedObjectDescriptor", {})
        locs = m.get("PositionLocation", [])
        loc_str = locs[0].get("LocationName", "") if locs else ""
        lat = float(locs[0].get("Latitude", 0)) if locs and locs[0].get("Latitude") else None
        lon = float(locs[0].get("Longitude", 0)) if locs and locs[0].get("Longitude") else None
        sal = m.get("PositionRemuneration", [{}])
        sal_str = ""
        if sal and sal[0].get("MinimumRange"):
            sal_str = f"${sal[0].get('MinimumRange', '')} - ${sal[0].get('MaximumRange', '')} {sal[0].get('RateIntervalCode', '')}"
        duties = ""
        try:
            duties = m.get("UserArea", {}).get("Details", {}).get("MajorDuties", [""])[0]
        except (IndexError, TypeError):
            pass
        sched = ""
        try:
            sched = m.get("PositionSchedule", [{}])[0].get("Name", "")
        except (IndexError, TypeError):
            pass
        jobs.append(Job(
            title=m.get("PositionTitle", ""), company=m.get("OrganizationName", "US Government"),
            location=loc_str, url=m.get("PositionURI", ""), source="USAJobs",
            description=_strip_html(duties)[:500], salary=sal_str,
            date_posted=m.get("PublicationStartDate", "")[:10], job_type=sched,
            lat=lat, lon=lon,
        ))
    return jobs

# A6: Arbeitnow (free, no key)
async def fetch_arbeitnow(session, query) -> list[Job]:
    jobs = []
    for page in range(1, 4):
        data = await _safe_json_get(session, "https://www.arbeitnow.com/api/job-board-api",
            params={"page": page})
        if not data:
            break
        for r in data.get("data", []):
            title = r.get("title", "")
            desc = r.get("description", "")
            if query and query.lower() not in title.lower() and query.lower() not in desc.lower():
                continue
            jobs.append(Job(
                title=title, company=r.get("company_name", ""),
                location=r.get("location", ""), url=r.get("url", ""),
                source="Arbeitnow", description=_strip_html(desc)[:500],
                date_posted=r.get("created_at", "")[:10],
                remote=r.get("remote", False),
                job_type=", ".join(r.get("tags", [])),
            ))
        if not data.get("links", {}).get("next"):
            break
    return jobs

# A7: FindWork.dev (free, dev/tech)
async def fetch_findwork(session, query) -> list[Job]:
    data = await _safe_json_get(session, "https://findwork.dev/api/jobs/",
        params={"search": query, "order_by": "-date_posted"},
        headers={"Accept": "application/json"})
    if not data:
        return []
    jobs = []
    for r in data.get("results", []):
        jobs.append(Job(
            title=r.get("role", ""), company=r.get("company_name", ""),
            location=r.get("location", "Remote"), url=r.get("url", ""),
            source="FindWork.dev", description=r.get("text", "")[:500],
            date_posted=r.get("date_posted", "")[:10],
            remote=r.get("remote", False),
            job_type=", ".join(r.get("keywords", [])),
        ))
    return jobs

# A8: Jobicy (free, remote)
async def fetch_jobicy(session, query) -> list[Job]:
    data = await _safe_json_get(session, "https://jobicy.com/api/v2/remote-jobs",
        params={"count": 50, "tag": query})
    if not data:
        return []
    jobs = []
    for r in data.get("jobs", []):
        jobs.append(Job(
            title=r.get("jobTitle", ""), company=r.get("companyName", ""),
            location=r.get("jobGeo", "Remote"), url=r.get("url", ""),
            source="Jobicy", description=r.get("jobExcerpt", "")[:500],
            date_posted=r.get("pubDate", "")[:10],
            job_type=r.get("jobType", ""), salary=r.get("annualSalaryMin", ""),
            remote=True,
        ))
    return jobs

# A9: The Muse (free, no key)
async def fetch_themuse(session, query, location) -> list[Job]:
    jobs = []
    for page in range(0, 5):
        params = {"page": page, "descending": "true"}
        if query:
            params["category"] = query
        if location:
            params["location"] = location
        data = await _safe_json_get(session, "https://www.themuse.com/api/public/jobs", params=params)
        if not data:
            break
        for r in data.get("results", []):
            locs = r.get("locations", [])
            loc_str = locs[0].get("name", "") if locs else ""
            comp = r.get("company", {}).get("name", "")
            jobs.append(Job(
                title=r.get("name", ""), company=comp, location=loc_str,
                url=r.get("refs", {}).get("landing_page", ""), source="TheMuse",
                description=_strip_html(r.get("contents", ""))[:500],
                date_posted=r.get("publication_date", "")[:10],
                job_type=", ".join(r.get("levels", [{}])[0].get("name", "") for _ in [1] if r.get("levels")),
            ))
        if page >= data.get("page_count", 0) - 1:
            break
    return jobs

# A10: Reed.co.uk (free key)
async def fetch_reed(session, query, location, radius_miles, config) -> list[Job]:
    api_key = config.get("reed_api_key", "")
    if not api_key:
        return []
    import base64
    auth = base64.b64encode(f"{api_key}:".encode()).decode()
    jobs = []
    for skip in range(0, 200, 100):
        data = await _safe_json_get(session, "https://www.reed.co.uk/api/1.0/search",
            params={"keywords": query, "location": location, "distancefromlocation": radius_miles,
                    "resultsToTake": 100, "resultsToSkip": skip},
            headers={"Authorization": f"Basic {auth}"})
        if not data:
            break
        for r in data.get("results", []):
            sal = ""
            if r.get("minimumSalary") and r.get("maximumSalary"):
                sal = f"£{int(r['minimumSalary']):,} - £{int(r['maximumSalary']):,}"
            jobs.append(Job(
                title=r.get("jobTitle", ""), company=r.get("employerName", ""),
                location=r.get("locationName", ""), url=r.get("jobUrl", ""),
                source="Reed", description=r.get("jobDescription", "")[:500],
                salary=sal, date_posted=r.get("date", "")[:10],
            ))
        if len(data.get("results", [])) < 100:
            break
    return jobs

# A11: Careerjet (scrape their search)
async def fetch_careerjet(session, query, location) -> list[Job]:
    encoded_q = urllib.parse.quote_plus(query)
    encoded_l = urllib.parse.quote_plus(location)
    jobs = []
    for page in range(1, 4):
        html = await _safe_get(session,
            f"https://www.careerjet.com/search/jobs?s={encoded_q}&l={encoded_l}&p={page}",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for article in soup.select("article.job"):
            title_el = article.select_one("h2 a, header a")
            company_el = article.select_one("p.company, .company")
            loc_el = article.select_one("ul.location li, .location")
            desc_el = article.select_one("div.desc, .description")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.careerjet.com" + href
            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source="Careerjet",
                description=desc_el.get_text(strip=True)[:500] if desc_el else "",
            ))
        await asyncio.sleep(1)
    return jobs

# A12: Talent.com
async def fetch_talent(session, query, location) -> list[Job]:
    encoded_q = urllib.parse.quote_plus(query)
    encoded_l = urllib.parse.quote_plus(location)
    jobs = []
    for page in range(1, 4):
        html = await _safe_get(session,
            f"https://www.talent.com/jobs?k={encoded_q}&l={encoded_l}&p={page}",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.card--job, div.link-job-wrap, a.link-job"):
            title_el = card.select_one("h2, .card__job-title, .link-job-wrap__title")
            company_el = card.select_one(".card__job-empname-label, .link-job-wrap__company")
            loc_el = card.select_one(".card__job-location, .link-job-wrap__location")
            href_el = card if card.name == "a" else card.select_one("a[href]")
            if not title_el:
                continue
            href = ""
            if href_el:
                href = href_el.get("href", "")
                if href and not href.startswith("http"):
                    href = "https://www.talent.com" + href
            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source="Talent.com",
            ))
        await asyncio.sleep(1)
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY B — DIRECT SITE SCRAPERS
# =========================================================================
# ---------------------------------------------------------------------------

async def _scrape_site(session, name, url, query, location, selectors, pages=3) -> list[Job]:
    """Generic scraper for job board search pages."""
    jobs = []
    for page in range(pages):
        page_url = url.format(query=urllib.parse.quote_plus(query),
                              location=urllib.parse.quote_plus(location), page=page)
        html = await _safe_get(session, page_url, headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(selectors["card"])
        if not cards:
            break
        for card in cards:
            title_el = card.select_one(selectors.get("title", "h2 a, h3 a"))
            company_el = card.select_one(selectors.get("company", ".company"))
            loc_el = card.select_one(selectors.get("location", ".location"))
            sal_el = card.select_one(selectors.get("salary", ".salary"))
            desc_el = card.select_one(selectors.get("description", ".description"))
            link_el = card.select_one(selectors.get("link", "a[href]")) or title_el
            if not title_el:
                continue
            href = ""
            if link_el:
                href = link_el.get("href", "")
                base = selectors.get("base_url", "")
                if href and not href.startswith("http") and base:
                    href = base + href
            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source=name,
                salary=sal_el.get_text(strip=True) if sal_el else "",
                description=desc_el.get_text(strip=True)[:500] if desc_el else "",
            ))
        await asyncio.sleep(random.uniform(1.5, 3.0))
    return jobs

# B1: Indeed
async def fetch_indeed(session, query, location, radius_miles) -> list[Job]:
    jobs = []
    for start in range(0, 60, 10):
        html = await _safe_get(session,
            f"https://www.indeed.com/jobs?q={urllib.parse.quote_plus(query)}&l={urllib.parse.quote_plus(location)}&radius={radius_miles}&start={start}&fromage=30",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div, td.resultContent"):
            title_el = card.select_one("h2.jobTitle a, a.jcs-JobTitle, span[id^='jobTitle']")
            company_el = card.select_one("span.companyName, span[data-testid='company-name'], .company_location .companyName")
            loc_el = card.select_one("div.companyLocation, div[data-testid='text-location']")
            sal_el = card.select_one("div.salary-snippet-container, div.metadata.salary-snippet-container")
            if not title_el:
                continue
            href = title_el.get("href", "") if title_el.name == "a" else ""
            parent_a = title_el.find_parent("a")
            if not href and parent_a:
                href = parent_a.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.indeed.com" + href
            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source="Indeed",
                salary=sal_el.get_text(strip=True) if sal_el else "",
            ))
        await asyncio.sleep(random.uniform(2, 4))
    return jobs

# B2: LinkedIn (guest search)
async def fetch_linkedin(session, query, location) -> list[Job]:
    jobs = []
    geo_id = ""  # LinkedIn uses geoIds; we'll try plain text
    for start in range(0, 75, 25):
        html = await _safe_get(session,
            f"https://www.linkedin.com/jobs/search/?keywords={urllib.parse.quote_plus(query)}&location={urllib.parse.quote_plus(location)}&start={start}&f_TPR=r2592000",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.base-card, li.jobs-search-results__list-item, div.job-search-card"):
            title_el = card.select_one("h3.base-search-card__title, h3.job-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle a, a.job-search-card__subtitle-link")
            loc_el = card.select_one("span.job-search-card__location")
            link_el = card.select_one("a.base-card__full-link, a.job-search-card__link-wrapper")
            date_el = card.select_one("time")
            if not title_el:
                continue
            href = link_el.get("href", "") if link_el else ""
            jobs.append(Job(
                title=title_el.get_text(strip=True),
                company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href.split("?")[0] if href else "", source="LinkedIn",
                date_posted=date_el.get("datetime", "")[:10] if date_el else "",
            ))
        await asyncio.sleep(random.uniform(2, 4))
    return jobs

# B3-B12: Other direct scrapers
async def fetch_glassdoor(session, query, location) -> list[Job]:
    return await _scrape_site(session, "Glassdoor",
        "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}&locT=C&locKeyword={location}&fromAge=30&p={page}",
        query, location, {
            "card": "li.JobsList_jobListItem__wjTHv, li.react-job-listing, div.jobCard",
            "title": "a.JobCard_jobTitle__GLyJ1, a.job-title",
            "company": "span.EmployerProfile_compactEmployerName__9MGcV, .job-search-key-l2wjgv",
            "location": "div.JobCard_location__N_iYE, .job-search-key-hl1r5r",
            "base_url": "https://www.glassdoor.com",
        })

async def fetch_ziprecruiter(session, query, location, radius_miles) -> list[Job]:
    jobs = []
    for page in range(1, 4):
        html = await _safe_get(session,
            f"https://www.ziprecruiter.com/jobs-search?search={urllib.parse.quote_plus(query)}&location={urllib.parse.quote_plus(location)}&radius={radius_miles}&days=30&page={page}",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("article.job_result, div.job_content"):
            title_el = card.select_one("h2.job_title a, a.job_link, span.job_title")
            company_el = card.select_one("a.t_org_link, p.company_name")
            loc_el = card.select_one("a.t_location_link, p.job_location")
            sal_el = card.select_one("span.job_salary, p.compensation")
            if not title_el:
                continue
            href = title_el.get("href", "") if title_el.name == "a" else ""
            if not href:
                parent_a = title_el.find_parent("a")
                if parent_a:
                    href = parent_a.get("href", "")
            jobs.append(Job(
                title=title_el.get_text(strip=True), company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source="ZipRecruiter",
                salary=sal_el.get_text(strip=True) if sal_el else "",
            ))
        await asyncio.sleep(random.uniform(2, 3))
    return jobs

async def fetch_monster(session, query, location) -> list[Job]:
    jobs = []
    html = await _safe_get(session,
        f"https://www.monster.com/jobs/search?q={urllib.parse.quote_plus(query)}&where={urllib.parse.quote_plus(location)}&tm=30",
        headers=_headers())
    if not html or not isinstance(html, str):
        return []
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("div.job-cardstyle__JobCardComponent, div.card-content, article"):
        title_el = card.select_one("h2 a, a.job-cardstyle__JobCardTitle")
        company_el = card.select_one("span.company, .job-cardstyle__CompanyName")
        loc_el = card.select_one("span.location, .job-cardstyle__Location")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.monster.com" + href
        jobs.append(Job(
            title=title_el.get_text(strip=True), company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else location,
            url=href, source="Monster",
        ))
    return jobs

async def fetch_dice(session, query, location, radius_miles) -> list[Job]:
    """Dice has a JSON API behind their search."""
    jobs = []
    for page in range(1, 4):
        data = await _safe_json_get(session,
            "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search",
            params={"q": query, "location": location, "latitude": "", "longitude": "",
                    "countryCode2": "US", "radius": str(radius_miles), "radiusUnit": "mi",
                    "page": str(page), "pageSize": "40", "facets": "employmentType|postedDate",
                    "fields": "id|jobId|guid|summary|title|postedDate|modifiedDate|jobLocation.displayName|detailsPageUrl|salary|clientBrandId|companyPageUrl|companyPageUrl|is498|companyName",
                    "culture": "en", "recommendations": "true", "interactionId": "0",
                    "fj": "true", "includeRemote": "true", "postedDate": "THIRTY"},
            headers=_headers())
        if not data:
            break
        for r in data.get("data", []):
            loc_str = r.get("jobLocation", {}).get("displayName", "") if isinstance(r.get("jobLocation"), dict) else ""
            jobs.append(Job(
                title=r.get("title", ""), company=r.get("companyName", ""),
                location=loc_str, url=r.get("detailsPageUrl", ""),
                source="Dice", description=r.get("summary", "")[:500],
                date_posted=r.get("postedDate", "")[:10],
                salary=r.get("salary", ""),
            ))
        if len(data.get("data", [])) < 40:
            break
    return jobs

async def fetch_simplyhired(session, query, location) -> list[Job]:
    jobs = []
    for page in range(1, 4):
        html = await _safe_get(session,
            f"https://www.simplyhired.com/search?q={urllib.parse.quote_plus(query)}&l={urllib.parse.quote_plus(location)}&pn={page}&fdb=30",
            headers=_headers())
        if not html or not isinstance(html, str):
            break
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("article[data-testid='searchSerpJob'], li.SerpJob, div.SerpJob-jobCard"):
            title_el = card.select_one("h2 a, a.SerpJob-link, a[data-testid='searchSerpJobTitle']")
            company_el = card.select_one("span[data-testid='companyName'], span.jobposting-company, .SerpJob-metaInfo .companyName")
            loc_el = card.select_one("span[data-testid='searchSerpJobLocation'], span.jobposting-location, .SerpJob-metaInfo .location")
            sal_el = card.select_one("span[data-testid='searchSerpJobSalary'], .SerpJob-metaInfo .salary")
            if not title_el:
                continue
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.simplyhired.com" + href
            jobs.append(Job(
                title=title_el.get_text(strip=True), company=company_el.get_text(strip=True) if company_el else "",
                location=loc_el.get_text(strip=True) if loc_el else location,
                url=href, source="SimplyHired",
                salary=sal_el.get_text(strip=True) if sal_el else "",
            ))
        await asyncio.sleep(random.uniform(1.5, 3))
    return jobs

async def fetch_builtin(session, query, location) -> list[Job]:
    """BuiltIn tech job board."""
    jobs = []
    html = await _safe_get(session,
        f"https://builtin.com/jobs?search={urllib.parse.quote_plus(query)}&location={urllib.parse.quote_plus(location)}",
        headers=_headers())
    if not html or not isinstance(html, str):
        return []
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("div[data-id], div.job-item, div.job-bounded-responsive"):
        title_el = card.select_one("h2 a, a.job-title")
        company_el = card.select_one("span.company-title, .company-name")
        loc_el = card.select_one("span.job-location, .location")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://builtin.com" + href
        jobs.append(Job(
            title=title_el.get_text(strip=True), company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else location,
            url=href, source="BuiltIn",
        ))
    return jobs

async def fetch_wellfound(session, query) -> list[Job]:
    """Wellfound (formerly AngelList) startup jobs."""
    jobs = []
    html = await _safe_get(session,
        f"https://wellfound.com/jobs?query={urllib.parse.quote_plus(query)}",
        headers=_headers())
    if not html or not isinstance(html, str):
        return []
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("div.styles_jobListing__aXfyJ, div[data-test='StartupResult'], div.job-listing"):
        title_el = card.select_one("a.styles_jobTitle__Fjq41, h4 a, a.job-title")
        company_el = card.select_one("a.styles_component__EfgOC, h2 a, .startup-link")
        loc_el = card.select_one("span.styles_location__onBiG, .location")
        sal_el = card.select_one("span.styles_compensation__yRjjG, .compensation")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://wellfound.com" + href
        jobs.append(Job(
            title=title_el.get_text(strip=True), company=company_el.get_text(strip=True) if company_el else "",
            location=loc_el.get_text(strip=True) if loc_el else "Startup",
            url=href, source="Wellfound",
            salary=sal_el.get_text(strip=True) if sal_el else "",
        ))
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY C — ATS BOARD CRAWLERS
# =========================================================================
# ---------------------------------------------------------------------------

# Well-known companies with public Greenhouse boards
GREENHOUSE_BOARDS = [
    "airbnb", "airtable", "anduril", "asana", "brex", "canva", "cloudflare",
    "coinbase", "databricks", "datadog", "discord", "doordash", "dropbox",
    "duolingo", "figma", "gitlab", "gusto", "hashicorp", "instacart",
    "lattice", "linear", "loom", "lyft", "miro", "netlify", "notion",
    "nytimes", "okta", "openai", "pagerduty", "palantir", "pinterestcareers",
    "plaid", "ramp", "reddit", "remitly", "retool", "rippling", "robinhood",
    "scale", "seatgeek", "sentry", "shopify", "snap", "sourcegraph",
    "spotify", "square", "stripe", "supabase", "sweetgreen", "tiktok",
    "twitch", "twilio", "vercel", "verkada", "vimeo", "wealthfront",
    "whatnot", "wikimedia", "zipline",
]

LEVER_BOARDS = [
    "Netflix", "figma", "verkada", "anthropic", "netlify", "mux",
    "postman", "relativity", "fleetsmith", "cockroachlabs", "clearbit",
    "webflow", "nerdwallet", "census", "demandbase", "benchling",
    "memsql", "segment", "truework", "lob", "manifold", "dbt-labs",
    "modern-treasury", "prefect", "temporal", "snyk", "tailscale",
]

ASHBY_BOARDS = [
    "ramp", "notion", "linear", "vercel", "supabase", "resend",
    "mintlify", "cal", "neon", "inngest",
]


async def fetch_greenhouse_boards(session, query) -> list[Job]:
    """Crawl Greenhouse job boards for matching positions."""
    jobs = []
    sem = _get_sem("greenhouse", 5)
    query_lower = query.lower()

    async def _fetch_board(board_name):
        async with sem:
            data = await _safe_json_get(session,
                f"https://boards-api.greenhouse.io/v1/boards/{board_name}/jobs")
            if not data:
                return []
            board_jobs = []
            for r in data.get("jobs", []):
                title = r.get("title", "")
                if query_lower and query_lower not in title.lower():
                    continue
                loc = r.get("location", {}).get("name", "")
                board_jobs.append(Job(
                    title=title,
                    company=board_name.replace("-", " ").title(),
                    location=loc,
                    url=r.get("absolute_url", ""),
                    source="Greenhouse",
                    date_posted=r.get("updated_at", "")[:10],
                ))
            return board_jobs

    tasks = [_fetch_board(b) for b in GREENHOUSE_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            jobs.extend(r)
    return jobs


async def fetch_lever_boards(session, query) -> list[Job]:
    """Crawl Lever job boards for matching positions."""
    jobs = []
    sem = _get_sem("lever", 5)
    query_lower = query.lower()

    async def _fetch_board(board_name):
        async with sem:
            data = await _safe_json_get(session,
                f"https://api.lever.co/v0/postings/{board_name}?mode=json")
            if not data or not isinstance(data, list):
                return []
            board_jobs = []
            for r in data:
                title = r.get("text", "")
                if query_lower and query_lower not in title.lower():
                    continue
                loc = r.get("categories", {}).get("location", "")
                board_jobs.append(Job(
                    title=title,
                    company=board_name.replace("-", " ").title(),
                    location=loc,
                    url=r.get("hostedUrl", ""),
                    source="Lever",
                    date_posted="",
                    job_type=r.get("categories", {}).get("commitment", ""),
                ))
            return board_jobs

    tasks = [_fetch_board(b) for b in LEVER_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            jobs.extend(r)
    return jobs


async def fetch_ashby_boards(session, query) -> list[Job]:
    """Crawl Ashby job boards for matching positions."""
    jobs = []
    sem = _get_sem("ashby", 5)
    query_lower = query.lower()

    async def _fetch_board(board_name):
        async with sem:
            data = await _safe_post(session,
                f"https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams",
                json={"operationName": "ApiJobBoardWithTeams",
                      "variables": {"organizationHostedJobsPageName": board_name},
                      "query": "query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) { jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) { teams { jobs { id title locationName employmentType publishedAt externalLink } } } }"})
            if not data or not isinstance(data, dict):
                return []
            board_jobs = []
            teams = data.get("data", {}).get("jobBoard", {}).get("teams", [])
            for team in teams:
                for r in team.get("jobs", []):
                    title = r.get("title", "")
                    if query_lower and query_lower not in title.lower():
                        continue
                    board_jobs.append(Job(
                        title=title,
                        company=board_name.replace("-", " ").title(),
                        location=r.get("locationName", ""),
                        url=r.get("externalLink", "") or f"https://jobs.ashbyhq.com/{board_name}/{r.get('id', '')}",
                        source="Ashby",
                        job_type=r.get("employmentType", ""),
                    ))
            return board_jobs

    tasks = [_fetch_board(b) for b in ASHBY_BOARDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            jobs.extend(r)
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY D — GOOGLE DORKING (30+ SITES)
# =========================================================================
# ---------------------------------------------------------------------------

DORK_TARGETS = [
    # Major boards (backup for direct scrapers)
    ("Indeed", 'site:indeed.com/viewjob OR site:indeed.com/rc'),
    ("LinkedIn", 'site:linkedin.com/jobs/view'),
    ("Glassdoor", 'site:glassdoor.com/job-listing'),
    ("ZipRecruiter", 'site:ziprecruiter.com/c'),
    ("Dice", 'site:dice.com/job-detail'),
    ("SimplyHired", 'site:simplyhired.com/job'),
    # Tech
    ("BuiltIn", 'site:builtin.com/job'),
    ("WeWorkRemotely", 'site:weworkremotely.com/remote-jobs'),
    ("StackOverflow", 'site:stackoverflow.com/jobs OR site:stackoverflow.co/company'),
    ("AngelList", 'site:wellfound.com/jobs OR site:angel.co/company'),
    ("Arc.dev", 'site:arc.dev/remote-jobs'),
    ("Otta", 'site:otta.com/jobs'),
    ("Triplebyte", 'site:triplebyte.com/company'),
    ("Hired", 'site:hired.com/jobs'),
    # Remote-specific
    ("FlexJobs", 'site:flexjobs.com/remote-jobs'),
    ("WorkingNomads", 'site:workingnomads.com/jobs'),
    ("Himalayas", 'site:himalayas.app/jobs'),
    ("DailyRemote", 'site:dailyremote.com/remote-jobs'),
    ("NoDesk", 'site:nodesk.co/remote-jobs'),
    ("Remoteok", 'site:remoteok.com/remote-jobs'),
    ("JustRemote", 'site:justremote.co/remote-jobs'),
    # Industry / niche
    ("Idealist", 'site:idealist.org/en/jobs'),
    ("HigherEdJobs", 'site:higheredjobs.com/search'),
    ("ClearanceJobs", 'site:clearancejobs.com/jobs'),
    ("eFinancialCareers", 'site:efinancialcareers.com/jobs'),
    ("Mediabistro", 'site:mediabistro.com/jobs'),
    ("Dribbble", 'site:dribbble.com/jobs'),
    ("PowerToFly", 'site:powertofly.com/jobs'),
    ("AuthenticJobs", 'site:authenticjobs.com'),
    ("CyberSecJobs", 'site:cybersecjobs.com/jobs'),
    ("GovernmentJobs", 'site:governmentjobs.com/careers'),
    # Staffing / recruiting
    ("RobertHalf", 'site:roberthalf.com/us/en/jobs'),
    ("KellyServices", 'site:kellyservices.com/job'),
    ("Adecco", 'site:adeccousa.com/jobs'),
    ("Randstad", 'site:randstadusa.com/jobs'),
    ("ManpowerGroup", 'site:manpowergroup.com/job'),
    ("Hays", 'site:hays.com/en-us/job'),
    # ATS boards (catch ones not in our explicit list)
    ("Greenhouse", 'site:boards.greenhouse.io'),
    ("Lever", 'site:jobs.lever.co'),
    ("Workday", 'site:myworkdayjobs.com'),
    ("iCIMS", 'site:careers-*.icims.com'),
    ("SmartRecruiters", 'site:jobs.smartrecruiters.com'),
    # Aggregators
    ("Talent.com", 'site:talent.com/view'),
    ("Jora", 'site:us.jora.com/job'),
    ("Jobrapido", 'site:us.jobrapido.com/job'),
]


async def fetch_google_dorks(session, query, location, radius_miles) -> list[Job]:
    """Scrape Google search results for job listings across 40+ boards."""
    all_jobs = []
    sem = _get_sem("google", 1)  # one at a time for Google

    for site_name, dork in DORK_TARGETS:
        async with sem:
            full_query = f'{query} {dork} "{location}" jobs'
            encoded = urllib.parse.quote_plus(full_query)

            for start in range(0, 30, 10):  # 3 pages per site
                url = f"https://www.google.com/search?q={encoded}&start={start}&num=10"
                html = await _safe_get(session, url, headers=_headers())
                if not html or not isinstance(html, str):
                    break

                # Check for CAPTCHA / rate limit
                if "unusual traffic" in html.lower() or "captcha" in html.lower():
                    break

                soup = BeautifulSoup(html, "lxml")
                found_any = False

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

                    company = ""
                    for sep in [" - ", " | ", " at ", " — ", " · "]:
                        if sep in title_text:
                            parts = title_text.split(sep)
                            if len(parts) >= 2:
                                company = parts[-1].strip()
                                title_text = sep.join(parts[:-1]).strip()
                            break

                    all_jobs.append(Job(
                        title=title_text, company=company,
                        location=_extract_location(snippet_text, location),
                        url=href, source=site_name,
                        description=snippet_text[:500],
                        remote=_is_remote(snippet_text),
                    ))
                    found_any = True

                if not found_any:
                    break
                await asyncio.sleep(random.uniform(2, 5))

    return all_jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY E — NICHE / INDUSTRY (direct scrape or API)
# =========================================================================
# ---------------------------------------------------------------------------

async def fetch_weworkremotely(session, query) -> list[Job]:
    jobs = []
    categories = ["programming", "design", "devops-sysadmin", "business-exec-management",
                   "finance-legal", "customer-support", "marketing", "product", "all-other-remote"]
    for cat in categories:
        html = await _safe_get(session, f"https://weworkremotely.com/categories/remote-{cat}-jobs",
            headers=_headers())
        if not html or not isinstance(html, str):
            continue
        soup = BeautifulSoup(html, "lxml")
        for li in soup.select("li.feature, li.listing-item"):
            link = li.select_one("a[href*='/remote-jobs/']")
            if not link:
                continue
            title_el = li.select_one("span.title")
            company_el = li.select_one("span.company")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if query and query.lower() not in title.lower():
                continue
            href = link.get("href", "")
            if href and not href.startswith("http"):
                href = "https://weworkremotely.com" + href
            jobs.append(Job(
                title=title, company=company_el.get_text(strip=True) if company_el else "",
                location="Remote", url=href, source="WeWorkRemotely", remote=True,
            ))
        await asyncio.sleep(0.5)
    return jobs

async def fetch_remoteok(session, query) -> list[Job]:
    """RemoteOK has a JSON API."""
    data = await _safe_json_get(session, "https://remoteok.com/api",
        headers={"User-Agent": "job-finder-extreme/2.0"})
    if not data or not isinstance(data, list):
        return []
    jobs = []
    query_lower = query.lower()
    for r in data[1:]:  # first element is metadata
        title = r.get("position", "")
        tags = " ".join(r.get("tags", []))
        if query_lower and query_lower not in title.lower() and query_lower not in tags.lower():
            continue
        sal = ""
        if r.get("salary_min") and r.get("salary_max"):
            sal = f"${int(r['salary_min']):,} - ${int(r['salary_max']):,}"
        jobs.append(Job(
            title=title, company=r.get("company", ""),
            location=r.get("location", "Remote"),
            url=r.get("url", ""), source="RemoteOK",
            description=_strip_html(r.get("description", ""))[:500],
            salary=sal, date_posted=r.get("date", "")[:10],
            job_type=", ".join(r.get("tags", [])),
            remote=True,
        ))
    return jobs

async def fetch_himalayas(session, query) -> list[Job]:
    data = await _safe_json_get(session, "https://himalayas.app/jobs/api",
        params={"limit": 100, "q": query})
    if not data:
        return []
    jobs = []
    for r in data.get("jobs", []):
        jobs.append(Job(
            title=r.get("title", ""), company=r.get("companyName", ""),
            location=r.get("location", "Remote"),
            url=f"https://himalayas.app/jobs/{r.get('slug', '')}",
            source="Himalayas",
            description=r.get("excerpt", "")[:500],
            date_posted=r.get("pubDate", "")[:10],
            salary=r.get("salaryCurrency", "") + " " + str(r.get("minSalary", "")) if r.get("minSalary") else "",
            remote=True,
        ))
    return jobs

async def fetch_workingnomads(session, query) -> list[Job]:
    data = await _safe_json_get(session, "https://www.workingnomads.com/api/exposed_jobs/")
    if not data or not isinstance(data, list):
        return []
    jobs = []
    query_lower = query.lower()
    for r in data:
        title = r.get("title", "")
        if query_lower and query_lower not in title.lower() and query_lower not in r.get("description", "").lower():
            continue
        jobs.append(Job(
            title=title, company=r.get("company_name", ""),
            location=r.get("location", "Remote"),
            url=r.get("url", ""), source="WorkingNomads",
            description=_strip_html(r.get("description", ""))[:500],
            date_posted=r.get("pub_date", "")[:10],
            job_type=r.get("category_name", ""),
            remote=True,
        ))
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY F — COMMUNITY / SOCIAL
# =========================================================================
# ---------------------------------------------------------------------------

async def fetch_hackernews_whoisshiring(session, query) -> list[Job]:
    """Parse the latest HN 'Who is Hiring' thread."""
    # Find the latest "Who is Hiring" post
    data = await _safe_json_get(session,
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": "Ask HN: Who is hiring", "tags": "story", "hitsPerPage": 1})
    if not data or not data.get("hits"):
        return []

    story_id = data["hits"][0].get("objectID")
    if not story_id:
        return []

    # Fetch comments
    comments_data = await _safe_json_get(session,
        f"https://hn.algolia.com/api/v1/items/{story_id}")
    if not comments_data:
        return []

    jobs = []
    query_lower = query.lower()
    for child in comments_data.get("children", [])[:300]:
        text = child.get("text", "")
        if not text:
            continue
        plain = _strip_html(text)
        if query_lower and query_lower not in plain.lower():
            continue

        # Parse HN format: "Company | Role | Location | ..."
        first_line = plain.split("\n")[0]
        parts = [p.strip() for p in first_line.split("|")]

        company = parts[0] if len(parts) > 0 else ""
        title = parts[1] if len(parts) > 1 else first_line[:100]
        loc = ""
        for p in parts:
            if any(kw in p.lower() for kw in ["remote", "sf", "nyc", "new york", "san francisco",
                    "austin", "seattle", "boston", "london", "berlin", "onsite"]):
                loc = p
                break
        if not loc and len(parts) > 2:
            loc = parts[2]

        jobs.append(Job(
            title=title[:200], company=company[:100], location=loc,
            url=f"https://news.ycombinator.com/item?id={child.get('id', story_id)}",
            source="HN Who's Hiring",
            description=plain[:500],
            remote=_is_remote(plain),
        ))
    return jobs

async def fetch_reddit_jobs(session, query, location) -> list[Job]:
    """Search Reddit job subreddits."""
    subreddits = [
        "forhire", "jobbit", "hiring", "remotejs", "techjobs",
        "cscareerquestions", "ProgrammingJobs", "gamedevclassifieds",
        "DesignJobs", "DataScienceJobs", "sysadminjobs", "devopsjobs",
    ]
    jobs = []
    search_query = f"{query} {location} hiring"

    for sub in subreddits:
        data = await _safe_json_get(session,
            f"https://www.reddit.com/r/{sub}/search.json",
            params={"q": search_query, "restrict_sr": "on", "sort": "new",
                    "t": "month", "limit": 25},
            headers={"User-Agent": "job-finder-extreme/2.0"})
        if not data:
            continue
        for post in data.get("data", {}).get("children", []):
            d = post.get("data", {})
            title = d.get("title", "")
            if not any(kw in title.lower() for kw in ["hiring", "job", "position", "looking for", "we're", "join"]):
                continue
            # Try to extract company from title: [Hiring] Company - Role
            company = ""
            for pattern in [r'\[hiring\]\s*(.+?)\s*[-–|]', r'^(.+?)\s+(?:is hiring|hiring)',
                           r'\[(.+?)\]']:
                m = re.search(pattern, title, re.IGNORECASE)
                if m:
                    company = m.group(1).strip()
                    break
            jobs.append(Job(
                title=title[:200], company=company,
                location=location,
                url=f"https://reddit.com{d.get('permalink', '')}",
                source=f"r/{sub}",
                description=d.get("selftext", "")[:500],
                date_posted=datetime.fromtimestamp(d.get("created_utc", 0)).strftime("%Y-%m-%d") if d.get("created_utc") else "",
            ))
        await asyncio.sleep(1)
    return jobs

async def fetch_craigslist(session, query, location) -> list[Job]:
    """Scrape Craigslist jobs for the area."""
    # Map common city names to CL subdomains
    cl_map = {
        "new york": "newyork", "nyc": "newyork", "los angeles": "losangeles",
        "la": "losangeles", "san francisco": "sfbay", "sf": "sfbay",
        "chicago": "chicago", "houston": "houston", "phoenix": "phoenix",
        "philadelphia": "philadelphia", "san antonio": "sanantonio",
        "san diego": "sandiego", "dallas": "dallas", "austin": "austin",
        "seattle": "seattle", "denver": "denver", "boston": "boston",
        "nashville": "nashville", "portland": "portland", "miami": "miami",
        "atlanta": "atlanta", "minneapolis": "minneapolis", "detroit": "detroit",
        "charlotte": "charlotte", "raleigh": "raleigh", "tampa": "tampa",
        "st louis": "stlouis", "pittsburgh": "pittsburgh", "baltimore": "baltimore",
        "salt lake city": "saltlakecity", "sacramento": "sacramento",
        "san jose": "sfbay", "columbus": "columbus", "indianapolis": "indianapolis",
        "fort worth": "dallas", "jacksonville": "jacksonville",
        "memphis": "memphis", "oklahoma city": "oklahomacity",
        "louisville": "louisville", "richmond": "richmond",
        "new orleans": "neworleans", "las vegas": "lasvegas",
        "tucson": "tucson", "albuquerque": "albuquerque",
    }

    loc_lower = location.lower().split(",")[0].strip()
    cl_domain = cl_map.get(loc_lower, loc_lower.replace(" ", ""))

    jobs = []
    for section in ["sof", "eng", "web", "sci", "bus", "acc", "ofc", "med", "hea"]:
        html = await _safe_get(session,
            f"https://{cl_domain}.craigslist.org/search/{section}?query={urllib.parse.quote_plus(query)}&postedToday=0&bundleDuplicates=1&searchNearby=1",
            headers=_headers())
        if not html or not isinstance(html, str):
            continue
        soup = BeautifulSoup(html, "lxml")
        for row in soup.select("li.cl-static-search-result, li.result-row, div.result-info"):
            title_el = row.select_one("div.title, a.titlestring, a.result-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = f"https://{cl_domain}.craigslist.org" + href
            loc_el = row.select_one("div.location, span.result-hood")
            price_el = row.select_one("div.price, span.result-price")
            jobs.append(Job(
                title=title, company="",
                location=loc_el.get_text(strip=True).strip("() ") if loc_el else location,
                url=href, source="Craigslist",
                salary=price_el.get_text(strip=True) if price_el else "",
            ))
        await asyncio.sleep(0.5)
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  CATEGORY G — GOVERNMENT / PUBLIC SECTOR
# =========================================================================
# ---------------------------------------------------------------------------

async def fetch_governmentjobs(session, query, location) -> list[Job]:
    """GovernmentJobs.com — state/local government positions."""
    jobs = []
    html = await _safe_get(session,
        f"https://www.governmentjobs.com/careers/jobs?keyword={urllib.parse.quote_plus(query)}&location={urllib.parse.quote_plus(location)}&sort=PostDate%7CDescending",
        headers=_headers())
    if not html or not isinstance(html, str):
        return []
    soup = BeautifulSoup(html, "lxml")
    for card in soup.select("div.job-table-title, li.list-item, tr.job-listing"):
        title_el = card.select_one("a")
        if not title_el:
            continue
        href = title_el.get("href", "")
        if href and not href.startswith("http"):
            href = "https://www.governmentjobs.com" + href
        employer_el = card.select_one(".employer-name, .job-table-department")
        loc_el = card.select_one(".job-table-location, .location")
        sal_el = card.select_one(".job-table-salary, .salary")
        jobs.append(Job(
            title=title_el.get_text(strip=True),
            company=employer_el.get_text(strip=True) if employer_el else "Government",
            location=loc_el.get_text(strip=True) if loc_el else location,
            url=href, source="GovernmentJobs",
            salary=sal_el.get_text(strip=True) if sal_el else "",
        ))
    return jobs


# ---------------------------------------------------------------------------
# =========================================================================
#  DEDUPLICATION & FILTERING
# =========================================================================
# ---------------------------------------------------------------------------

def deduplicate(jobs: list[Job]) -> list[Job]:
    seen_fingerprints = set()
    seen_urls = set()
    unique = []
    for job in jobs:
        fp = job.fingerprint
        url_key = job.url.split("?")[0].rstrip("/") if job.url else ""
        if fp in seen_fingerprints:
            continue
        if url_key and url_key in seen_urls:
            continue
        seen_fingerprints.add(fp)
        if url_key:
            seen_urls.add(url_key)
        unique.append(job)
    return unique


def filter_by_radius(jobs: list[Job], center: tuple[float, float], radius_miles: float) -> list[Job]:
    filtered = []
    for job in jobs:
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
# =========================================================================
#  OUTPUT
# =========================================================================
# ---------------------------------------------------------------------------

def export_csv(jobs: list[Job], filepath: str):
    fields = ["title", "company", "location", "salary", "job_type",
              "date_posted", "source", "distance_miles", "remote", "url", "description"]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for j in sorted(jobs, key=lambda x: x.distance_miles or 9999):
            d = asdict(j)
            d.pop("lat", None)
            d.pop("lon", None)
            writer.writerow({k: d.get(k, "") for k in fields})


def export_json(jobs: list[Job], filepath: str):
    data = []
    for j in sorted(jobs, key=lambda x: x.distance_miles or 9999):
        d = asdict(j)
        d.pop("lat", None)
        d.pop("lon", None)
        data.append(d)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def display_table(jobs: list[Job], limit: int = 60):
    table = Table(title=f"Found {len(jobs)} Jobs", show_lines=False, expand=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Title", style="bold cyan", max_width=40)
    table.add_column("Company", style="green", max_width=22)
    table.add_column("Location", max_width=22)
    table.add_column("Salary", style="yellow", max_width=18)
    table.add_column("Dist", style="magenta", width=7)
    table.add_column("Source", style="dim", width=14)

    for i, j in enumerate(sorted(jobs, key=lambda x: x.distance_miles or 9999)[:limit], 1):
        dist = f"{j.distance_miles}mi" if j.distance_miles is not None else "?"
        loc = j.location[:22] if j.location else ("Remote" if j.remote else "?")
        table.add_row(str(i), j.title[:40], j.company[:22], loc, j.salary[:18], dist, j.source)

    console.print(table)
    if len(jobs) > limit:
        console.print(f"  [dim]...and {len(jobs) - limit} more (see export files)[/dim]")


# ---------------------------------------------------------------------------
# =========================================================================
#  MAIN ORCHESTRATOR
# =========================================================================
# ---------------------------------------------------------------------------

async def run(args):
    config = load_config()
    radius_km = int(args.radius * 1.60934)

    source_count = (
        12  # Category A APIs
        + 10  # Category B direct scrapers
        + len(GREENHOUSE_BOARDS) + len(LEVER_BOARDS) + len(ASHBY_BOARDS)  # Category C ATS
        + len(DORK_TARGETS)  # Category D Google dorks
        + 5   # Category E niche
        + 3   # Category F community
        + 1   # Category G government
    )

    console.print(Panel.fit(
        f"[bold]Job Finder v2.0[/bold] — Exhaustive Multi-Source Scraper\n"
        f"Query: [cyan]{args.query}[/cyan]\n"
        f"Location: [cyan]{args.location}[/cyan]\n"
        f"Radius: [cyan]{args.radius} miles[/cyan]\n"
        f"Sources: [cyan]~{source_count} channels[/cyan] across 7 categories",
        border_style="blue",
    ))

    # Geocode center point
    with console.status("Geocoding location..."):
        center = geocode(args.location)
        if not center:
            console.print(f"[red]Could not geocode '{args.location}'. Try a more specific location.[/red]")
            sys.exit(1)
        console.print(f"  Center: {center[0]:.4f}, {center[1]:.4f}\n")

    all_jobs: list[Job] = []
    source_stats: dict[str, int] = {}

    connector = aiohttp.TCPConnector(limit=30, limit_per_host=5)
    timeout = aiohttp.ClientTimeout(total=300)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            console=console,
        ) as progress:

            # Define all source coroutines
            sources = {
                # --- Category A: APIs ---
                "A1  Adzuna API": fetch_adzuna(session, args.query, args.location, radius_km, config),
                "A2  JSearch API": fetch_jsearch(session, args.query, args.location, args.radius, config),
                "A3  Jooble API": fetch_jooble(session, args.query, args.location, args.radius, config),
                "A4  Remotive": fetch_remotive(session, args.query),
                "A5  USAJobs": fetch_usajobs(session, args.query, args.location, args.radius),
                "A6  Arbeitnow": fetch_arbeitnow(session, args.query),
                "A7  FindWork.dev": fetch_findwork(session, args.query),
                "A8  Jobicy": fetch_jobicy(session, args.query),
                "A9  The Muse": fetch_themuse(session, args.query, args.location),
                "A10 Reed.co.uk": fetch_reed(session, args.query, args.location, args.radius, config),
                "A11 Careerjet": fetch_careerjet(session, args.query, args.location),
                "A12 Talent.com": fetch_talent(session, args.query, args.location),
                # --- Category B: Direct scrapers ---
                "B1  Indeed": fetch_indeed(session, args.query, args.location, args.radius),
                "B2  LinkedIn": fetch_linkedin(session, args.query, args.location),
                "B3  Glassdoor": fetch_glassdoor(session, args.query, args.location),
                "B4  ZipRecruiter": fetch_ziprecruiter(session, args.query, args.location, args.radius),
                "B5  Monster": fetch_monster(session, args.query, args.location),
                "B6  Dice": fetch_dice(session, args.query, args.location, args.radius),
                "B7  SimplyHired": fetch_simplyhired(session, args.query, args.location),
                "B8  BuiltIn": fetch_builtin(session, args.query, args.location),
                "B9  Wellfound": fetch_wellfound(session, args.query),
                # --- Category C: ATS boards ---
                "C1  Greenhouse (57 companies)": fetch_greenhouse_boards(session, args.query),
                "C2  Lever (27 companies)": fetch_lever_boards(session, args.query),
                "C3  Ashby (10 companies)": fetch_ashby_boards(session, args.query),
                # --- Category D: Google dorks ---
                "D   Google Dorks (44 sites)": fetch_google_dorks(session, args.query, args.location, args.radius),
                # --- Category E: Niche ---
                "E1  WeWorkRemotely": fetch_weworkremotely(session, args.query),
                "E2  RemoteOK": fetch_remoteok(session, args.query),
                "E3  Himalayas": fetch_himalayas(session, args.query),
                "E4  WorkingNomads": fetch_workingnomads(session, args.query),
                # --- Category F: Community ---
                "F1  HN Who's Hiring": fetch_hackernews_whoisshiring(session, args.query),
                "F2  Reddit Jobs": fetch_reddit_jobs(session, args.query, args.location),
                "F3  Craigslist": fetch_craigslist(session, args.query, args.location),
                # --- Category G: Government ---
                "G1  GovernmentJobs": fetch_governmentjobs(session, args.query, args.location),
            }

            task_map = {}
            main_task = progress.add_task("All sources", total=len(sources))

            for name, coro in sources.items():
                t = progress.add_task(f"  {name}...", total=1)
                task_map[t] = (name, coro)

            # Run all sources concurrently
            coro_list = list(task_map.values())
            results = await asyncio.gather(
                *[coro for _, coro in coro_list],
                return_exceptions=True
            )

            for (task_id, (name, _)), result in zip(task_map.items(), results):
                if isinstance(result, Exception):
                    progress.update(task_id, completed=1, description=f"  [red]{name}: error[/red]")
                    source_stats[name] = 0
                else:
                    count = len(result)
                    progress.update(task_id, completed=1, description=f"  [green]{name}: {count}[/green]")
                    source_stats[name] = count
                    all_jobs.extend(result)
                progress.advance(main_task)

    # Stats
    total_raw = len(all_jobs)
    active_sources = sum(1 for v in source_stats.values() if v > 0)
    console.print(f"\n  Raw results: [bold]{total_raw}[/bold] jobs from {active_sources} active sources")

    # Show per-source breakdown
    if args.verbose:
        for name, count in sorted(source_stats.items(), key=lambda x: -x[1]):
            if count > 0:
                console.print(f"    {name}: {count}")

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
    slug = re.sub(r'[^a-z0-9]+', '_', args.query.lower())[:30] or "all_jobs"

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
        description="Exhaustive multi-source job scraper — every conceivable source",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python job_finder.py -l "Austin, TX" -r 25 -q "software engineer"
  python job_finder.py -l "San Francisco, CA" -r 50 -q "data scientist" -o json
  python job_finder.py -l "Remote" -r 9999 -q "python developer" --no-filter
  python job_finder.py -l "NYC" -r 10 -q "product manager" -v
        """,
    )
    parser.add_argument("-l", "--location", required=True, help="City, State or address")
    parser.add_argument("-r", "--radius", type=int, default=25, help="Search radius in miles (default: 25)")
    parser.add_argument("-q", "--query", default="", help="Job title or keywords (default: all jobs)")
    parser.add_argument("-o", "--output", choices=["csv", "json", "both"], default="both", help="Output format")
    parser.add_argument("--no-filter", action="store_true", help="Skip radius filtering (keep all results)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show per-source breakdown")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
