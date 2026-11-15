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
EVALUATED_FIELDS = ("amount", "service_date", "paid_date", "patient_name", "provider", "insurer")
TRUSTED_SOURCES = frozenset({"manual", "confirmed"})
SOURCE_RULES = "labelled-text"
SOURCE_AI_GROUNDED = "ai-grounded"
SOURCE_AI_UNVERIFIED = "ai-unverified"
_CLINICIAN = re.compile(r"(?:dr\.?|doctor)\s+", re.IGNORECASE)
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


def compare_field(name: str, truth: str, candidate: str) -> str:
    """Judge one extracted value against a value a person typed or confirmed.

    Returns "correct", "wrong" or "missing". Dates and amounts must be equal
    once normalised. Names are compared as sets of words, ignoring case, order
    and punctuation: the extracted name may carry extra words ("Jane Q Doe"
    for "Jane Doe"), or drop some as long as two or more remain.
    """
    if not (candidate or "").strip():
        return "missing"
    if name in NORMALISED_FIELDS:
        normalised = normalize_date(candidate) if name in DATE_FIELDS else normalize_amount(candidate)
        return "correct" if normalised and normalised == truth else "wrong"
    wanted, found = _name_words(truth), _name_words(candidate)
    if not wanted or not found:
        return "wrong"
    if wanted <= found or (found < wanted and len(found) >= 2):
        return "correct"
    return "wrong"


def dates_in_text(text: str) -> set[str]:
    """Every date printed in ``text``, normalised."""
    found: set[str] = set()
    for pattern in (_ISO_DATE, _US_DATE, _MONTH_FIRST, _DAY_FIRST):
        for match in pattern.finditer(text or ""):
            normalised = normalize_date(match.group(0))
            if normalised:
                found.add(normalised)
    return found


def amount_in_text(amount: str, text: str) -> bool:
    """Whether a normalised amount such as "1234.50" is printed in ``text``, with or without thousands separators."""
    whole, _, cents = amount.partition(".")
    grouped = "".join(digit + (",?" if index and index % 3 == 0 else "") for index, digit in zip(range(len(whole) - 1, -1, -1), whole))
    decimals = r"(?:\.00)?" if cents == "00" else r"\." + cents
    return re.search(r"(?<![\d.,])" + grouped + decimals + r"(?!\d|[.,]\d)", text or "") is not None


def _squashed(value: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", (value or "").casefold()).split())


def ground(name: str, value: str, text: str) -> bool:
    """Whether ``value`` can be found in the text read from the receipt.

    Amounts and dates are matched by value, so "$1,234.50" grounds "1234.50"
    and "10/16/2026" grounds "2026-10-16". Names must appear as the same run
    of words, ignoring case and punctuation.
    """
    if name == "amount":
        normalised = normalize_amount(value)
        return bool(normalised) and amount_in_text(normalised, text)
    if name in DATE_FIELDS:
        normalised = normalize_date(value)
        return bool(normalised) and normalised in dates_in_text(text)
    wanted = _squashed(value)
    return bool(wanted) and f" {wanted} " in f" {_squashed(text)} "


def merge_model_fields(fields: dict[str, str], sources: dict[str, str], model_fields: dict[str, str], text: str) -> dict[str, dict[str, str]]:
    """Fold a model's candidates into rule-based ``fields`` in place; return the disagreements.

    A model value found in the receipt text is ``ai-grounded``; one that is not
    (a name read from a logo, anything from a blurry photo) is ``ai-unverified``.
    Where the rules found nothing, the model's value is taken either way.
    Where both found something and disagree:
      - amounts and dates: the rule's value stays. A rule only fires on an
        explicit printed label such as "Amount paid", and on real receipts it
        has been sparse but not wrong;
      - names: the model's value is taken, because it reads the whole receipt
        while the rules match by position and are often wrong.
    Every disagreement is returned so a reviewer sees both values.
    """
    conflicts: dict[str, dict[str, str]] = {}
    for name in EVALUATED_FIELDS:
        value = (model_fields.get(name) or "").strip()
        if not value:
            continue
        if name == "provider" and _CLINICIAN.match(value):
            continue
        if name in NORMALISED_FIELDS:
            normalised = normalize_date(value) if name in DATE_FIELDS else normalize_amount(value)
            if not normalised:
                continue
        grounded = ground(name, value, text)
        source = SOURCE_AI_GROUNDED if grounded else SOURCE_AI_UNVERIFIED
        current = fields.get(name) or ""
        if not current:
            fields[name], sources[name] = value, source
            continue
        if name in NORMALISED_FIELDS:
            current_normalised = normalize_date(current) if name in DATE_FIELDS else normalize_amount(current)
            agree = compare_field(name, current_normalised, value) == "correct"
        else:
            agree = _squashed(current) == _squashed(value)
        if agree:
            if grounded:
                sources[name] = SOURCE_AI_GROUNDED
            continue
        conflicts[name] = {SOURCE_RULES: current, "ai": value}
        if name not in NORMALISED_FIELDS:
            fields[name], sources[name] = value, source
    return conflicts


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
