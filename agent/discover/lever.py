from datetime import datetime, timezone

from agent.models import Job
from agent.text import html_to_text

URL = "https://api.lever.co/v0/postings/{token}?mode=json"


def _salary_text(salary: dict) -> str:
    if not salary.get("max"):
        return ""
    return f"{salary.get('currency', '')} {salary.get('min', salary['max'])} - {salary['max']} {salary.get('interval', '')}".strip()


def parse(payload: list, company: dict) -> list[Job]:
    jobs = []
    for item in payload:
        categories = item.get("categories") or {}
        location = categories.get("location") or ", ".join(categories.get("allLocations") or [])
        parts = [item.get("descriptionPlain") or ""]
        for block in item.get("lists") or []:
            parts.append(block.get("text") or "")
            parts.append(html_to_text(block.get("content") or ""))
        parts.append(item.get("additionalPlain") or "")
        created = item.get("createdAt")
        jobs.append(
            Job(
                source="lever",
                external_id=str(item["id"]),
                company=company["name"],
                company_tier=company.get("tier", "B"),
                title=(item.get("text") or "").strip(),
                location=location,
                remote=item.get("workplaceType") == "remote" or "remote" in location.lower(),
                url=item.get("hostedUrl") or "",
                apply_url=item.get("applyUrl") or item.get("hostedUrl") or "",
                description="\n\n".join(part for part in parts if part).strip(),
                salary_text=_salary_text(item.get("salaryRange") or {}),
                posted_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat() if created else "",
            )
        )
    return jobs
