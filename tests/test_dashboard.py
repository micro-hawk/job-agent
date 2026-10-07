import json
import threading
from datetime import date

import httpx

from agent.db import connect, finish_run, start_run, update_job, upsert_job
from agent.models import Job
from dashboard.server import make_server, render

from tests.fakes import NOW

TODAY = date(2026, 10, 5)


def add(conn, external_id, status, **fields):
    job_id, _ = upsert_job(conn, Job(source=fields.pop("source", "greenhouse"), external_id=external_id, company="Acme", title=fields.pop("title", "Backend Engineer"), location="Bengaluru", url=fields.pop("url", f"https://example.com/{external_id}"), description="d"), NOW)
    update_job(conn, job_id, status=status, **fields)
    return job_id


def test_ready_view_shows_scores_and_escapes_html(conn):
    add(conn, "1", "ready", title="Senior Backend Engineer <script>", score=88, market="india", expected_salary="₹28 LPA",
        score_detail=json.dumps({"score": 88, "reasons": ["Kafka match"], "gaps": ["No Go"]}))
    html = render(conn, "ready", TODAY)
    assert "Kafka match" in html and "No Go" in html and "₹28 LPA" in html and "88" in html
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_manual_view_lists_alert_jobs_and_refuses_unsafe_links(conn):
    add(conn, "li1", "manual", source="linkedin", title="Java Developer", url="https://www.linkedin.com/jobs/view/1/")
    add(conn, "nk1", "manual", source="naukri", title="Backend Developer", url="javascript:alert(1)")
    html = render(conn, "manual", TODAY)
    assert 'href="https://www.linkedin.com/jobs/view/1/"' in html
    assert "Backend Developer" in html
    assert "LinkedIn alert" in html and "Naukri alert" in html
    assert "javascript:" not in html


def test_skipped_view_shows_reasons(conn):
    add(conn, "1", "filtered_out", reason="US location")
    assert "US location" in render(conn, "skipped", TODAY)


def test_today_view_shows_counts_spend_and_run_errors(conn):
    add(conn, "1", "ready", score=80)
    conn.execute("INSERT INTO llm_calls (ts, task, model, cost_usd, ok) VALUES ('2026-10-05T10:00:00', 'score', 'sonnet', 0.25, 1)")
    conn.execute("INSERT INTO llm_calls (ts, task, model, cost_usd, ok) VALUES ('2026-10-04T10:00:00', 'score', 'sonnet', 9, 1)")
    run_id = start_run(conn, NOW)
    finish_run(conn, run_id, NOW, {"ats": {"new": 1}}, ["gmail: GMAIL_APP_PASSWORD not set; alert emails skipped"])
    html = render(conn, "today", TODAY)
    assert "$0.25" in html
    assert "GMAIL_APP_PASSWORD not set" in html


def test_unknown_view_falls_back_to_today(conn):
    assert "Recent runs" in render(conn, "nope", TODAY)


def test_server_rejects_unknown_routes(tmp_path):
    db_path = tmp_path / "agent.db"
    connect(db_path).close()
    server = make_server(db_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert httpx.get(f"{base}/?view=ready").status_code == 200
        assert httpx.get(f"{base}/other").status_code == 404
        assert httpx.post(f"{base}/", data={"x": "1"}).status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_instahyre_view_lists_only_instahyre_jobs_with_mark_button(conn):
    ih = add(conn, "ih1", "manual", source="instahyre", title="SDE II", url="https://www.instahyre.com/job-1-sde-ii/")
    add(conn, "li1", "manual", source="linkedin", title="Java Developer", url="https://www.linkedin.com/jobs/view/1/")
    html = render(conn, "instahyre", TODAY)
    assert "SDE II" in html and "Java Developer" not in html
    assert f'action="/jobs/{ih}/applied"' in html
    assert "SDE II" not in render(conn, "manual", TODAY)


def test_mark_applied_endpoint_updates_manual_job(tmp_path):
    db = tmp_path / "agent.db"
    conn = connect(db)
    job_id = add(conn, "ih1", "manual", source="instahyre", title="SDE II", url="https://www.instahyre.com/job-1/")
    greenhouse = add(conn, "gh1", "ready")
    conn.close()
    server = make_server(db, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        response = httpx.post(f"{base}/jobs/{job_id}/applied")
        assert response.status_code == 303
        assert httpx.post(f"{base}/jobs/{greenhouse}/applied").status_code == 404
    finally:
        server.shutdown()
        server.server_close()
    conn = connect(db)
    assert conn.execute("SELECT status, reason FROM jobs WHERE id=?", (job_id,)).fetchone()[:] == ("applied", "applied manually")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (greenhouse,)).fetchone()[0] == "ready"


def test_referrals_view_and_mark_sent(tmp_path):
    from dashboard.server import mark_referral_sent
    conn = connect(tmp_path / "agent.db")
    job_id = add(conn, "gh1", "applied", title="SDE II")
    conn.execute("INSERT INTO referrals (company, job_id, note, message, status, created_at) VALUES ('Acme', ?, 'Hi <Name>, note', 'Hi <Name>, msg', 'todo', ?)", (job_id, NOW))
    conn.commit()
    ref_id = conn.execute("SELECT id FROM referrals").fetchone()[0]
    html = render(conn, "referrals", TODAY)
    assert "Hi &lt;Name&gt;, note" in html and f'action="/referrals/{ref_id}/sent"' in html
    assert "linkedin.com/search/results/people/?keywords=Acme" in html
    assert mark_referral_sent(conn, ref_id, NOW) and not mark_referral_sent(conn, ref_id, NOW)
    assert conn.execute("SELECT status FROM referrals").fetchone()[0] == "sent"


def test_refresher_runs_once_at_a_time_and_keeps_last_line(tmp_path):
    import sys
    from dashboard.server import Refresher
    refresher = Refresher([sys.executable, "-c", "import time; print('working'); time.sleep(0.5); print('{\"new_in_tab\": 3}')"], tmp_path)
    assert refresher.start() and not refresher.start()
    assert refresher.status()["running"]
    refresher.wait()
    status = refresher.status()
    assert not status["running"] and status["last"] == '{"new_in_tab": 3}' and status["started_at"]


def test_instahyre_view_shows_refresh_button_and_status(conn):
    html = render(conn, "instahyre", TODAY, {"running": True, "started_at": "2026-10-06T10:00:00", "last": ""})
    assert "Working on Instahyre" in html and 'action="/instahyre/refresh"' not in html
    assert 'action="/instahyre/stop"' in html
    html = render(conn, "instahyre", TODAY, {"running": False, "started_at": "2026-10-06T10:00:00", "last": "instahyre: stopped, logged out"})
    assert "instahyre: stopped, logged out" in html and 'action="/instahyre/refresh"' in html
    assert 'action="/instahyre/refresh"' not in render(conn, "ready", TODAY)
    assert 'action="/instahyre/applied"' in html
    assert 'action="/instahyre/apply"' in html


def test_remove_applied_endpoint_runs_applied_command(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    connect(db).close()
    refresher = Refresher([sys.executable, "-c", "print('refresh')"], tmp_path)
    server = make_server(db, "127.0.0.1", 0, refresher, applied_command=[sys.executable, "-c", "print('applied')"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.server_address[1]}/instahyre/applied")
        assert response.status_code == 303 and response.headers["location"] == "/?view=instahyre"
    finally:
        server.shutdown()
        server.server_close()
    refresher.wait()
    assert refresher.status()["last"] == "applied"


def test_apply_endpoint_runs_apply_command(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    connect(db).close()
    refresher = Refresher([sys.executable, "-c", "print('refresh')"], tmp_path)
    server = make_server(db, "127.0.0.1", 0, refresher, apply_command=[sys.executable, "-c", "print('apply')"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.server_address[1]}/instahyre/apply")
        assert response.status_code == 303 and response.headers["location"] == "/?view=instahyre"
    finally:
        server.shutdown()
        server.server_close()
    refresher.wait()
    assert refresher.status()["last"] == "apply"


def test_refresh_endpoint_starts_refresher(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    connect(db).close()
    refresher = Refresher([sys.executable, "-c", "print('done')"], tmp_path)
    server = make_server(db, "127.0.0.1", 0, refresher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.server_address[1]}/instahyre/refresh")
        assert response.status_code == 303 and response.headers["location"] == "/?view=instahyre"
    finally:
        server.shutdown()
        server.server_close()
    refresher.wait()
    assert refresher.status()["last"] == "done"


def test_instahyre_view_hides_applied_jobs(conn):
    add(conn, "ih1", "manual", source="instahyre", title="SDE II")
    add(conn, "ih2", "applied", source="instahyre", title="Product Engineer 2")
    html = render(conn, "instahyre", TODAY)
    assert "SDE II" in html and "Product Engineer 2" not in html


def test_refresh_result_is_described_in_words():
    from dashboard.server import describe_refresh
    assert describe_refresh('{"instahyre_matches": 247, "new_in_tab": 48, "hidden_gone": 2}') == "247 matches on Instahyre · 48 new in this tab · 2 hidden (applied or closed)"
    assert describe_refresh('{"instahyre_matches": 247, "new_in_tab": 0, "hidden_gone": 0, "marked_applied": 4}') == "247 matches on Instahyre · 0 new in this tab · 0 hidden (applied or closed) · 4 marked applied"
    assert describe_refresh('{"applied_on_instahyre": 54, "marked_applied": 4}') == "54 on your Instahyre Applied list · 4 removed from this tab"
    assert describe_refresh('{"instahyre_applied": 7, "already_applied": 2, "left_for_you": 1, "stopped": ""}') == "applied to 7 · 2 were already applied · 1 left for you to check"
    assert describe_refresh('{"instahyre_applied": 3, "already_applied": 0, "left_for_you": 0, "stopped": "logged out"}') == "applied to 3 · 0 were already applied · 0 left for you to check · stopped: logged out"
    assert describe_refresh("instahyre: stopped, logged out") == "instahyre: stopped, logged out"


def test_context_has_nav_counts_and_fourteen_day_chart(conn):
    from dashboard.server import build_context
    ready = add(conn, "1", "ready", score=80)
    add(conn, "2", "manual", source="instahyre", title="SDE II")
    add(conn, "3", "needs_you", reason="unanswered required: Why us?")
    conn.execute("INSERT INTO applications (job_id, ts, mode, status) VALUES (?, ?, 'live', 'applied')", (ready, "2026-10-05T09:00:00"))
    conn.execute("INSERT INTO applications (job_id, ts, mode, status) VALUES (?, ?, 'live', 'applied')", (ready, "2026-10-04T09:00:00"))
    conn.execute("INSERT INTO applications (job_id, ts, mode, status) VALUES (?, ?, 'dry_run', 'applied')", (ready, "2026-10-05T10:00:00"))
    conn.commit()
    ctx = build_context(conn, "today", TODAY)
    assert ctx["nav_counts"]["ready"] == 1 and ctx["nav_counts"]["instahyre"] == 1 and ctx["nav_counts"]["needs"] == 1
    assert len(ctx["daily"]) == 14 and ctx["daily"][-1] == {"day": "2026-10-05", "label": "5", "n": 1} and ctx["daily"][-2]["n"] == 1
    assert ctx["applied_today"] == 1


def test_needs_and_applied_views_list_their_jobs(conn):
    add(conn, "1", "needs_you", reason="unanswered required: Why us?")
    add(conn, "2", "applied", title="Platform Engineer", reason="applied on Instahyre by the agent")
    assert "Why us?" in render(conn, "needs", TODAY)
    assert "Platform Engineer" in render(conn, "applied", TODAY)


def test_run_stats_read_as_a_sentence():
    from dashboard.server import summarize_run
    stats = {"ats": {"fetched": 18885, "new": 180}, "alerts": {"new": 2}, "score": {"ready": 2}, "llm_spend_today_usd": 5.6794}
    assert summarize_run(stats) == "18,885 fetched · 180 new · 2 from alerts · 2 ready · $5.68 spent today"
    assert summarize_run({"applied": 3, "needs_you": 1}) == "3 applied · 1 needs you"
    assert summarize_run({}) == "no changes"


def test_static_files_are_served_and_paths_cannot_escape(tmp_path):
    db = tmp_path / "agent.db"
    connect(db).close()
    server = make_server(db, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        css = httpx.get(f"{base}/static/app.css")
        assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
        assert httpx.get(f"{base}/static/app.js").headers["content-type"].startswith("text/javascript")
        assert httpx.get(f"{base}/static/..%2Fserver.py").status_code == 404
        assert httpx.get(f"{base}/static/missing.css").status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_company_hue_is_stable_and_in_range():
    from dashboard.server import company_hue
    assert company_hue("Stripe") == company_hue("Stripe") and 0 <= company_hue("Stripe") < 360
    assert company_hue("Stripe") != company_hue("Plazza")


def test_market_labels_read_naturally():
    from dashboard.server import market_label
    assert [market_label(m) for m in ("uk", "eu", "uae", "global_remote", "india", "mars")] == ["UK", "EU", "UAE", "Global remote", "India", "Mars"]


def test_refresher_stop_kills_the_run_and_its_children(tmp_path):
    import os
    import sys
    import time
    from dashboard.server import Refresher
    child_pid = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    refresher = Refresher([sys.executable, "-c", script], tmp_path)
    assert not refresher.stop()
    refresher.start()
    for _ in range(100):
        if child_pid.exists() and child_pid.read_text():
            break
        time.sleep(0.05)
    started = time.monotonic()
    assert refresher.stop()
    refresher.wait()
    assert time.monotonic() - started < 5
    status = refresher.status()
    assert not status["running"] and status["last"] == "stopped from the dashboard"
    time.sleep(0.2)
    pid = int(child_pid.read_text())
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_stop_endpoint_stops_the_refresher(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    connect(db).close()
    refresher = Refresher([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path)
    server = make_server(db, "127.0.0.1", 0, refresher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        refresher.start()
        response = httpx.post(f"http://127.0.0.1:{server.server_address[1]}/instahyre/stop")
        assert response.status_code == 303 and response.headers["location"] == "/?view=instahyre"
    finally:
        server.shutdown()
        server.server_close()
    refresher.wait()
    assert refresher.status()["last"] == "stopped from the dashboard"


def test_manual_refresh_endpoint_runs_its_own_refresher(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    connect(db).close()
    instahyre = Refresher([sys.executable, "-c", "print('instahyre')"], tmp_path)
    manual = Refresher([sys.executable, "-c", "print('manual')"], tmp_path)
    server = make_server(db, "127.0.0.1", 0, instahyre, manual_refresher=manual)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(f"http://127.0.0.1:{server.server_address[1]}/manual/refresh")
        assert response.status_code == 303 and response.headers["location"] == "/?view=manual"
    finally:
        server.shutdown()
        server.server_close()
    manual.wait()
    assert manual.status()["last"] == "manual" and instahyre.status()["last"] == ""


def test_manual_view_shows_refresh_button_and_last_result(conn):
    html = render(conn, "manual", TODAY, manual_refresh={"running": False, "started_at": "2026-10-05T10:05:00", "last": '{"linkedin_applied": 2, "naukri_applied": 1, "expired": 4}'})
    assert 'action="/manual/refresh"' in html
    assert "3 applied (2 LinkedIn, 1 Naukri) · 4 expired" in html
    assert 'action="/manual/refresh"' not in render(conn, "instahyre", TODAY)


def test_manual_view_reloads_while_refreshing(conn):
    html = render(conn, "manual", TODAY, manual_refresh={"running": True, "started_at": "2026-10-05T10:05:00", "last": ""})
    assert 'http-equiv="refresh"' in html and 'action="/manual/refresh"' not in html


QUESTIONS = [
    {"id": 1, "text": "Which LLMs have you used? <b>", "type": 0, "required": True, "position": 0},
    {"id": 2, "text": "Biggest AI project?", "type": 0, "required": True, "position": 1},
]


def add_questionnaire(conn, status="draft", answers=None, title="Acme - SDE 2"):
    cursor = conn.execute(
        "INSERT INTO questionnaires (questionnaire_id, opportunity_id, job_title, url, questions, answers, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, "1", title, "https://www.instahyre.com/questionnaire/1/100", json.dumps(QUESTIONS), json.dumps(answers or {}), status, NOW),
    )
    conn.commit()
    return cursor.lastrowid


def test_questionnaires_view_shows_drafts_for_review(conn):
    add_questionnaire(conn, answers={"1": "GPT-4 in a support bot", "2": ""})
    add_questionnaire(conn, status="manual", title="Beta - Java")
    add_questionnaire(conn, status="submitted", title="Gamma - SDE")
    html = render(conn, "questionnaires", TODAY)
    assert "Acme - SDE 2" in html and "GPT-4 in a support bot" in html
    assert "Which LLMs have you used? &lt;b&gt;" in html
    assert 'name="a_2"' in html and "Needs your answer" in html
    assert "Beta - Java" in html and 'href="https://www.instahyre.com/questionnaire/1/100"' in html
    assert "Gamma - SDE" in html
    assert 'action="/questionnaires/fetch"' in html


def test_questionnaires_nav_counts_drafts_and_manual(conn):
    add_questionnaire(conn)
    add_questionnaire(conn, status="manual", title="Beta")
    add_questionnaire(conn, status="sent", title="Gamma")
    assert render(conn, "today", TODAY) and __import__("dashboard.server", fromlist=["build_context"]).build_context(conn, "today", TODAY)["nav_counts"]["questionnaires"] == 2


def serve_with(db, refresher):
    import sys
    server = make_server(db, "127.0.0.1", 0, refresher, questionnaires_command=[sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))"])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_questionnaire_submit_saves_answers_and_starts_the_submit_run(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    conn = connect(db)
    row_id = add_questionnaire(conn)
    refresher = Refresher([sys.executable, "-c", "print('refresh')"], tmp_path)
    server, base = serve_with(db, refresher)
    try:
        response = httpx.post(f"{base}/questionnaires/{row_id}/submit", data={"a_1": "GPT-4", "a_2": "A RAG bot", "action": "submit"})
        assert response.status_code == 303 and response.headers["location"] == "/?view=questionnaires"
    finally:
        server.shutdown()
        server.server_close()
    refresher.wait()
    assert json.loads(conn.execute("SELECT answers FROM questionnaires").fetchone()[0]) == {"1": "GPT-4", "2": "A RAG bot"}
    assert refresher.status()["last"] == f"--submit {row_id}"


def test_questionnaire_save_or_blank_submit_does_not_start_a_run(tmp_path):
    import sys
    from dashboard.server import Refresher
    db = tmp_path / "agent.db"
    conn = connect(db)
    row_id = add_questionnaire(conn)
    refresher = Refresher([sys.executable, "-c", "print('refresh')"], tmp_path)
    server, base = serve_with(db, refresher)
    try:
        httpx.post(f"{base}/questionnaires/{row_id}/submit", data={"a_1": "GPT-4", "a_2": "", "action": "save"})
        httpx.post(f"{base}/questionnaires/{row_id}/submit", data={"a_1": "GPT-4", "a_2": " ", "action": "submit"})
        assert httpx.post(f"{base}/questionnaires/999/submit", data={"action": "save"}).status_code == 404
    finally:
        server.shutdown()
        server.server_close()
    row = conn.execute("SELECT answers, reason, status FROM questionnaires").fetchone()
    assert json.loads(row["answers"]) == {"1": "GPT-4", "2": ""}
    assert row["reason"] == "answer every required question first" and row["status"] == "draft"
    assert refresher.status() == {"running": False, "started_at": "", "last": ""}


def test_questionnaire_results_are_described_in_words():
    from dashboard.server import describe_refresh
    assert describe_refresh('{"questionnaires_new": 3, "to_review": 1, "already_sent": 1, "open_yourself": 1, "drafted": 1}') == "3 new questionnaires · 1 drafted for your review · 1 already sent · 1 to open yourself"
    assert describe_refresh('{"questionnaire_submit": "submitted", "job_title": "Acme - SDE 2"}') == "Acme - SDE 2: questionnaire submitted"
    assert describe_refresh('{"questionnaire_submit": "unconfirmed", "job_title": "Acme"}') == "Acme: clicked Submit but Instahyre did not confirm — open it to check"


def test_blank_answers_offer_a_one_click_suggestion(conn):
    row_id = add_questionnaire(conn, answers={"1": "GPT-4", "2": ""})
    conn.execute("UPDATE questionnaires SET suggestions=? WHERE id=?", (json.dumps({"2": "I have not led an AI project; my closest is <MCP>."}), row_id))
    conn.commit()
    html = render(conn, "questionnaires", TODAY)
    assert 'placeholder="I have not led an AI project; my closest is &lt;MCP&gt;."' in html
    assert html.count("data-suggest=") == 1 and f'data-suggest="q{row_id}_2"' in html


def test_questionnaires_live_under_the_instahyre_tab(conn):
    add_questionnaire(conn)
    for view in ("instahyre", "questionnaires"):
        html = render(conn, view, TODAY)
        assert 'href="/?view=instahyre" class="subtab' in html and 'href="/?view=questionnaires" class="subtab' in html
        assert 'href="/?view=instahyre" class="nav-item on"' in html
        assert 'href="/?view=questionnaires" class="nav-item' not in html
    assert 'class="subtab on">Questionnaires' in render(conn, "questionnaires", TODAY)


def ats_result(score=72, fixes=("Quantify <impact>",)):
    return {"score": score, "parse_score": 83, "content_score": 70, "keyword_score": 60, "ratings": {"impact": 80, "seniority": 60, "clarity": 70},
            "checks": [{"name": "contact", "ok": False, "detail": "add an email and phone number as plain text"}],
            "matched_keywords": ["Kafka"], "missing_keywords": ["Golang"], "fixes": list(fixes), "with_jd": True}


def test_ats_view_shows_the_form_and_latest_result(conn):
    from agent.ats import record_check
    record_check(conn, "old.pdf", "junior", ats_result(40), NOW)
    record_check(conn, "cv.pdf", "mid", ats_result(), NOW)
    html = render(conn, "ats", TODAY)
    assert 'enctype="multipart/form-data"' in html and 'action="/ats/check"' in html
    assert html.count('<option value="') == 5 and "Senior (5–8 yrs)" in html
    assert "72" in html and "Golang" in html and "Quantify &lt;impact&gt;" in html and "add an email and phone number" in html
    assert "old.pdf" in html and 'href="/?view=ats" class="nav-item on"' in html


def test_ats_view_shows_an_error_once(conn):
    assert "upload a PDF or DOCX resume" in render(conn, "ats", TODAY, ats_error="upload a PDF or DOCX resume")


def serve_ats(db, llm):
    server = make_server(db, "127.0.0.1", 0, llm_factory=lambda conn: llm, ats_model="sonnet")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_ats_upload_scores_and_stores_the_result(tmp_path):
    from tests.fakes import StubLLM
    from tests.test_ats import RESUME, make_docx
    db = tmp_path / "agent.db"
    conn = connect(db)
    llm = StubLLM({"ats": lambda prompt: {"impact": 80, "seniority": 70, "clarity": 75, "keywords": ["Kafka", "Golang"], "fixes": ["Lead with impact"]}})
    server, base = serve_ats(db, llm)
    try:
        response = httpx.post(f"{base}/ats/check", data={"level": "senior", "jd": "Kafka and Golang"}, files={"resume": ("cv.docx", make_docx(RESUME.splitlines()))})
        assert response.status_code == 303 and response.headers["location"] == "/?view=ats"
        page = httpx.get(f"{base}/?view=ats").text
    finally:
        server.shutdown()
        server.server_close()
    row = conn.execute("SELECT filename, level, result FROM ats_checks").fetchone()
    assert (row["filename"], row["level"]) == ("cv.docx", "senior") and json.loads(row["result"])["missing_keywords"] == ["Golang"]
    assert "Kafka and Golang" in llm.calls[0][2] and "Lead with impact" in page


def test_ats_upload_rejects_bad_input_without_storing(tmp_path):
    from agent.ats import MAX_UPLOAD_BYTES
    from agent.llm import LLMError
    from tests.fakes import StubLLM
    from tests.test_ats import RESUME, make_docx
    db = tmp_path / "agent.db"
    conn = connect(db)
    llm = StubLLM({"ats": lambda prompt: LLMError("daily LLM budget $2.00 reached")})
    server, base = serve_ats(db, llm)
    docx = make_docx(RESUME.splitlines())
    cases = [
        ({"level": "mid"}, {"resume": ("cv.txt", b"hello")}, "upload a PDF or DOCX resume"),
        ({"level": "guru"}, {"resume": ("cv.docx", docx)}, "choose an experience level"),
        ({"level": "mid"}, {"resume": ("cv.docx", b"x" * (MAX_UPLOAD_BYTES + 1))}, "larger than 5 MB"),
        ({"level": "mid"}, {"resume": ("cv.docx", docx)}, "budget"),
    ]
    try:
        for data, files, message in cases:
            assert httpx.post(f"{base}/ats/check", data=data, files=files).headers["location"] == "/?view=ats"
            page = httpx.get(f"{base}/?view=ats").text
            assert message in page, message
            assert message not in httpx.get(f"{base}/?view=ats").text
    finally:
        server.shutdown()
        server.server_close()
    assert conn.execute("SELECT COUNT(*) FROM ats_checks").fetchone()[0] == 0
