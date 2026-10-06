import pytest

from agent.config import EXAMPLE_DIR, load_settings
from agent.salary import SalaryRange, decide_salary, parse_salary

CFG = load_settings(EXAMPLE_DIR)["salary"]


@pytest.mark.parametrize(
    ("text", "currency", "low", "high", "period"),
    [
        ("₹25L–35L", "INR", 2_500_000, 3_500_000, "year"),
        ("15-25 Lacs P.A.", "INR", 1_500_000, 2_500_000, "year"),
        ("30 LPA", "INR", 3_000_000, 3_000_000, "year"),
        ("INR 25,00,000 - 35,00,000", "INR", 2_500_000, 3_500_000, "year"),
        ("INR 2500000 - 3500000 per-year-salary", "INR", 2_500_000, 3_500_000, "year"),
        ("₹3.5M – ₹5M", "INR", 3_500_000, 5_000_000, "year"),
        ("$257K – $335K • Offers Equity", "USD", 257_000, 335_000, "year"),
        ("$120-150K", "USD", 120_000, 150_000, "year"),
        ("£60,000 - £75,000", "GBP", 60_000, 75_000, "year"),
        ("€60.000 – €80.000", "EUR", 60_000, 80_000, "year"),
        ("CA$110,000 - CA$140,000", "CAD", 110_000, 140_000, "year"),
        ("AED 18,000 - 25,000 per month", "AED", 18_000, 25_000, "month"),
        ("S$8k - S$10k / month", "SGD", 8_000, 10_000, "month"),
        ("USD 120000 - 150000 per-year-salary", "USD", 120_000, 150_000, "year"),
    ],
)
def test_parse_salary_formats(text, currency, low, high, period):
    assert parse_salary(text) == SalaryRange(currency, low, high, period)


@pytest.mark.parametrize("text", ["", "Not Disclosed", "3 - 8 Yrs", "$45 - $60 / hour", "We raised $10B", "Java 8 lambdas"])
def test_parse_salary_rejects_non_salaries(text):
    assert parse_salary(text) is None


def test_require_range_ignores_single_amounts_in_descriptions():
    assert parse_salary("We raised $50M and pay well", require_range=True) is None
    assert parse_salary("Salary: $150K", require_range=True) is None
    assert parse_salary("Pay range $150K - $180K", require_range=True).high == 180_000


def lpa(top, low=None):
    return SalaryRange("INR", (low or top) * 100_000, top * 100_000, "year")


@pytest.mark.parametrize(
    ("rng", "tier", "text"),
    [
        (lpa(35, 20), "B", "₹28 LPA"),
        (lpa(28), "B", "₹28 LPA"),
        (lpa(25, 18), "B", "₹25 LPA"),
        (lpa(27.5), "B", "₹27.5 LPA"),
        (lpa(23), "B", "₹23 LPA"),
        (None, "A", "₹28 LPA"),
        (None, "B", "₹25 LPA"),
        (SalaryRange("USD", 100_000, 150_000, "year"), "B", "₹25 LPA"),
    ],
)
def test_india_rules(rng, tier, text):
    decision = decide_salary("india", rng, tier, CFG)
    assert not decision.skip
    assert decision.text == text


def test_india_skips_when_top_below_23_lpa():
    decision = decide_salary("india", lpa(22, 15), "A", CFG)
    assert decision.skip
    assert "22" in decision.reason


def test_india_monthly_range_is_annualised():
    decision = decide_salary("india", SalaryRange("INR", 150_000, 200_000, "month"), "B", CFG)
    assert decision.text == "₹24 LPA"


@pytest.mark.parametrize(
    ("market", "rng", "text"),
    [
        ("uk", None, "GBP 55,000 per year"),
        ("uk", SalaryRange("GBP", 60_000, 70_000, "year"), "GBP 70,000 per year"),
        ("uk", SalaryRange("EUR", 10_000, 20_000, "year"), "GBP 55,000 per year"),
        ("eu", None, "EUR 60,000 per year"),
        ("canada", None, "CAD 95,000 per year"),
        ("global_remote", SalaryRange("USD", 40_000, 90_000, "year"), "USD 90,000 per year"),
        ("uae", SalaryRange("AED", 18_000, 25_000, "month"), "AED 25,000 per month"),
        ("uae", SalaryRange("AED", 240_000, 300_000, "year"), "AED 25,000 per month"),
        ("singapore", None, "SGD 7,000 per month"),
    ],
)
def test_abroad_rules(market, rng, text):
    decision = decide_salary(market, rng, "B", CFG)
    assert not decision.skip
    assert decision.text == text


@pytest.mark.parametrize(
    ("market", "rng"),
    [
        ("uk", SalaryRange("GBP", 40_000, 50_000, "year")),
        ("global_remote", SalaryRange("USD", 20_000, 30_000, "year")),
        ("singapore", SalaryRange("SGD", 5_000, 6_000, "month")),
        ("us", None),
    ],
)
def test_abroad_skips(market, rng):
    assert decide_salary(market, rng, "A", CFG).skip


@pytest.mark.parametrize(
    ("text", "dollar", "expected"),
    [
        ("Base salary: $70,000 - $85,000 CAD", "USD", SalaryRange("CAD", 70_000, 85_000, "year")),
        ("$136,000 - $187,000", "CAD", SalaryRange("CAD", 136_000, 187_000, "year")),
        ("$5,000 - $6,000 per month", "SGD", SalaryRange("SGD", 5_000, 6_000, "month")),
        ("$120K - $150K", "USD", SalaryRange("USD", 120_000, 150_000, "year")),
    ],
)
def test_bare_dollar_uses_code_or_market_currency(text, dollar, expected):
    assert parse_salary(text, dollar=dollar) == expected


@pytest.mark.parametrize(
    "text",
    [
        "We serve 5-10 lakh merchants across India",
        "Our app has 2-3 lakh daily users",
        "A learning stipend of $1,000 - $2,000 every year",
        "Grew from £1M to £5M ARR last year",
        "We process $20M–$50M in annual payment volume",
    ],
)
def test_description_numbers_without_salary_context_are_ignored(text):
    assert parse_salary(text, require_range=True) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CTC: 25-35 LPA for the right candidate", SalaryRange("INR", 2_500_000, 3_500_000, "year")),
        ("Compensation: £60,000 - £75,000 plus equity", SalaryRange("GBP", 60_000, 75_000, "year")),
        ("Salary $120,000 - $150,000, plus a $500 monthly wellness stipend", SalaryRange("USD", 120_000, 150_000, "year")),
        ("Base pay $90k - $120k / yr. Hourly contractors welcome", SalaryRange("USD", 90_000, 120_000, "year")),
        (
            "The base salary range for this position for candidates located in Canada is between:\n\n$136,000 — $187,000 CAD",
            SalaryRange("CAD", 136_000, 187_000, "year"),
        ),
    ],
)
def test_description_salaries_with_context_parse(text, expected):
    assert parse_salary(text, require_range=True) == expected
