from agent.text import dedupe_key, html_to_text, jd_hash, normalize_company, normalize_title


def test_html_to_text_unescapes_greenhouse_content():
    text = html_to_text("&lt;p&gt;Build &lt;strong&gt;Java&lt;/strong&gt; services with Kafka &amp;amp; PostgreSQL.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Redis&lt;/li&gt;&lt;/ul&gt;")
    assert "Build Java services with Kafka & PostgreSQL." in text
    assert "Redis" in text
    assert "<" not in text


def test_html_to_text_drops_scripts_and_handles_empty():
    assert html_to_text("<script>alert(1)</script><p>Hi</p>") == "Hi"
    assert html_to_text("") == ""


def test_jd_hash_is_stable_and_short():
    assert jd_hash("abc") == jd_hash("abc")
    assert jd_hash("abc") != jd_hash("abd")
    assert len(jd_hash("abc")) == 16


def test_normalize_company_strips_legal_noise():
    assert normalize_company("Razorpay Software Private Limited") == "razorpay"
    assert normalize_company("Grafana Labs") == "grafana"
    assert normalize_company("Labs") == "labs"


def test_dedupe_key_treats_sr_as_senior():
    assert normalize_title("Sr. Backend Engineer") == "senior backend engineer"
    assert dedupe_key("Grafana Labs", "Senior Backend Engineer") == dedupe_key("grafana", "Sr. Backend Engineer")
