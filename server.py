#!/usr/bin/env python3
"""
FastAPI server wrapping job_finder scraper.
Streams results via SSE so the UI updates live as each source completes.

Run:  python server.py
Then: open http://localhost:8000
"""

import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import aiohttp
import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from job_finder import (
    Job, load_config, geocode, deduplicate, filter_by_radius,
    _get_sem, _headers,
    # Category A
    fetch_adzuna, fetch_jsearch, fetch_jooble, fetch_remotive,
    fetch_usajobs, fetch_arbeitnow, fetch_findwork, fetch_jobicy,
    fetch_themuse, fetch_reed, fetch_careerjet, fetch_talent,
    # Category B
    fetch_indeed, fetch_linkedin, fetch_glassdoor, fetch_ziprecruiter,
    fetch_monster, fetch_dice, fetch_simplyhired, fetch_builtin, fetch_wellfound,
    # Category C
    fetch_greenhouse_boards, fetch_lever_boards, fetch_ashby_boards,
    # Category D
    fetch_google_dorks,
    # Category E
    fetch_weworkremotely, fetch_remoteok, fetch_himalayas, fetch_workingnomads,
    # Category F
    fetch_hackernews_whoisshiring, fetch_reddit_jobs, fetch_craigslist,
    # Category G
    fetch_governmentjobs,
)

app = FastAPI(title="Job Finder")

# Serve the static UI
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/search")
async def search(
    q: str = Query("", description="Job title or keywords"),
    location: str = Query(..., description="City, State"),
    radius: int = Query(25, description="Miles"),
    no_filter: bool = Query(False),
):
    """Stream job results as server-sent events."""

    async def event_stream():
        config = load_config()
        radius_km = int(radius * 1.60934)

        # Geocode
        center = geocode(location)
        if not center:
            yield _sse("error", {"message": f"Could not geocode '{location}'"})
            return

        yield _sse("status", {"message": f"Center: {center[0]:.4f}, {center[1]:.4f}", "phase": "geocoded"})

        connector = aiohttp.TCPConnector(limit=30, limit_per_host=5)
        timeout = aiohttp.ClientTimeout(total=300)

        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            sources = {
                "Adzuna": fetch_adzuna(session, q, location, radius_km, config),
                "JSearch": fetch_jsearch(session, q, location, radius, config),
                "Jooble": fetch_jooble(session, q, location, radius, config),
                "Remotive": fetch_remotive(session, q),
                "USAJobs": fetch_usajobs(session, q, location, radius),
                "Arbeitnow": fetch_arbeitnow(session, q),
                "FindWork.dev": fetch_findwork(session, q),
                "Jobicy": fetch_jobicy(session, q),
                "The Muse": fetch_themuse(session, q, location),
                "Reed": fetch_reed(session, q, location, radius, config),
                "Careerjet": fetch_careerjet(session, q, location),
                "Talent.com": fetch_talent(session, q, location),
                "Indeed": fetch_indeed(session, q, location, radius),
                "LinkedIn": fetch_linkedin(session, q, location),
                "Glassdoor": fetch_glassdoor(session, q, location),
                "ZipRecruiter": fetch_ziprecruiter(session, q, location, radius),
                "Monster": fetch_monster(session, q, location),
                "Dice": fetch_dice(session, q, location, radius),
                "SimplyHired": fetch_simplyhired(session, q, location),
                "BuiltIn": fetch_builtin(session, q, location),
                "Wellfound": fetch_wellfound(session, q),
                "Greenhouse": fetch_greenhouse_boards(session, q),
                "Lever": fetch_lever_boards(session, q),
                "Ashby": fetch_ashby_boards(session, q),
                "Google Dorks": fetch_google_dorks(session, q, location, radius),
                "WeWorkRemotely": fetch_weworkremotely(session, q),
                "RemoteOK": fetch_remoteok(session, q),
                "Himalayas": fetch_himalayas(session, q),
                "WorkingNomads": fetch_workingnomads(session, q),
                "HN Who's Hiring": fetch_hackernews_whoisshiring(session, q),
                "Reddit": fetch_reddit_jobs(session, q, location),
                "Craigslist": fetch_craigslist(session, q, location),
                "GovernmentJobs": fetch_governmentjobs(session, q, location),
            }

            total = len(sources)
            yield _sse("sources", {"total": total, "names": list(sources.keys())})

            all_jobs: list[Job] = []
            completed = 0

            # Wrap each source to yield as it completes
            async def run_source(name, coro):
                try:
                    result = await coro
                    return name, result
                except Exception as e:
                    return name, []

            # Use asyncio.as_completed pattern via tasks
            tasks = {asyncio.create_task(run_source(n, c)): n for n, c in sources.items()}

            for future in asyncio.as_completed(tasks):
                name, result = await future
                completed += 1
                count = len(result)
                all_jobs.extend(result)

                yield _sse("source_done", {
                    "name": name,
                    "count": count,
                    "completed": completed,
                    "total": total,
                    "total_jobs": len(all_jobs),
                })

        # Dedup
        yield _sse("status", {"message": "Deduplicating...", "phase": "dedup"})
        all_jobs = deduplicate(all_jobs)
        yield _sse("status", {"message": f"{len(all_jobs)} unique jobs after dedup", "phase": "dedup_done"})

        # Filter
        if not no_filter:
            yield _sse("status", {"message": "Filtering by radius...", "phase": "filtering"})
            all_jobs = filter_by_radius(all_jobs, center, radius)
            yield _sse("status", {"message": f"{len(all_jobs)} jobs within {radius} mi", "phase": "filter_done"})

        # Sort by distance
        all_jobs.sort(key=lambda x: x.distance_miles or 9999)

        # Send final results
        jobs_data = []
        for j in all_jobs:
            d = asdict(j)
            d.pop("lat", None)
            d.pop("lon", None)
            jobs_data.append(d)

        # Send in chunks to avoid huge SSE payloads
        chunk_size = 50
        for i in range(0, len(jobs_data), chunk_size):
            chunk = jobs_data[i:i + chunk_size]
            yield _sse("jobs", {"jobs": chunk, "offset": i, "total": len(jobs_data)})

        yield _sse("done", {"total": len(jobs_data)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


if __name__ == "__main__":
    os.makedirs(STATIC_DIR, exist_ok=True)
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
