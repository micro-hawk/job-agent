import pytest

from agent.markets import classify_market


@pytest.mark.parametrize(
    ("location", "description", "remote", "default", "market"),
    [
        ("Bengaluru, Karnataka, India", "", False, None, "india"),
        ("Remote - India", "", True, None, "india"),
        ("Hybrid - Hyderaba...", "", False, None, "india"),
        ("Bengaluru(Domlur)", "", False, None, "india"),
        ("Bengaluru / London", "", False, None, "india"),
        ("London, UK", "", False, None, "uk"),
        ("Berlin, Germany", "", False, None, "eu"),
        ("Amsterdam", "", False, None, "eu"),
        ("Toronto, ON", "", False, None, "canada"),
        ("Dubai", "", False, None, "uae"),
        ("Singapore", "", False, None, "singapore"),
        ("Remote", "We are fully distributed; work from anywhere in the world.", True, None, "global_remote"),
        ("Remote", "Open to candidates based in India.", True, None, "india"),
        ("Remote", "", False, "india", "india"),
        ("Remote - Worldwide", "", True, None, "global_remote"),
    ],
)
def test_targeted_markets(location, description, remote, default, market):
    assert classify_market(location, description, remote, default).market == market


@pytest.mark.parametrize(
    ("location", "description", "remote", "reason"),
    [
        ("Remote (UK)", "", True, "remote restricted to uk"),
        ("Remote - US", "work from anywhere", True, "US location"),
        ("San Francisco, CA", "", False, "US location"),
        ("Remote", "", True, "remote region unclear"),
        ("Remote", "Join our global company", True, "remote region unclear"),
        ("Sydney, Australia", "", False, "location not targeted: Sydney, Australia"),
        ("Indianapolis, IN", "", False, "US location"),
        ("Indiana", "", False, "location not targeted: Indiana"),
        ("Foster City, CA", "Millions of users worldwide.", True, "US location"),
        ("Brazil", "Work from anywhere.", True, "location not targeted: Brazil"),
        ("Remote", "US only. We have offices in India.", True, "remote restricted to us"),
        ("Remote", "This role is not open to candidates in India.", True, "remote region unclear"),
        ("Remote", "Hiring across Asia-Pacific (excluding India).", True, "remote region unclear"),
        ("Remote", "Remote within US. We serve customers worldwide.", True, "remote restricted to us"),
        ("Remote", "Work from anywhere within US time zones.", True, "remote restricted to us"),
    ],
)
def test_rejected_locations(location, description, remote, reason):
    result = classify_market(location, description, remote)
    assert result.market is None
    assert result.reason == reason
