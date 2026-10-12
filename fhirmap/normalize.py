"""Turn printed fax strings into FHIR-typed values.

Each normalizer returns None when it cannot parse the input; the mapper treats
that as a field needing review rather than guessing.
"""
import re
from datetime import date

_DATE_PATTERNS = [
    re.compile(r"^(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})$"),
    re.compile(r"^(?P<m>\d{1,2})[/\-.](?P<d>\d{1,2})[/\-.](?P<y>\d{4})$"),
    re.compile(r"^(?P<m>\d{1,2})[/\-.](?P<d>\d{1,2})[/\-.](?P<y>\d{2})$"),
]


def to_date(s: str) -> str | None:
    s = (s or "").strip().replace(" ", "")
    for pat in _DATE_PATTERNS:
        m = pat.match(s)
        if not m:
            continue
        y, mo, d = int(m["y"]), int(m["m"]), int(m["d"])
        if y < 100:  # two-digit year: assume not in the future
            y += 2000 if y <= date.today().year % 100 else 1900
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def to_gender(s: str) -> str | None:
    s = (s or "").strip().lower()
    return {"m": "male", "male": "male", "f": "female", "female": "female",
            "u": "unknown", "unknown": "unknown", "o": "other", "other": "other"}.get(s)


def to_phone(s: str) -> str | None:
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}" if len(digits) == 10 else None


_ADDR = re.compile(r"^(?P<line>.+?),\s*(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)?$")


def to_address(s: str) -> dict | None:
    m = _ADDR.match((s or "").strip())
    if not m:
        return {"text": s} if s else None
    out = {"line": [m["line"]], "city": m["city"], "state": m["state"], "country": "US", "text": s}
    if m["zip"]:
        out["postalCode"] = m["zip"]
    return out


def to_number(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group()) if m else None


def to_int(s: str) -> int | None:
    n = to_number(s)
    return int(n) if n is not None and n == int(n) else None


def to_priority(s: str) -> str | None:
    s = (s or "").strip().lower()
    return s if s in {"routine", "urgent", "asap", "stat"} else None


def strip_digits(s: str) -> str:
    """Synthea suffixes names with digits; compare without them."""
    return re.sub(r"\d+", "", s or "").strip()
