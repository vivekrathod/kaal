#!/usr/bin/env python3
"""Kaal's demo: a Kaal that holds only made-up data, and goes back to it every night.

This runs inside the demo's container (see compose.yaml beside it):

    demo.py serve     start Kaal, fill it with the made-up data, and put that back every night
    demo.py health    succeed once Kaal answers and the made-up data is in (the health check)

Everything is made the way people make it: the administrator's setup link
from `kaal-server`, then Kaal's own API for the rest, so nothing here writes
Kaal's files. Nothing here prints a password or a setup link either.

While it runs, `kill -HUP` (deploy/demo/run.sh reset) puts the made-up data
back at once. Everyone who looks at the demo signs in with the same published
password, so every minute it also checks that the next visitor still can: a
changed password or vault name is put back (which ends every token made with
the old one), and a deleted account or vault, or storage that strangers have
nearly filled, brings the whole reset forward.

It needs the standard library, plus Pillow for the pictures, which the
container image has.
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("kaal.demo")

ADMIN = "owner"            # the administrator a new installation starts with
REVIEWER = "reviewer"
REVIEWER_NAME = "Jane Madeup"
SHARER = "john"            # a made-up person who shares the vaults; he has no password, so nobody can sign in as him
VAULT_NAME = "Demo: anything you add is removed every night"
HOUSEHOLD = "Madeup household (shared)"
FAMILY = ["Jane Madeup", "John Madeup", "Robin Madeup"]
NOTICE_TITLE = "Read me first: this is Kaal's demo"
TOKEN = {"name": "Kaal for Chrome", "scopes": ["read", "write"]}   # so that the Tokens page has one to show

LOCAL = "http://127.0.0.1:8765"
READY = Path("/tmp/kaal-demo-ready")
CHECK_SECONDS = 60         # how often the reviewer's account is checked (see keep_reviewer)
SESSION_COOKIES = {"kaal_session", "__Host-kaal_session"}   # over plain http, and over https
RESET_AT = "03:00"         # UTC, unless KAAL_DEMO_RESET_AT says otherwise
PASSWORD = re.compile(r"[A-Za-z0-9._-]{10,128}")


class DemoError(Exception):
    """Something the demo could not do. The message is safe to log: it never holds a password or a link."""


# --- made-up files


def _pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(items: list[tuple], *, width: int = 360, height: int = 540, title: str = "") -> bytes:
    """A one-page PDF with real text, which Kaal reads as it reads any PDF (its text, not OCR).

    ``items`` are ("text", x, y, size, bold, words) and ("box", x, y, w, h, (r, g, b)),
    with y counted up from the bottom of the page as PDF does.
    """
    drawn = []
    for item in items:
        if item[0] == "box":
            _kind, x, y, w, h, (r, g, b) = item
            drawn.append(f"{r:.3f} {g:.3f} {b:.3f} rg {x} {y} {w} {h} re f")
        else:
            _kind, x, y, size, bold, words = item
            drawn.append(f"0.1 0.1 0.1 rg BT /{'F2' if bold else 'F1'} {size} Tf {x} {y} Td ({_pdf_text(words)}) Tj ET")
    stream = "\n".join(drawn).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        f"<< /Title ({_pdf_text(title)}) /Producer (Kaal demo, made-up data) >>".encode("latin-1"),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 7 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    return bytes(out)


def printed_page(heading: str, rows: list[tuple[str, str]], *, width: int = 360, height: int = 540, value_at: int = 150) -> bytes:
    """A receipt or a letter: a heading in a band, then rows of label and value.

    A row with no label prints its value alone; a label of "-" draws a rule.
    Each label and its value share one printed line, as on real receipts.
    """
    items: list[tuple] = [("box", 0, height - 64, width, 64, (0.90, 0.93, 0.96)), ("text", 24, height - 40, 15, True, heading)]
    y = height - 90
    for label, value in rows:
        if label == "-":
            items.append(("box", 24, y + 6, width - 48, 0.8, (0.6, 0.6, 0.6)))
            y -= 14
            continue
        if label:
            items += [("text", 24, y, 10, True, label), ("text", value_at, y, 10, False, value)]
        else:
            items.append(("text", 24, y, 9, False, value))
        y -= 18
    return pdf(items, width=width, height=height, title=heading)


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow before 10.1 has one size only
        return ImageFont.load_default()


def receipt_photo(heading: str, rows: list[tuple[str, str]]) -> bytes:
    """A phone's photo of a paper receipt on a table, which Kaal reads by OCR."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 1040), (96, 84, 72))
    draw = ImageDraw.Draw(image)
    draw.rectangle((64, 40, 656, 1000), fill=(250, 248, 240))
    draw.text((100, 84), heading, fill=(20, 20, 20), font=_font(40))
    y = 170
    for label, value in rows:
        if label == "-":
            draw.line((100, y + 12, 620, y + 12), fill=(150, 150, 150), width=2)
            y += 34
            continue
        draw.text((100, y), f"{label} {value}".strip(), fill=(25, 25, 25), font=_font(30))
        y += 52
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def garden_plan() -> bytes:
    """A drawing of a made-up garden, for a note with a picture in it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 600), (231, 240, 223))
    draw = ImageDraw.Draw(image)
    draw.text((30, 18), "Made-up garden plan", fill=(40, 62, 40), font=_font(30))
    draw.rectangle((30, 70, 870, 130), fill=(205, 194, 178), outline=(120, 108, 96), width=3)
    draw.text((400, 86), "House", fill=(60, 50, 40), font=_font(24))
    draw.rectangle((420, 130, 480, 575), fill=(214, 204, 184))
    beds = [((60, 170, 390, 310), (118, 168, 92), "Tomatoes and beans"), ((60, 340, 390, 470), (148, 188, 110), "Herbs"),
            ((520, 170, 845, 330), (104, 160, 84), "Lawn"), ((520, 360, 700, 560), (166, 136, 98), "Compost"),
            ((720, 360, 845, 560), (158, 158, 168), "Shed")]
    for box, colour, name in beds:
        draw.rounded_rectangle(box, radius=18, fill=colour, outline=(70, 90, 60), width=2)
        draw.text((box[0] + 16, box[1] + 14), name, fill=(255, 255, 255), font=_font(24))
    draw.text((60, 500), "Water butt by the shed.", fill=(60, 70, 60), font=_font(20))
    draw.text((60, 530), "The path is 60 cm wide.", fill=(60, 70, 60), font=_font(20))
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


# --- the made-up content


def _us(day: date) -> str:
    return f"{day:%m/%d/%Y}"


def _long(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


@dataclass
class Receipt:
    file: str
    title: str
    heading: str
    printed: list[tuple[str, str]]
    # "wait" leaves it as Kaal read it, waiting for review; "review" and
    # "reimburse" first correct what Kaal could not read into ``expected``.
    then: str = "wait"
    expected: dict[str, str] = field(default_factory=dict)
    source_url: str = ""
    source_title: str = ""
    captured: date | None = None
    reimbursed: tuple[str, str] = ("", "")   # date, note

    def content(self) -> tuple[str, bytes]:
        if self.file.endswith(".jpg"):
            return "image/jpeg", receipt_photo(self.heading, self.printed)
        return "application/pdf", printed_page(self.heading, self.printed)


def receipts(today: date) -> list[Receipt]:
    """Receipts for the made-up Madeup family: some waiting for review, some reviewed, one reimbursed."""
    ago = lambda days: today - timedelta(days=days)  # noqa: E731
    clinic = ["123 Example Street, Anytown", "Phone 555-0142"]
    # Oldest first: the last one captured is at the top of the receipts waiting for review.
    return [
        Receipt("example-dental-cleaning.pdf", "Example Dental Group: cleaning", "EXAMPLE DENTAL GROUP",
                [("", "300 Example Boulevard, Anytown"), ("-", ""), ("Patient name:", "MADEUP, JOHN"), ("Facility:", "Example Dental Group"),
                 ("Date of service:", _us(ago(38))), ("Date paid:", _us(ago(38))), ("Insurance:", "Example Dental Plan"),
                 ("Procedure:", "Cleaning and exam"), ("You paid:", "$95.00"), ("-", ""), ("", "Made up for Kaal's demo.")],
                then="review", expected={"amount": "95.00", "provider": "Example Dental Group", "service_date": ago(38).isoformat()},
                source_url="https://patients.example.net/payments/5520", source_title="Payment receipt", captured=ago(37)),
        Receipt("example-pt-copay.pdf", "Physical therapy copay", "EXAMPLE PT & REHAB LLC",
                [("", "17 Example Lane, Anytown"), ("-", ""), ("Patient:", "Jane Madeup"), ("Visit date:", _us(ago(61))),
                 ("Session:", "Physical therapy, 45 minutes"), ("Amount collected:", "$35.00"), ("-", ""), ("", "Made up for Kaal's demo.")],
                then="review", expected={"amount": "35.00", "provider": "Example Physical Therapy", "service_date": ago(61).isoformat()},
                captured=ago(60)),
        Receipt("example-pharmacy-last-year.pdf", "Example Street Pharmacy: prescription", "EXAMPLE STREET PHARMACY",
                [("", "88 Example Street, Anytown"), ("-", ""), ("Patient:", "John Madeup"), ("Facility:", "Example Street Pharmacy"),
                 ("Date paid:", _us(ago(380))), ("Item:", "Prescription, 30 days"), ("Total paid:", "$18.40"), ("-", ""), ("", "Made up for Kaal's demo.")],
                then="review", expected={"amount": "18.40", "provider": "Example Street Pharmacy", "paid_date": ago(380).isoformat()},
                captured=ago(379)),
        Receipt("example-family-clinic-last-year.pdf", "Example Family Clinic: sick visit", "EXAMPLE FAMILY CLINIC",
                [("", clinic[0]), ("", clinic[1]), ("-", ""), ("Patient:", "Robin Madeup"), ("Practice:", "Example Family Clinic"),
                 ("Date of service:", _us(ago(400))), ("Payment date:", _us(ago(400))), ("Insurance:", "Example Health Plan"),
                 ("-", ""), ("Amount paid:", "$25.00"), ("", "This receipt is made up for Kaal's demo.")],
                then="reimburse", expected={"amount": "25.00", "provider": "Example Family Clinic", "service_date": ago(400).isoformat()},
                source_url="https://portal.example.org/receipts/visit-0617", source_title="Example Family Clinic: payment received",
                captured=ago(399), reimbursed=(ago(370).isoformat(), "HSA withdrawal (made up)")),
        Receipt("example-dental-cleaning-copy.pdf", "Example Dental Group: cleaning (saved twice)", "EXAMPLE DENTAL GROUP",
                [("", "300 Example Boulevard, Anytown"), ("-", ""), ("Patient name:", "MADEUP, JOHN"), ("Facility:", "Example Dental Group"),
                 ("Date of service:", _us(ago(38))), ("You paid:", "$95.00"), ("-", ""), ("", "Made up for Kaal's demo.")],
                then="trash", captured=ago(36)),
        Receipt("example-imaging-statement.pdf", "Example Imaging Center: statement", "EXAMPLE IMAGING CENTER",
                [("", "9 Example Plaza, Anytown"), ("", "STATEMENT"), ("-", ""), ("Patient:", "Robin Madeup"), ("Insurance:", "Example Health Plan"),
                 ("Date of service:", _us(ago(31))), ("Charges:", "$310.00"), ("Adjustments:", "-$190.00"), ("Amount due:", "$120.00"),
                 ("-", ""), ("", "A made-up bill: nothing has been paid on it yet.")],
                source_url="https://billing.example.com/statements/7781", source_title="Your statement is ready", captured=ago(29)),
        Receipt("IMG_0412.jpg", "Eye exam receipt (photo)", "EXAMPLE EYE CARE",
                [("", "45 Example Avenue, Anytown"), ("-", ""), ("Patient:", "Robin Madeup"), ("Provider:", "Example Eye Care"),
                 ("Date of service:", _us(ago(24))), ("Service:", "Eye exam"), ("Amount paid:", "$60.00"), ("-", ""), ("", "Made up for Kaal's demo")],
                captured=ago(23)),
        Receipt("example-street-pharmacy.pdf", "Example Street Pharmacy", "EXAMPLE STREET PHARMACY",
                [("", "88 Example Street, Anytown"), ("-", ""), ("", "SALE - APPROVED"), ("Date:", _us(ago(16))), ("Card:", "XXXX XXXX XXXX 0000"),
                 ("Items:", "2"), ("Total Amount:", "$12.80"), ("-", ""), ("", "Keep this receipt for your records (made up).")],
                captured=ago(16)),
        Receipt("example-family-clinic-visit.pdf", "Example Family Clinic: office visit", "EXAMPLE FAMILY CLINIC",
                [("", clinic[0]), ("", clinic[1]), ("-", ""), ("Patient:", "Jane Madeup"), ("Practice:", "Example Family Clinic"),
                 ("Date of service:", _us(ago(9))), ("Payment date:", _us(ago(9))), ("Insurance:", "Example Health Plan"),
                 ("Visit:", "Office visit, established patient"), ("-", ""), ("Amount paid:", "$45.00"),
                 ("", "Paid by card ending 0000."), ("", "This receipt is made up for Kaal's demo.")],
                source_url="https://portal.example.org/receipts/visit-1042", source_title="Example Family Clinic: payment received", captured=ago(8)),
    ]


@dataclass
class Note:
    title: str
    body: str
    tags: list[str]
    # Files by name. The body refers to one as attachment:NAME, which becomes
    # its id once it is stored; a file the body does not mention is listed under the note.
    files: dict[str, bytes] = field(default_factory=dict)
    trash: bool = False


def northside_report(today: date) -> bytes:
    visit = today - timedelta(days=52)
    return printed_page("Northside Heating (made up)", [
        ("", "Boiler service report"), ("-", ""), ("Customer:", "The Madeup family"), ("Address:", "12 Example Road, Anytown"),
        ("Date of visit:", _us(visit)), ("Boiler:", "Example Combi 30"), ("Pressure, cold:", "1.5 bar"), ("Flue check:", "Passed"),
        ("Gas rate:", "Within range"), ("Next service:", _us(visit + timedelta(days=365))), ("-", ""),
        ("", "Radiators bled in the hall and the second bedroom."), ("", "Everything on this page is made up for Kaal's demo.")],
        width=595, height=842, value_at=190)


def insurance_summary(today: date) -> bytes:
    renewal = today + timedelta(days=74)
    return printed_page("Example Home Insurance (made up)", [
        ("", "Policy summary"), ("-", ""), ("Policyholder:", "Jane and John Madeup"), ("Policy number:", "EX-0000-0000"),
        ("Cover:", "Buildings and contents"), ("Renewal date:", _us(renewal)), ("Excess:", "$250"), ("Emergency line:", "555-0199"),
        ("-", ""), ("", "Not a real policy: made up for Kaal's demo.")], width=595, height=842, value_at=190)


def notes(today: date) -> list[Note]:
    """The notes in the reviewer's own vault, oldest first: the last one is listed at the top."""
    ago = lambda days: today - timedelta(days=days)  # noqa: E731
    return [
        Note("Made-up packing list (old)", "- Passports (made up)\n- Chargers\n- Sun cream\n- Robin's swimming things\n", ["travel"], trash=True),
        Note("Made-up car service record", f"""# Car service record

The family's made-up 2019 Example Hatchback.

| Date | Mileage | Work | Cost |
|---|---|---|---|
| {_long(ago(40))} | 41,200 | Oil and filter | $89.00 |
| {_long(ago(230))} | 35,900 | Front brake pads | $240.00 |
| {_long(ago(410))} | 30,100 | Yearly service | $310.00 |

Tyre pressure: **36 psi** front, **34 psi** rear. The next service is due at 46,000 miles.
""", ["car"]),
        Note("Made-up recipe: lentil soup", """# Lentil soup

Serves four. Made up for Kaal's demo, but it does work.

## You need

- 1 cup of red lentils
- 1 onion, 2 carrots and 2 sticks of celery, chopped
- 1 litre of vegetable stock
- 1 teaspoon of cumin, and the juice of half a lemon

## Method

1. Soften the onion, carrot and celery in a little oil, about 8 minutes.
2. Stir in the cumin, then the lentils and the stock.
3. Simmer for 20 minutes, blend half of it, and add the lemon.
""", ["recipes"]),
        Note("Made-up home insurance summary", f"""# Home insurance

Renews on **{_long(today + timedelta(days=74))}**. The policy summary from the made-up insurer:

[insurance-summary.pdf](attachment:insurance-summary.pdf)

- Emergency line: 555-0199 (made up)
- Excess: $250
""", ["home", "insurance"], files={"insurance-summary.pdf": insurance_summary(today)}),
        Note("Made-up Wi-Fi and alarm codes", """# Wi-Fi and alarm codes

None of these are real: this is Kaal's demo.

- Wi-Fi network: **Madeup-Home**
- Wi-Fi password: `not-a-real-password`
- Alarm code: 0000
- Spare key: with the neighbours at number 14
""", ["home"]),
        Note("Made-up shopping list", """- [ ] Oat milk
- [ ] Lemons
- [x] Coffee beans
- [ ] Bin bags
- [ ] Birthday card for Robin
""", ["home"]),
        Note("Made-up garden plan", """# Garden plan

![Garden plan](attachment:garden-plan.png)

- Tomatoes go in after the last frost.
- Herbs: basil, parsley and chives.
- Turn the compost once a month.
""", ["garden", "home"], files={"garden-plan.png": garden_plan()}),
        Note("Made-up house notes", """# House notes

Made up for Kaal's demo.

## Boiler

**Pressure should be 1.5 bar** when the heating is off. Below 1 bar, top it up with the filling loop under the boiler.

The last service report from Northside Heating is attached below.

## Radiators

Bleed the radiators each autumn, starting downstairs.

| Room | Radiators | Valve |
|---|---|---|
| Hall | 1 | Thermostatic |
| Kitchen | 1 | Manual |
| Living room | 2 | Thermostatic |
| Bedrooms | 3 | Thermostatic |

## Stopcock

Under the kitchen sink, behind the bucket.
""", ["home"], files={"northside.pdf": northside_report(today)}),
    ]


def household_notes(today: date) -> list[Note]:
    """The notes in the vault the reviewer shares with John."""
    term = lambda days: _long(today + timedelta(days=days))  # noqa: E731
    return [
        Note("Made-up chores rota", """| Day | Dishes | Bins |
|---|---|---|
| Monday | Robin | John |
| Wednesday | Jane | Robin |
| Friday | John | Jane |
""", ["home"]),
        Note("Made-up school term dates", f"""# School term dates (made up)

- Winter break: {term(35)} to {term(49)}
- Spring term starts: {term(50)}
- Half term: {term(98)} to {term(102)}
""", ["school"]),
    ]


def notice(reset_at: str) -> Note:
    return Note(NOTICE_TITLE, f"""# This is Kaal's demo

Everything here is made up: the Madeup family, their notes and their receipts.

**Anything you add is removed every night** (at {reset_at} UTC), when the demo goes back to what you see now. Please do not put anything real in it.

Things to try:

- **Notes**: search for "radiators", open *Made-up house notes* and its PDF, or the garden plan with its picture.
- **Receipts**: four are waiting to be checked. Open one, compare the values with the receipt, and press *Looks right*.
- **Totals**: what the family paid, per person and per year, and what has not been claimed back yet.
- **Trash**: a note and a receipt are in it, to restore or delete.
- **Settings**: the family, the two vaults this account can open (one shared), and its tokens.

Receipts are read here by Kaal's own rules only: reading them with a model is switched off, and nothing is sent anywhere.
""", ["demo"])


# --- Kaal's API, as a person's browser or the iPhone app uses it


Sender = Callable[[str, str, dict[str, str], bytes], tuple[int, list[str], bytes]]


def multipart(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """A form with files, as a browser sends one: (body, content type)."""
    boundary = "kaal-demo-" + secrets.token_hex(12)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + value.encode() + b"\r\n"
             for name, value in fields.items()]
    parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {media}\r\n\r\n'.encode()
              + content + b"\r\n" for name, filename, media, content in files]
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class Kaal:
    """One person signed in to Kaal: the session they were given in a cookie, sent back in
    the X-Kaal-Session header with X-Kaal-Request: 1, as the iPhone app does. (A cookie
    would do over plain http only: over https its name changes and browsers alone may send it.)"""

    def __init__(self, send: Sender) -> None:
        self._send = send
        self.session = ""

    def call(self, method: str, path: str, body: dict | None = None, *, files: list[tuple[str, str, str, bytes]] | None = None,
             fields: dict[str, str] | None = None) -> object:
        headers = {"X-Kaal-Request": "1", "Accept": "application/json", "User-Agent": "Kaal demo"}
        if self.session:
            headers["X-Kaal-Session"] = self.session
        data = b""
        if files is not None:
            data, headers["Content-Type"] = multipart(fields or {}, files)
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        status, cookies, raw = self._send(method, path, headers, data)
        for cookie in cookies:
            name, _, rest = cookie.partition("=")
            if name.strip() in SESSION_COOKIES:
                self.session = rest.split(";", 1)[0].strip().strip('"')
        try:
            answer = json.loads(raw or b"{}")
        except ValueError:
            answer = {}
        if status != 200:
            reason = answer.get("error", "") if isinstance(answer, dict) else ""
            raise DemoError(f"{method} {path.split('?')[0]} was refused ({status}): {reason}")
        return answer.get("result", answer) if isinstance(answer, dict) else answer


def http_sender(base: str) -> Sender:
    """Send requests to a Kaal over HTTP, never through a proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def send(method: str, path: str, headers: dict[str, str], data: bytes) -> tuple[int, list[str], bytes]:
        request = urllib.request.Request(base + path, data=data if data or method in {"POST", "PUT", "PATCH"} else None, headers=headers, method=method)
        try:
            with opener.open(request, timeout=120) as answer:
                return answer.status, answer.headers.get_all("Set-Cookie") or [], answer.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.headers.get_all("Set-Cookie") or [], error.read()

    return send


def setup_code(printed: str) -> str:
    """The code in a setup link that `kaal-server account setup-link` printed."""
    found = re.search(r"#/setup/([A-Za-z0-9_-]+)\s*$", printed.strip())
    if not found:
        raise DemoError("kaal-server did not print a setup link.")
    return found.group(1)


def sign_in_with_setup_link(kaal: Kaal, run: Callable[[list[str]], str], login: str, password: str, name: str) -> dict:
    """Choose a person's password with a single-use link, the way they would. It signs everyone else out."""
    code = setup_code(run(["account", "setup-link", login, "--hours", "0.1"]))
    return kaal.call("POST", "/v1/setup", {"code": code, "password": password, "name": name})


# --- filling a new Kaal


def _add_note(kaal: Kaal, note: Note) -> str:
    note_id = kaal.call("POST", "/v1/library/notes", {"title": note.title, "body": note.body, "tags": note.tags})["note"]["id"]
    body = note.body
    for name, content in note.files.items():
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        stored = kaal.call("POST", f"/v1/library/notes/{note_id}/attachments", files=[("file", name, media, content)])
        body = body.replace(f"attachment:{name}", f"attachment:{stored['attachment']['id']}")
    if body != note.body:
        kaal.call("PUT", f"/v1/library/notes/{note_id}", {"title": note.title, "body": body, "tags": note.tags})
    if note.trash:
        kaal.call("POST", f"/v1/library/notes/{note_id}/trash")
    return note_id


def _wait_until_read(kaal: Kaal, seconds: float = 180) -> None:
    """Receipts are read after they are captured; wait until none is left being read."""
    deadline = time.monotonic() + seconds
    while kaal.call("GET", "/v1/medical/receipts?status=extracting&limit=1"):
        if time.monotonic() > deadline:
            raise DemoError("Kaal took too long to read the made-up receipts.")
        time.sleep(0.5)


def _tick() -> None:
    # Kaal records times to the second, so a little over one keeps every list in the order things were added.
    time.sleep(1.05)


def seed(connect: Callable[[], Kaal], run: Callable[[list[str]], str], *, reviewer_password: str, admin_password: str,
         today: date, reset_at: str = RESET_AT, pause: Callable[[], None] = _tick) -> dict:
    """Fill a new, empty Kaal with the made-up demo, through its API as people would.

    ``connect`` gives a new, signed-out session; ``run`` runs a kaal-server
    command on the server and returns what it printed. ``pause`` is called
    between notes and between receipts and must let Kaal's clock move on by a
    second, so that each list's order is the same every time.

    Answers how much was put in, and under "vaults" the reviewer's two vaults
    (id to name), which keep_reviewer looks after until the next reset.
    """
    for which, password in (("reviewer", reviewer_password), ("administrator", admin_password)):
        if not PASSWORD.fullmatch(password or ""):
            raise DemoError(f"The {which}'s password must be 10 to 128 letters, digits, dots, dashes or underscores.")

    admin = connect()
    sign_in_with_setup_link(admin, run, ADMIN, admin_password, "Demo administrator")
    admin.call("POST", "/v1/admin/sign-up", {"signup": "invitation"})
    made = admin.call("POST", "/v1/admin/people", {"login": REVIEWER, "name": REVIEWER_NAME, "vault_name": VAULT_NAME})
    admin.call("DELETE", "/v1/session")
    # Made at the server, so that no setup link for him exists anywhere.
    run(["account", "add", SHARER, "--name", "John Madeup", "--no-vault"])

    jane = connect()
    # The password everyone knows is given to the account last. Until then it has one nobody
    # knows, so no visitor can sign in to a demo that is still being filled (and, by deleting
    # the vault under it, keep it from ever being ready).
    filling = secrets.token_urlsafe(24)
    signed_in = jane.call("POST", "/v1/setup", {"code": made["setup_path"].rsplit("/", 1)[-1], "password": filling, "name": REVIEWER_NAME})
    main = signed_in["vault"]["id"]
    jane.call("POST", "/v1/medical/people", {"add": FAMILY})

    household = jane.call("POST", "/v1/account/vaults", {"name": HOUSEHOLD})["vault"]["id"]
    jane.call("POST", "/v1/session/vault", {"vault": household})
    for note in household_notes(today):
        _add_note(jane, note)
        pause()
    jane.call("POST", "/v1/vault/members", {"login": SHARER, "role": "editor"})
    jane.call("POST", "/v1/session/vault", {"vault": main})
    jane.call("POST", "/v1/vault/members", {"login": SHARER, "role": "viewer"})

    made_receipts = receipts(today)
    for receipt in made_receipts:
        media, content = receipt.content()
        sent = {"title": receipt.title, "source_url": receipt.source_url, "source_title": receipt.source_title}
        if receipt.captured:
            sent["captured_at"] = f"{receipt.captured.isoformat()}T18:20:00+00:00"
        note_id = jane.call("POST", "/v1/medical/receipts", files=[("file", receipt.file, media, content)], fields=sent)["id"]
        # One at a time, so the order receipts were last changed in is the same every night.
        _wait_until_read(jane)
        if receipt.then == "trash":
            jane.call("POST", f"/v1/medical/receipts/{note_id}/trash")
        elif receipt.then != "wait":
            read = jane.call("GET", f"/v1/medical/receipts/{note_id}")["receipt"].get("fields", {})
            corrections = {name: value for name, value in receipt.expected.items() if (read.get(name) or "") != value}
            if corrections:
                jane.call("PATCH", f"/v1/medical/receipts/{note_id}/fields", {"fields": corrections})
            jane.call("POST", f"/v1/medical/receipts/{note_id}/review")
            if receipt.then == "reimburse":
                when, why = receipt.reimbursed
                jane.call("POST", "/v1/medical/receipts/reimburse", {"ids": [note_id], "date": when, "note": why})
        pause()

    made_notes = notes(today)
    for note in made_notes:
        _add_note(jane, note)
        pause()
    jane.call("POST", "/v1/account/tokens", {**TOKEN, "vault": main, "password": filling})
    # The notice is written a moment after everything else, so it is always at the top of the list.
    pause()
    notice_id = _add_note(jane, notice(reset_at))
    jane.call("POST", "/v1/account/password", {"current": filling, "new": reviewer_password})
    jane.call("DELETE", "/v1/session")
    return {"notes": len(made_notes) + 1, "household_notes": len(household_notes(today)), "receipts": len(made_receipts),
            "vaults": {main: VAULT_NAME, household: HOUSEHOLD},
            # What keep_reviewer puts back if a visitor rewrites or removes it: "seen" is when Kaal last
            # recorded a change to it, learnt at the first check.
            "notice": {"id": notice_id, "note": notice(reset_at), "seen": None}}


# --- keeping the reviewer's account as App Review was told it is
#
# The reviewer's password is published, so anyone may do whatever that account
# may: change its password, delete it or its vaults, rename them. None of that
# may keep the next visitor out until the night's reset.


def what_is_wrong(password: str, vaults: dict[str, str], notice: dict | None = None) -> list[str]:
    """What a visitor has done to the reviewer's account that would keep others out or
    mislead them: "gone" (the account or one of its vaults was deleted or given away),
    "password" (changed), "names" (a vault renamed, so the notice in its name is lost),
    "notice" (the note that says this is a demo was rewritten, moved to the Trash or deleted),
    "extra" (a vault was added, under whatever name its maker liked).

    Asks Kaal's own account store, so it signs nobody in and leaves the count of wrong passwords alone.
    """
    from kaal import accounts, passwords, server

    settings = server.Settings.from_env()
    store = accounts.AccountStore(settings.accounts_file, settings.home)
    person = store.account(REVIEWER)
    if person is None or person["disabled_at"]:
        return ["gone"]
    owned = {vault["id"]: vault["name"] for vault in store.vaults_of(person["id"]) if vault["role"] == "owner"}
    if any(vault not in owned for vault in vaults):
        return ["gone"]
    found = []
    if not passwords.verify_password(store.password_hash(person["id"]), password):
        found.append("password")
    if any(owned[vault] != name for vault, name in vaults.items()):
        found.append("names")
    if any(vault not in vaults for vault in owned):
        found.append("extra")
    if notice is not None:
        from kaal import core

        main = next(vault for vault, name in vaults.items() if name == VAULT_NAME)
        with core.use_vault(core.Vault(store.folder_of(store.vault(main)))):
            kept = next((note for note in core.load_index().get("notes", []) if note.get("id") == notice["id"]), None)
        if kept is not None and notice.get("seen") is None and not kept.get("trashed_at") and kept.get("title") == NOTICE_TITLE:
            notice["seen"] = kept.get("updated")
        if kept is None or kept.get("trashed_at") or kept.get("title") != NOTICE_TITLE or kept.get("updated") != notice.get("seen"):
            found.append("notice")
    return found


def keep_reviewer(connect: Callable[[], Kaal], run: Callable[[list[str]], str], password: str, vaults: dict[str, str],
                  wrong: Callable[..., list[str]] = what_is_wrong, notice: dict | None = None) -> list[str]:
    """Put the reviewer's password, vault names and the demo's notice back if a visitor changed
    them, remove vaults a visitor added, and say what was found. ["gone"] is for the caller: only
    starting again brings a deleted account or vault back."""
    found = wrong(password, vaults, notice) if notice is not None else wrong(password, vaults)
    if not found or "gone" in found:
        return found
    person = connect()
    if "password" in found:
        # A setup link, as for someone who forgot theirs. It signs everyone out, whoever changed
        # it included, and ends the account's tokens, a visitor's with the one the Tokens page shows.
        sign_in_with_setup_link(person, run, REVIEWER, password, REVIEWER_NAME)
        try:
            main = next(vault for vault, name in vaults.items() if name == VAULT_NAME)
            person.call("POST", "/v1/account/tokens", {**TOKEN, "vault": main, "password": password})
        except (DemoError, StopIteration):
            # Visitors share how often the account may give its password and make tokens, so
            # this can be refused. The password is back, which is what matters; the token waits for the reset.
            pass
    else:
        person.call("POST", "/v1/sign-in", {"login": REVIEWER, "password": password})
    if "names" in found:
        for vault, name in vaults.items():
            person.call("POST", "/v1/session/vault", {"vault": vault})
            person.call("POST", "/v1/vault/name", {"name": name})
    main = next(vault for vault, name in vaults.items() if name == VAULT_NAME)
    if "extra" in found:
        for vault in person.call("GET", "/v1/account")["vaults"]:
            if vault["role"] == "owner" and vault["id"] not in vaults:
                person.call("POST", "/v1/session/vault", {"vault": vault["id"]})
                person.call("DELETE", "/v1/vault", {"password": password})
    if "notice" in found and notice is not None:
        person.call("POST", "/v1/session/vault", {"vault": main})
        wanted = notice["note"]
        try:
            person.call("POST", f"/v1/library/notes/{notice['id']}/restore")
        except DemoError:
            pass   # it was not in the Trash
        try:
            person.call("PUT", f"/v1/library/notes/{notice['id']}", {"title": wanted.title, "body": wanted.body, "tags": wanted.tags})
        except DemoError:
            notice["id"] = _add_note(person, wanted)   # it had been deleted for good
        notice["seen"] = None   # learnt again at the next check
    person.call("POST", "/v1/session/vault", {"vault": main})
    person.call("DELETE", "/v1/session")
    return found


def room_to_keep() -> int:
    """How many bytes of the demo's storage must stay free: Kaal's own floor (KAAL_FREE_SPACE_MB),
    below which it stores nothing new, and twice the largest file it allows on top, so that
    whoever comes next can add one (a file takes about a third more room once encrypted)."""
    from kaal import server

    settings = server.Settings.from_env()
    return settings.free_space_bytes + 2 * settings.max_upload_bytes


def nearly_full(folder: Path, keep: int) -> bool:
    """Whether strangers have nearly filled the demo's storage. Kaal would soon refuse whatever
    the next visitor adds, so the demo starts again rather than wait for the night."""
    return shutil.disk_usage(folder).free < keep


# --- the container's main process


def next_reset(now: datetime, at: str) -> datetime:
    """The first moment after ``now`` that the clock reads ``at`` (HH:MM, UTC)."""
    found = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (at or "").strip())
    if not found:
        raise DemoError(f"The reset time must be HH:MM, such as {RESET_AT}.")
    then = now.astimezone(timezone.utc).replace(hour=int(found[1]), minute=int(found[2]), second=0, microsecond=0)
    return then if then > now else then + timedelta(days=1)


def mount_types(mounts: str) -> dict[str, str]:
    """Mount point -> file system type, from the text of /proc/self/mounts."""
    found = {}
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            found[parts[1].replace("\\040", " ")] = parts[2]
    return found


def refuse_unless_in_memory(folders: list[Path], mounts: str) -> None:
    """The demo empties its folders at every reset, so it runs only where they are
    held in memory (tmpfs) for this one container, never on a volume that could hold real data."""
    types = mount_types(mounts)
    lasting = [str(folder) for folder in folders if types.get(str(folder)) != "tmpfs"]
    if lasting:
        raise DemoError(f"Refusing to run: {', '.join(lasting)} must be in memory (tmpfs), because the demo empties it every night. "
                        "Start the demo with deploy/demo/run.sh.")


def empty(folder: Path) -> None:
    for entry in folder.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


class Stopping(BaseException):
    """Raised by SIGTERM wherever the main process is, so that Kaal is stopped on the way out."""


def serve() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stdout)
    reviewer_password = os.environ.get("KAAL_DEMO_PASSWORD", "")
    admin_password = os.environ.get("KAAL_DEMO_ADMIN_PASSWORD", "")
    reset_at = (os.environ.get("KAAL_DEMO_RESET_AT") or RESET_AT).strip()
    home, keys = Path(os.environ["KAAL_HOME"]), Path(os.environ["KAAL_KEY_FILE"]).parent
    try:
        next_reset(datetime.now(timezone.utc), reset_at)
        refuse_unless_in_memory([home, keys], Path("/proc/self/mounts").read_text())
    except (DemoError, OSError) as error:
        log.error("%s", error)
        return 2
    # Kaal itself is not told the demo's passwords.
    quiet = {name: value for name, value in os.environ.items() if not name.startswith("KAAL_DEMO_")}

    def run(args: list[str]) -> str:
        done = subprocess.run(["kaal-server", *args], env=quiet, capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise DemoError(f"kaal-server {' '.join(args[:2])} failed: {(done.stderr.strip().splitlines() or [''])[-1]}")
        return done.stdout

    connect = lambda: Kaal(http_sender(LOCAL))  # noqa: E731
    keep = room_to_keep()
    wake = threading.Event()
    asked = {"reset": False}

    def stop_now(_number: int, _frame: object) -> None:
        raise Stopping()

    def reset_now(_number: int, _frame: object) -> None:
        asked["reset"] = True
        wake.set()

    signal.signal(signal.SIGTERM, stop_now)
    signal.signal(signal.SIGINT, stop_now)
    signal.signal(signal.SIGHUP, reset_now)
    try:
        while True:
            asked["reset"] = False
            READY.unlink(missing_ok=True)
            for folder in (home, keys):
                empty(folder)
            log.info("starting Kaal and filling it with the made-up data")
            kaal_server = subprocess.Popen(["kaal-entrypoint", "kaal-server", "serve"], env=quiet)
            try:
                _wait_until_up(kaal_server)
                made = seed(connect, run, reviewer_password=reviewer_password, admin_password=admin_password,
                            today=datetime.now(timezone.utc).date(), reset_at=reset_at)
                vaults = made["vaults"]
                READY.write_text(secrets.token_hex(8) + "\n")
                when = next_reset(datetime.now(timezone.utc), reset_at)
                log.info("ready: %d notes and %d receipts, all made up; back to this at %s UTC", made["notes"], made["receipts"], f"{when:%Y-%m-%d %H:%M}")
                # Starting again would not help a demo that is too small even when new.
                too_small = nearly_full(home, keep)
                if too_small:
                    log.error("the demo's storage is too small for its made-up data and a file of the largest size allowed; "
                              "give it more with KAAL_DEMO_STORAGE (see deploy/demo/README.md)")
                check_at = time.monotonic() + CHECK_SECONDS
                while not asked["reset"]:
                    if kaal_server.poll() is not None:
                        log.error("Kaal stopped by itself; starting again with the made-up data")
                        break
                    if datetime.now(timezone.utc) >= when:
                        log.info("putting the made-up data back, as every night")
                        break
                    if time.monotonic() >= check_at:
                        try:
                            found = keep_reviewer(connect, run, reviewer_password, vaults, notice=made["notice"])
                        except Exception as error:   # a check that fails is tried again; it is no reason to start over
                            found = []
                            log.error("could not check the reviewer's account (%s: %s)", type(error).__name__, error)
                        if "gone" in found:
                            log.warning("the reviewer's account or one of its vaults had been deleted; putting the made-up data back now")
                            break
                        if not too_small and nearly_full(home, keep):
                            log.warning("the demo's storage was nearly full; putting the made-up data back now")
                            break
                        if "password" in found:
                            log.warning("the reviewer's password had been changed; it is back to the one App Review was given")
                        if "names" in found:
                            log.warning("a vault had been renamed; its name is back")
                        if "notice" in found:
                            log.warning("the note that says this is a demo had been changed or removed; it is back")
                        if "extra" in found:
                            log.warning("a vault had been added; it is removed")
                        check_at = time.monotonic() + CHECK_SECONDS
                    wake.wait(max(1.0, min(30.0, (when - datetime.now(timezone.utc)).total_seconds(), check_at - time.monotonic())))
                    wake.clear()
                if asked["reset"]:
                    log.info("putting the made-up data back, as asked")
            except Exception as error:   # Stopping is not an Exception, so it passes on
                log.error("the made-up data could not be put in (%s: %s). Trying again in a minute.", type(error).__name__, error)
                # Not left reachable, half filled, while waiting.
                _stop(kaal_server)
                wake.wait(60)
                wake.clear()
            finally:
                READY.unlink(missing_ok=True)
                _stop(kaal_server)
    except Stopping:
        log.info("stopped; everything the demo held is gone")
        return 0


def _wait_until_up(kaal_server: subprocess.Popen, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if kaal_server.poll() is not None:
            raise DemoError("Kaal stopped while starting; see the lines above")
        if _answers():
            return
        time.sleep(0.5)
    raise DemoError("Kaal did not answer in time")


def _answers() -> bool:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(LOCAL + "/v1/health", timeout=4) as answer:
            return answer.status == 200
    except (OSError, ValueError):
        return False


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "serve":
        return serve()
    if command == "health":
        return 0 if READY.exists() and _answers() else 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
