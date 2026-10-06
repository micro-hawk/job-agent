import hashlib
import html
import re

_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_WORDS = re.compile(r"[a-z0-9]+")
_COMPANY_NOISE = {"private", "pvt", "limited", "ltd", "inc", "llc", "software", "technologies", "technology", "solutions", "india", "corp", "corporation", "co", "labs"}
_TITLE_ALIASES = {"sr": "senior"}


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = _SCRIPT.sub(" ", text)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def jd_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _words(value: str) -> list[str]:
    return _WORDS.findall((value or "").lower())


def normalize_company(name: str) -> str:
    words = _words(name)
    kept = [word for word in words if word not in _COMPANY_NOISE]
    return " ".join(kept or words)


def normalize_title(title: str) -> str:
    return " ".join(_TITLE_ALIASES.get(word, word) for word in _words(title))


def dedupe_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"
