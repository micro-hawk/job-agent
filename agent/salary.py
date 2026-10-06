import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SalaryRange:
    currency: str
    low: float
    high: float
    period: str = "year"


@dataclass(frozen=True)
class SalaryDecision:
    skip: bool
    currency: str = ""
    amount: float = 0.0
    period: str = "year"
    text: str = ""
    reason: str = ""


_SEP = r"\s*(?:-|–|—|to)\s*"
_LAKH_UNIT = r"(?:lpa|lakhs?|lacs?|l)"
_LAKH = re.compile(
    rf"(?:₹|rs\.?|inr)?\s*(?P<low>\d+(?:\.\d+)?)\s*{_LAKH_UNIT}?"
    rf"(?:{_SEP}(?:₹|rs\.?|inr)?\s*(?P<high>\d+(?:\.\d+)?))?"
    rf"\s*{_LAKH_UNIT}\b",
    re.I,
)
_CUR = r"(?:ca\$|c\$|us\$|s\$|a\$|\$|£|€|₹|aed|sgd|cad|gbp|eur|usd|inr)"
_MONEY = re.compile(
    rf"(?P<cur>{_CUR})\s*(?P<low>\d+(?:[.,]\d+)*)\s*(?P<lowk>[km])?\b"
    rf"(?:{_SEP}(?:{_CUR})?\s*(?P<high>\d+(?:[.,]\d+)*)\s*(?P<highk>[km])?\b)?",
    re.I,
)
_HOURLY = re.compile(r"/\s*h(?:ou)?r\b|per[- ]hour|hourly|an hour", re.I)
_MONTHLY = re.compile(r"/\s*mo(?:nth)?\b|per[- ]month|monthly|a month", re.I)
_YEARLY = re.compile(r"/\s*y(?:ea)?r\b|per[- ]year|per annum|annual(?:ly)?|a year|p\.?a\.?\b", re.I)
_CLAUSE_END = re.compile(r"[,;]\s|\.(?:\s|$)")
_DOLLAR_CODE = re.compile(r"\s*(cad|sgd|usd|aud)\b", re.I)
_SALARY_CONTEXT = re.compile(r"\b(?:salary|salaries|ctc|compensation|base|pay|pay range|per annum|lpa|package|remuneration|wage|ote)\b", re.I)
_CONTEXT_WINDOW = 120
_MAX_YEARLY = 1_000_000
_EURO_THOUSANDS = re.compile(r"\d{1,3}(?:\.\d{3})+")
_CURRENCIES = {
    "$": "USD", "us$": "USD", "usd": "USD", "ca$": "CAD", "c$": "CAD", "cad": "CAD", "£": "GBP", "gbp": "GBP",
    "€": "EUR", "eur": "EUR", "s$": "SGD", "sgd": "SGD", "aed": "AED", "₹": "INR", "inr": "INR", "a$": "AUD",
}
_MULTIPLIERS = {"k": 1_000, "m": 1_000_000}


def _amount(raw: str, suffix: str | None) -> float:
    digits = raw.replace(".", "") if _EURO_THOUSANDS.fullmatch(raw) else raw.replace(",", "")
    return float(digits) * _MULTIPLIERS.get((suffix or "").lower(), 1)


def _has_context(text: str, match: re.Match) -> bool:
    return bool(_SALARY_CONTEXT.search(text[max(0, match.start() - _CONTEXT_WINDOW): match.end()]))


def _clause(text: str, start: int) -> str:
    tail = text[start: start + 30]
    end = _CLAUSE_END.search(tail)
    return tail[: end.start()] if end else tail


def parse_salary(text: str, require_range: bool = False, dollar: str = "USD") -> SalaryRange | None:
    if not text:
        return None
    for match in _LAKH.finditer(text):
        if require_range and not (match["high"] and _has_context(text, match)):
            continue
        low = float(match["low"]) * 100_000
        high = float(match["high"] or match["low"]) * 100_000
        return SalaryRange("INR", low, high, "year")
    for match in _MONEY.finditer(text):
        if require_range and not (match["high"] and _has_context(text, match)):
            continue
        tail = _clause(text, match.end())
        yearly = bool(_YEARLY.search(tail))
        if _HOURLY.search(tail) and not yearly:
            continue
        currency = _CURRENCIES[match["cur"].lower()]
        if match["cur"] == "$":
            code = _DOLLAR_CODE.match(text, match.end())
            currency = code[1].upper() if code else dollar
        low = _amount(match["low"], match["lowk"])
        high = _amount(match["high"], match["highk"]) if match["high"] else low
        if match["high"] and not match["lowk"] and match["highk"] and low < 1_000:
            low = _amount(match["low"], match["highk"])
        period = "month" if _MONTHLY.search(tail) and not yearly else "year"
        if period == "year" and high < (100_000 if currency == "INR" else 1_000):
            continue
        if currency != "INR" and _per(high, period, "year") > _MAX_YEARLY:
            continue
        return SalaryRange(currency, low, high, period)
    return None


def _per(amount: float, period: str, target: str) -> float:
    if period == target:
        return amount
    return amount * 12 if target == "year" else amount / 12


def _india(lpa: float, reason: str) -> SalaryDecision:
    return SalaryDecision(False, "INR", lpa * 100_000, "year", f"₹{lpa:g} LPA", reason)


def decide_salary(market: str, rng: SalaryRange | None, tier: str, cfg: dict) -> SalaryDecision:
    if market == "india":
        india = cfg["india"]
        if rng is None or rng.currency != "INR":
            lpa = india["no_range_tier_a_lpa"] if tier == "A" else india["no_range_lpa"]
            return _india(lpa, "no range posted")
        top = round(_per(rng.high, rng.period, "year") / 100_000, 1)
        if top < india["floor_lpa"]:
            return SalaryDecision(True, reason=f"range tops at ₹{top:g} LPA, below ₹{india['floor_lpa']} LPA")
        return _india(min(top, india["target_lpa"]), f"posted top ₹{top:g} LPA")
    minimum = cfg["minimums"].get(market)
    if minimum is None:
        return SalaryDecision(True, reason=f"no salary rule for market {market}")
    currency, period = minimum["currency"], minimum["period"]
    amount = float(minimum["amount"])
    if rng is not None and rng.currency == currency:
        top = _per(rng.high, rng.period, period)
        if top < amount:
            return SalaryDecision(True, reason=f"range tops at {currency} {top:,.0f} per {period}, below {currency} {amount:,.0f}")
        amount = top
    return SalaryDecision(False, currency, amount, period, f"{currency} {amount:,.0f} per {period}", "")
