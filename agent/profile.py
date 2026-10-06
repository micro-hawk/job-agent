from datetime import date
from pathlib import Path

from agent.config import CONFIG_DIR, load_yaml


def load_profile(path: Path = CONFIG_DIR / "profile.yaml") -> dict:
    return load_yaml(path)


def load_resume(path: Path = CONFIG_DIR / "master_resume.yaml") -> dict:
    return load_yaml(path)


def notice_answer(today: date, profile: dict) -> str:
    notice = profile["notice"]
    return notice["serving"] if today <= notice["last_working_day"] else notice["after"]


def all_bullets(resume: dict) -> list[dict]:
    sections = resume.get("experience", []) + resume.get("projects", [])
    return [bullet for section in sections for bullet in section["bullets"]]


def profile_brief(profile: dict, resume: dict) -> str:
    skills = "; ".join(f"{group['group']}: {', '.join(group['items'])}" for group in resume["skills"])
    highlights = "\n".join(f"- {bullet['text']}" for bullet in all_bullets(resume) if bullet.get("highlight"))
    return (
        f"{resume['headline']}, {profile['total_experience_years']} years of experience. "
        f"Based in {profile['location']['city']}, {profile['location']['country']}; "
        "authorised to work in India only, needs visa sponsorship elsewhere; open to relocation and remote.\n"
        f"Skills: {skills}\n"
        f"Highlights:\n{highlights}"
    )
