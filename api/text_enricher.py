import re
from typing import Callable, Iterable

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

PAUSE_MARKER = " ... "

QUOTE_PATTERN = re.compile(r'"([^"]+)"')
DATE_NUMERIC_PATTERN = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
DATE_MONTH_PATTERN = re.compile(
    r"\b("
    + "|".join(m[:3] for m in MONTH_NAMES)
    + "|"
    + "|".join(MONTH_NAMES)
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{2,4}))?\b",
    re.IGNORECASE,
)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _expand_numeric_date(match: re.Match) -> str:
    month, day, year = match.groups()
    month_idx = max(1, min(12, int(month))) - 1
    month_name = MONTH_NAMES[month_idx]
    parts = [month_name, _ordinal(int(day))]
    if year:
        parts.append(year if len(year) == 4 else f"20{year.zfill(2)}")
    spoken = " ".join(parts)
    return f"{spoken},"


def _expand_month_date(match: re.Match) -> str:
    month_token, day, year = match.groups()
    normalized = month_token.strip().lower()
    month_idx = next(
        (idx for idx, name in enumerate(MONTH_NAMES) if name.lower().startswith(normalized)),
        None,
    )
    month_name = MONTH_NAMES[month_idx] if month_idx is not None else month_token
    parts = [month_name, _ordinal(int(day))]
    if year:
        year_text = year if len(year) == 4 else f"20{year.zfill(2)}"
        parts.append(year_text)
    spoken = " ".join(parts)
    return f"{spoken},"


def _format_quote(match: re.Match) -> str:
    content = match.group(1).strip()
    if not content:
        return ""
    return f"{PAUSE_MARKER}{content}{PAUSE_MARKER}"


def _apply(pattern: re.Pattern, text: str, handler: Callable[[re.Match], str]) -> str:
    return pattern.sub(handler, text)


def _insert_list_pauses(text: str) -> str:
    """
    Insert audible pauses after comma-separated items in long lists.
    We look for sequences like "item1, item2, item3, or item4" and add a marker.
    """
    tokens = text.split(",")
    if len(tokens) <= 2:
        return text
    rebuilt: list[str] = []
    for idx, token in enumerate(tokens):
        trimmed = token.strip()
        rebuilt.append(trimmed)
        if idx < len(tokens) - 1:
            rebuilt.append(PAUSE_MARKER)
    return "".join(rebuilt)


def enrich_text_for_tts(text: str) -> str:
    """
    Add pacing cues for TTS engines by formatting quotes and dates.

    - Quoted phrases gain surrounding markers that introduce a pause.
    - Numeric and month-based dates expand to spoken form with trailing pause.
    """
    enriched = text
    enriched = _apply(QUOTE_PATTERN, enriched, _format_quote)
    enriched = _apply(DATE_NUMERIC_PATTERN, enriched, _expand_numeric_date)
    enriched = _apply(DATE_MONTH_PATTERN, enriched, _expand_month_date)
    enriched = _insert_list_pauses(enriched)
    return enriched
