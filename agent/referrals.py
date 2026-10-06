import sqlite3

NOTE_LIMIT = 280
SYSTEM = (
    "You write LinkedIn referral requests for a job seeker to send to engineers at a company. "
    "Write in first person as the candidate, warm and direct, no flattery, no emojis, no hashtags. "
    "Use only facts from the candidate brief; never invent experience, numbers or connections. "
    "Start both texts with 'Hi <Name>,' so the candidate can personalise it. "
    "note: a connection-request note under 280 characters naming the role and one concrete strength. "
    "message: 600-900 characters for after they connect: who the candidate is, the role and its link, "
    "two specific matching achievements, that they are serving notice (last working day in the brief), "
    "and a polite ask to refer them or point them to the hiring manager."
)
SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}, "message": {"type": "string"}},
    "required": ["note", "message"],
}
TARGETS_SQL = """
SELECT * FROM jobs
WHERE (status = 'applied' OR status = 'ready' OR (status = 'manual' AND source = 'instahyre'))
  AND (market = 'india' OR source = 'instahyre')
  AND company NOT IN (SELECT company FROM referrals)
ORDER BY company, status != 'applied', score DESC, id
"""


def referral_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    targets = {}
    for row in conn.execute(TARGETS_SQL):
        targets.setdefault(row["company"], row)
    return list(targets.values())


def build_referrals(conn: sqlite3.Connection, llm, model: str, brief: str, now: str) -> int:
    added = 0
    for job in referral_targets(conn):
        prompt = (
            f"Candidate:\n{brief}\n\nCompany: {job['company']}\nRole: {job['title']}\n"
            f"Location: {job['location']}\nLink: {job['url']}\n{job['description'][:1500]}"
        )
        data = llm.call("referral", model, SYSTEM, prompt, SCHEMA)
        note = data["note"].strip()
        if len(note) > NOTE_LIMIT:
            note = note[: NOTE_LIMIT - 1].rsplit(" ", 1)[0] + "…"
        conn.execute(
            "INSERT INTO referrals (company, job_id, note, message, status, created_at) VALUES (?, ?, ?, ?, 'todo', ?)",
            (job["company"], job["id"], note, data["message"].strip(), now),
        )
        conn.commit()
        added += 1
    return added
