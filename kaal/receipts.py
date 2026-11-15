"""Medical receipt records: normalised values, family members and status.

A receipt keeps two views of each date and amount: ``fields`` holds the value
reports can compute with (``YYYY-MM-DD``, ``123.45``) and ``field_raw`` holds
what was printed or typed when that differs. ``upgrade`` brings any record to
this shape and is safe to run any number of times.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA = 2
DATE_FIELDS = ("service_date", "paid_date")
NORMALISED_FIELDS = (*DATE_FIELDS, "amount")
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_REVIEWED = "reviewed"

_MONTHS = {name: number for number, names in enumerate(
    ("jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july",
     "aug august", "sep sept september", "oct october", "nov november", "dec december"), start=1)
    for name in names.split()}
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
# Receipts in this vault are US documents: numeric dates are month first.
_US_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d)")
_MONTH_FIRST = re.compile(rf"\b({_MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.IGNORECASE)
_DAY_FIRST = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_WORD})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_AMOUNT = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?\s*(?:usd)?", re.IGNORECASE)


def _iso(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def normalize_date(raw: str) -> str:
    """Return the first date in ``raw`` as YYYY-MM-DD, or "" when none can be read."""
    text = (raw or "").strip()
    if not text:
        return ""
    match = _ISO_DATE.search(text)
    if match:
        return _iso(int(match[1]), int(match[2]), int(match[3]))
    match = _US_DATE.search(text)
    if match:
        year = int(match[3])
        return _iso(year + 2000 if year < 100 else year, int(match[1]), int(match[2]))
    match = _MONTH_FIRST.search(text)
    if match:
        return _iso(int(match[3]), _MONTHS[match[1].lower()], int(match[2]))
    match = _DAY_FIRST.search(text)
    if match:
        return _iso(int(match[3]), _MONTHS[match[2].lower()], int(match[1]))
    return ""


def normalize_amount(raw: str) -> str:
    """Return ``raw`` as a plain two-decimal amount, or "" unless it is exactly one amount."""
    match = _AMOUNT.fullmatch((raw or "").strip())
    if not match:
        return ""
    try:
        return str(Decimal(match[1].replace(",", "") + (match[2] or "")).quantize(Decimal("0.01")))
    except InvalidOperation:
        return ""


def amount_value(raw: str) -> Decimal | None:
    normalised = normalize_amount(raw)
    return Decimal(normalised) if normalised else None


def _name_words(name: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[^\W\d_]+", name or "")}


def clean_person_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()[:100]


def person_for(patient_name: str, people: list[str]) -> str:
    """The one family member whose every name word appears in ``patient_name``.

    Word order, case and punctuation are ignored, so "DOE, JANE Q" matches
    "Jane Doe". No match, or more than one, is "" and left for a person to settle.
    """
    words = _name_words(patient_name)
    if not words:
        return ""
    matches = [person for person in people if _name_words(person) and _name_words(person) <= words]
    return matches[0] if len(matches) == 1 else ""


def upgrade(receipt: dict[str, Any], *, reviewed: bool, people: list[str]) -> bool:
    """Bring one receipt record to the current shape in place; return whether it changed."""
    before = json.dumps(receipt, sort_keys=True)
    fields = receipt.setdefault("fields", {})
    receipt.setdefault("field_sources", {})
    raw = receipt.setdefault("field_raw", {})
    for name in NORMALISED_FIELDS:
        value = fields.get(name) or ""
        if not value:
            continue
        normalised = normalize_date(value) if name in DATE_FIELDS else normalize_amount(value)
        if normalised != value:
            raw[name] = value
            fields[name] = normalised
    flags: dict[str, str] = {}
    for name in NORMALISED_FIELDS:
        if raw.get(name) and not fields.get(name):
            flags[name] = "unparsed"
    patient = fields.get("patient_name") or ""
    receipt["person"] = person_for(patient, people)
    if patient and people and not receipt["person"]:
        flags["patient_name"] = "unknown_person"
    receipt["flags"] = flags
    receipt["status"] = STATUS_REVIEWED if reviewed else STATUS_NEEDS_REVIEW
    receipt["schema"] = SCHEMA
    return json.dumps(receipt, sort_keys=True) != before
