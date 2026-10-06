import re
from dataclasses import dataclass

_INDIA = re.compile(
    r"\b(?:india(?!na)|bengal|bangal|hyderab|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|kolkata|ahmedabad|karnataka|"
    r"maharashtra|telangana|tamil nadu|haryana|kochi|jaipur|indore|chandigarh|coimbatore|trivandrum|thiruvananthapuram|"
    r"mysore|mysuru|nagpur|bhubaneswar|vadodara)",
    re.I,
)
_ABROAD = {
    "uk": re.compile(r"\b(?:united kingdom|uk|england|scotland|london|manchester|edinburgh|cambridge|bristol|belfast)\b", re.I),
    "eu": re.compile(
        r"\b(?:germany|berlin|munich|hamburg|frankfurt|netherlands|amsterdam|rotterdam|france|paris|spain|madrid|barcelona|"
        r"ireland|dublin|portugal|lisbon|porto|poland|warsaw|krakow|sweden|stockholm|denmark|copenhagen|belgium|brussels|"
        r"austria|vienna|finland|helsinki|italy|milan|czech|prague|estonia|tallinn|luxembourg)\b",
        re.I,
    ),
    "canada": re.compile(r"\b(?:canada|toronto|vancouver|montreal|ottawa|waterloo|calgary)\b", re.I),
    "uae": re.compile(r"\b(?:uae|united arab emirates|dubai|abu dhabi)\b", re.I),
    "singapore": re.compile(r"\bsingapore\b", re.I),
}
_US = re.compile(
    r"\b(?:united states|usa|us|u\.s\.?|new york|nyc|san francisco|bay area|seattle|austin|boston|chicago|los angeles|"
    r"denver|atlanta|miami|palo alto|mountain view|menlo park|sunnyvale|san jose|california|texas|washington)\b",
    re.I,
)
_US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|"
    r"PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
_US_ONLY = re.compile(
    r"\b(?:(?:us|u\.s\.|usa|united states)[- ](?:only|based)|within (?:the )?(?:us|u\.s\.|usa|united states)\b|"
    r"(?:us|u\.s\.|united states) time ?zones?|authori[sz]ed to work in the (?:us|u\.s\.|united states))",
    re.I,
)
_REMOTE = re.compile(r"\b(?:remote|anywhere|distributed|work from home|wfh)\b", re.I)
_INDIA_HIRING = re.compile(
    r"\b(?:india[- ]based|(?:hiring|candidates?|based|located|residing|living|remote)\b[^.]{0,40}\bindia)\b", re.I
)
_INDIA_EXCLUDED = re.compile(r"\b(?:exclud\w*|except|outside(?: of)?|not (?:open|available|eligible)[^.]{0,40}?)\s+(?:\w+\s+){0,2}?india\b", re.I)
_GLOBAL = re.compile(
    r"\b(?:work from anywhere|anywhere in the world|fully distributed|globally distributed|any time ?zone|"
    r"(?:remote|hire|hiring|candidates?|based)\W+(?:\w+\W+){0,3}?(?:worldwide|globally))\b",
    re.I,
)
_GLOBAL_LOCATION = re.compile(r"\b(?:worldwide|anywhere|global)\b", re.I)
_LOCATION_NOISE = re.compile(r"\b(?:remote|anywhere|distributed|work from home|wfh|hybrid|on-?site|full[- ]time)\b|[^a-z]", re.I)


@dataclass(frozen=True)
class MarketResult:
    market: str | None
    reason: str = ""


def classify_market(location: str, description: str = "", remote: bool = False, default_country: str | None = None) -> MarketResult:
    location = location or ""
    description = description or ""
    is_remote = remote or bool(_REMOTE.search(location))
    if _INDIA.search(location):
        return MarketResult("india")
    for market, pattern in _ABROAD.items():
        if pattern.search(location):
            return MarketResult(None, f"remote restricted to {market}") if is_remote else MarketResult(market)
    if _US.search(location) or _US_STATE.search(location):
        return MarketResult(None, "US location")
    if is_remote and _GLOBAL_LOCATION.search(location):
        return MarketResult("global_remote")
    if _LOCATION_NOISE.sub("", location):
        return MarketResult(None, f"location not targeted: {location[:60]}")
    if is_remote or not location.strip():
        if default_country == "india":
            return MarketResult("india")
        if _US_ONLY.search(description):
            return MarketResult(None, "remote restricted to us")
        if _INDIA_HIRING.search(description) and not _INDIA_EXCLUDED.search(description):
            return MarketResult("india")
        if is_remote and _GLOBAL.search(description):
            return MarketResult("global_remote")
        return MarketResult(None, "remote region unclear" if is_remote else "no location")
    return MarketResult(None, f"location not targeted: {location[:60]}")
