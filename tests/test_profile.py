from datetime import date

from agent.config import EXAMPLE_DIR
from agent.profile import all_bullets, load_profile, load_resume, notice_answer, profile_brief

SERVING = "Serving notice, last working day 30 Oct 2026"


def test_notice_switches_after_last_working_day():
    profile = load_profile(EXAMPLE_DIR / "profile.yaml")
    assert notice_answer(date(2026, 10, 5), profile) == SERVING
    assert notice_answer(date(2026, 10, 30), profile) == SERVING
    assert notice_answer(date(2026, 10, 31), profile) == "Immediate joiner"


def test_profile_facts():
    profile = load_profile(EXAMPLE_DIR / "profile.yaml")
    assert profile["phone"] == "+91 90000 00000"
    assert profile["current_ctc"]["fixed_lpa"] == 15
    assert profile["work_authorization"]["requires_sponsorship_outside_india"] is True
    assert profile["eeo"] == {"gender": "Male", "ethnicity": "Asian (Indian)", "veteran": "Not a protected veteran", "disability": "No disability"}


def test_resume_bullets_have_unique_ids_and_tags():
    bullets = all_bullets(load_resume(EXAMPLE_DIR / "master_resume.yaml"))
    ids = [bullet["id"] for bullet in bullets]
    assert len(bullets) == 10
    assert len(ids) == len(set(ids))
    assert all(bullet["tags"] and bullet["text"] for bullet in bullets)


def test_projects_are_part_of_the_bullets():
    bullets = {bullet["id"] for bullet in all_bullets(load_resume(EXAMPLE_DIR / "master_resume.yaml"))}
    assert {"pr-ledgerlite", "pr-tripboard"} <= bullets


def test_profile_brief_is_compact_and_grounded():
    brief = profile_brief(load_profile(EXAMPLE_DIR / "profile.yaml"), load_resume(EXAMPLE_DIR / "master_resume.yaml"))
    assert len(brief) < 3000
    for fact in ("Senior Backend Engineer", "4 years", "Kafka", "sponsorship", "MCP"):
        assert fact in brief
