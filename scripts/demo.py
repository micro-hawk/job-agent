import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import DATA_DIR
from agent.db import connect
from dashboard.server import serve

DEMO_DB = DATA_DIR / "demo.db"
PORT = 8778

COMPANIES = ["Northwind", "Contoso", "Fabrikam", "Tailspin", "Wingtip", "Litware", "Proseware", "Adatum", "Woodgrove", "Lucerne", "Fourth Coffee", "Margie's Travel", "Blue Yonder", "Coho Vineyard", "Alpine Ski House", "Trey Research"]
TITLES = ["Senior Software Engineer", "Backend Engineer", "Software Engineer II", "Senior Backend Engineer (Java)", "Product Engineer", "SDE 2", "Java Developer", "Platform Engineer"]
LOCATIONS = [("Bengaluru, India", "india"), ("Remote", "global_remote"), ("Pune, India", "india"), ("London, UK", "uk"), ("Hyderabad, India", "india"), ("Berlin, Germany", "eu")]
SALARIES = {"india": "₹28 LPA", "global_remote": "USD 40,000 per year", "uk": "GBP 55,000 per year", "eu": "EUR 60,000 per year"}
SOURCES = ["greenhouse", "lever", "ashby"]
REASONS = ["Java and Spring Boot match the core stack", "Kafka event-driven work is listed as a must-have", "Seniority matches 4 years of experience", "PostgreSQL tuning experience is relevant"]
GAPS = ["Asks for Go, which is not on the resume", "Prefers payments domain experience"]
NOTE = "Hi <Name>, I'm a backend engineer (Java, Kafka) and just applied for the {title} role at {company}. Would you be open to referring me?"
MESSAGE = "Thanks for connecting, <Name>! I applied for {title} at {company}. I've spent 4 years on Java microservices and Kafka pipelines. If it looks like a fit, a referral would mean a lot."


def seed(path: Path) -> None:
    path.unlink(missing_ok=True)
    conn = connect(path)
    rng = random.Random(7)
    now = datetime.now()
    plan = [("ready", 6), ("manual", 9), ("needs_you", 3), ("applied", 70), ("candidate", 12), ("prescored", 8), ("below_threshold", 25), ("low_prescore", 60), ("filtered_out", 900)]
    job_id = 0
    for status, count in plan:
        for _ in range(count):
            job_id += 1
            company = rng.choice(COMPANIES)
            title = rng.choice(TITLES)
            location, market = rng.choice(LOCATIONS)
            source = "instahyre" if status == "manual" and job_id % 2 else (rng.choice(["linkedin", "naukri"]) if status == "manual" else rng.choice(SOURCES))
            score = rng.randint(72, 94) if status in ("ready", "applied", "needs_you") else None
            detail = json.dumps({"reasons": rng.sample(REASONS, 3), "gaps": rng.sample(GAPS, 1)}) if score else None
            days_ago = 0 if status == "applied" and job_id % 5 == 0 else rng.randint(1, 13)
            seen = (now - timedelta(days=days_ago, minutes=rng.randint(0, 600) if days_ago else 0)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO jobs (source, external_id, company, company_tier, title, location, url, jd_hash, market, expected_salary, status, reason, prescore, score, score_detail, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, str(job_id), company, rng.choice("AB"), title, location, f"https://example.com/jobs/{job_id}", str(job_id), market,
                 SALARIES[market], status,
                 "a required field needs your answer" if status == "needs_you" else "", rng.randint(55, 90), score, detail, seen, seen),
            )
            if status == "applied":
                conn.execute("INSERT INTO applications (job_id, ts, mode, status) VALUES (?, ?, 'live', 'applied')", (job_id, seen))
    for day in range(3, -1, -1):
        start = now - timedelta(days=day, hours=2)
        stats = {"ats": {"fetched": 18000 + day * 211, "new": 40 + day * 13}, "score": {"ready": 3 + day}, "applied": 12 - day, "llm_spend_today_usd": 2.8 - day * 0.4}
        conn.execute("INSERT INTO runs (started_at, finished_at, stats) VALUES (?, ?, ?)",
                     (start.isoformat(timespec="seconds"), (start + timedelta(minutes=4)).isoformat(timespec="seconds"), json.dumps(stats)))
    picked = {}
    for job_id, company, title in conn.execute("SELECT id, company, title FROM jobs WHERE status='applied' AND market='india' ORDER BY id"):
        picked.setdefault(company, (job_id, title))
    for company, (job_id, title) in list(picked.items())[:5]:
        conn.execute("INSERT INTO referrals (company, job_id, note, message, status, created_at) VALUES (?, ?, ?, ?, 'todo', ?)",
                     (company, job_id, NOTE.format(title=title, company=company), MESSAGE.format(title=title, company=company), now.isoformat(timespec="seconds")))
    for _ in range(140):
        conn.execute("INSERT INTO llm_calls (ts, task, model, cost_usd, ok) VALUES (?, 'score', 'sonnet', 0.02, 1)", (now.isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed(DEMO_DB)
    print(f"Demo dashboard with fake data: http://127.0.0.1:{PORT}")
    serve(DEMO_DB, "127.0.0.1", PORT)
