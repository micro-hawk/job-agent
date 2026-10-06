import sqlite3

import httpx

from agent.db import upsert_job
from agent.discover import ashby, greenhouse, lever
from agent.models import Job

PLATFORMS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
FETCH_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError)


def fetch_company(client: httpx.Client, company: dict) -> list[Job]:
    module = PLATFORMS[company["platform"]]
    response = client.get(module.URL.format(token=company["token"]))
    response.raise_for_status()
    return module.parse(response.json(), company)


def discover_ats(conn: sqlite3.Connection, client: httpx.Client, companies: list[dict], now: str) -> tuple[dict, list[str]]:
    stats = {"fetched": 0, "new": 0}
    errors = []
    for company in companies:
        try:
            jobs = fetch_company(client, company)
        except FETCH_ERRORS as exc:
            errors.append(f"{company['platform']}:{company['token']}: {exc.__class__.__name__}: {str(exc)[:120]}")
            continue
        for job in jobs:
            _, is_new = upsert_job(conn, job, now)
            stats["fetched"] += 1
            stats["new"] += int(is_new)
    return stats, errors
