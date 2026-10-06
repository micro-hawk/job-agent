from dataclasses import dataclass

ALERT_SOURCES = ("linkedin", "naukri")


@dataclass
class Job:
    source: str
    external_id: str
    company: str
    title: str
    location: str = ""
    remote: bool = False
    url: str = ""
    apply_url: str = ""
    description: str = ""
    salary_text: str = ""
    posted_at: str = ""
    company_tier: str = "B"
