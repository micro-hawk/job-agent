from agent.models import Job
from agent.text import html_to_text

URL = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _location_name(value) -> str:
    return (value.get("location") or "") if isinstance(value, dict) else (value or "")


def parse(payload: dict, company: dict) -> list[Job]:
    jobs = []
    for item in payload.get("jobs", []):
        if item.get("isListed") is False:
            continue
        names = [_location_name(item.get("location"))] + [_location_name(extra) for extra in item.get("secondaryLocations") or []]
        compensation = item.get("compensation") or {}
        salary_text = (
            compensation.get("scrapeableCompensationSalarySummary")
            or item.get("scrapeableCompensationSalarySummary")
            or compensation.get("compensationTierSummary")
            or ""
        )
        jobs.append(
            Job(
                source="ashby",
                external_id=str(item["id"]),
                company=company["name"],
                company_tier=company.get("tier", "B"),
                title=(item.get("title") or "").strip(),
                location=" / ".join(name for name in names if name),
                remote=bool(item.get("isRemote")) or (item.get("workplaceType") or "").lower() == "remote",
                url=item.get("jobUrl") or "",
                apply_url=item.get("applyUrl") or item.get("jobUrl") or "",
                description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml") or ""),
                salary_text=salary_text,
                posted_at=item.get("publishedAt") or "",
            )
        )
    return jobs
