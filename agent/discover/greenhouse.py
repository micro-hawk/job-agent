from agent.models import Job
from agent.text import html_to_text

URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def parse(payload: dict, company: dict) -> list[Job]:
    jobs = []
    for item in payload.get("jobs", []):
        location = (item.get("location") or {}).get("name") or ""
        jobs.append(
            Job(
                source="greenhouse",
                external_id=str(item["id"]),
                company=company["name"],
                company_tier=company.get("tier", "B"),
                title=(item.get("title") or "").strip(),
                location=location,
                remote="remote" in location.lower(),
                url=item.get("absolute_url") or "",
                apply_url=item.get("absolute_url") or "",
                description=html_to_text(item.get("content") or ""),
                posted_at=item.get("first_published") or item.get("updated_at") or "",
            )
        )
    return jobs
