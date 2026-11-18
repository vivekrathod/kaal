#!/usr/bin/env python3
"""A Kaal that holds only made-up notes and receipts.

For store screenshots, the account App Review signs in with, and for trying
Kaal out without putting anything real into it. Everything it writes is
invented: the family is called Example, every receipt and letter says SAMPLE
on it, and the addresses, phone numbers and card numbers are ones that belong
to no one.

    bin/demo-data.py serve --port 8831
        Starts a throwaway Kaal in a new temporary folder, fills it, prints the
        address, the sign-in name and the password, and runs until Ctrl-C. The
        folder is deleted then. It listens on this machine only, over plain
        http, with a key of its own made for the run.

    bin/demo-data.py fill --url https://demo.example.org --token-file demo.token
        Fills an existing Kaal through its API. The token needs the read,
        reveal and write scopes. Use it on an empty vault. It stops, changing
        nothing, if the made-up notes are already there; --again adds them a
        second time.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAMILY = ["Jane Example", "Sam Example", "Leo Example"]
SAMPLE = "SAMPLE - made up for illustration. Not a real receipt."

# --- a PDF the way a provider's site hands one out


# Fonts to draw pages with, best first; Pillow's own font when none of these is here.
FONT_FILES = {
    False: [("/System/Library/Fonts/Helvetica.ttc", 0), ("/System/Library/Fonts/Supplemental/Arial.ttf", 0),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0), ("/usr/share/fonts/dejavu/DejaVuSans.ttf", 0)],
    True: [("/System/Library/Fonts/Helvetica.ttc", 1), ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
           ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0), ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 0)],
}


def font(size: float, bold: bool = False):
    from PIL import ImageFont

    for path, index in FONT_FILES[bold]:
        if Path(path).exists():
            return ImageFont.truetype(path, round(size), index=index)
    return ImageFont.load_default(size=round(size))


def _escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _width(text: str, size: float) -> float:
    """How wide Helvetica sets a short amount; enough to right-align figures."""
    narrow = {".": 278, ",": 278, " ": 278, "-": 333}
    return sum(narrow.get(character, 556) for character in text) * size / 1000


def _picture(width: float, height: float, marks: list[tuple], dpi: int = 150) -> tuple[bytes, int, int]:
    """The page drawn as a picture, as JPEG bytes and its size in pixels."""
    from PIL import Image, ImageDraw

    scale = dpi / 72
    image = Image.new("RGB", (round(width * scale), round(height * scale)), (255, 255, 255))
    pen = ImageDraw.Draw(image)
    rgb = lambda colour: tuple(round(part * 255) for part in colour)  # noqa: E731
    for mark in marks:
        if mark[0] == "box":
            _, x, y, w, h, colour = mark
            pen.rectangle((x * scale, (height - y - h) * scale, (x + w) * scale, (height - y) * scale), fill=rgb(colour))
        elif mark[0] == "rule":
            _, x1, x2, y, grey = mark
            pen.line((x1 * scale, (height - y) * scale, x2 * scale, (height - y) * scale), fill=rgb((grey,) * 3), width=max(1, round(0.7 * scale)))
        else:
            _, x, y, size, bold, colour, text, *align = mark
            anchor = "rs" if align == ["right"] else "ls"
            pen.text((x * scale, (height - y) * scale), text, font=font(size * scale, bold), fill=rgb(colour), anchor=anchor)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue(), image.width, image.height


def pdf_page(width: float, height: float, marks: list[tuple]) -> bytes:
    """One page drawn from boxes, rules and lines of text.

    The page is a picture, so it looks the same wherever it is shown, with its
    words laid over it as invisible text, as a scanner that reads its pages
    writes them. Kaal reads that text layer as it would a receipt saved from a
    provider's site. (Text drawn in a font the PDF only names is set in
    whatever font the viewer has, which can look broken.)
    """
    ops = []
    for mark in marks:
        if mark[0] == "text":
            _, x, y, size, bold, _colour, text, *align = mark
            if align == ["right"]:
                x -= _width(text, size)
            ops.append(f"BT 3 Tr /{'F2' if bold else 'F1'} {size} Tf {x:.1f} {y:.1f} Td ({_escaped(text)}) Tj ET")
    jpeg, pixels_wide, pixels_high = _picture(width, height, marks)
    stream = (f"q {width} 0 0 {height} 0 0 cm /Page Do Q\n" + "\n".join(ops)).encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 %d %d]/Contents 4 0 R/Resources<</Font<</F1 5 0 R/F2 6 0 R>>/XObject<</Page 7 0 R>>>>>>" % (width, height),
        b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica-Bold/Encoding/WinAnsiEncoding>>",
        b"<</Type/XObject/Subtype/Image/Width %d/Height %d/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode/Length %d>>\nstream\n%s\nendstream"
        % (pixels_wide, pixels_high, len(jpeg), jpeg),
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return out + b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


INK, GREY, WHITE = (0.13, 0.13, 0.13), (0.45, 0.45, 0.45), (1, 1, 1)


def receipt_pdf(receipt: dict) -> bytes:
    """A provider's payment receipt, laid out with the labels Kaal's rules look for."""
    width, height = 600, 860
    colour = tuple(int(receipt["colour"][i:i + 2], 16) / 255 for i in (1, 3, 5))
    right = width - 40
    marks: list[tuple] = [("box", 0, height - 116, width, 116, colour),
                          ("text", 40, height - 60, 28, True, WHITE, receipt["provider"].upper()),
                          ("text", 40, height - 92, 16, False, WHITE, receipt["address"])]
    y = height - 168
    marks.append(("text", 40, y, 22, True, INK, receipt.get("heading", "PAYMENT RECEIPT")))
    marks.append(("text", right, y, 16, False, GREY, f"No. {receipt['number']}", "right"))
    y -= 48
    for label, value in receipt["labels"]:
        marks.append(("text", 40, y, 18, True, INK, f"{label}:"))
        marks.append(("text", 224, y, 18, False, INK, value))
        y -= 30
    y -= 8
    marks.append(("rule", 40, right, y, 0.75))
    y -= 36
    for item, amount in receipt["items"]:
        marks.append(("text", 40, y, 18, False, INK, item))
        marks.append(("text", right, y, 18, False, INK, amount, "right"))
        y -= 30
    y -= 4
    marks.append(("rule", 40, right, y, 0.75))
    y -= 40
    label, amount = receipt["total"]
    marks.append(("text", 40, y, 22, True, INK, label))
    marks.append(("text", right, y, 22, True, INK, amount, "right"))
    y -= 36
    if receipt.get("paid_by"):
        marks.append(("text", 40, y, 17, False, GREY, f"Paid by: {receipt['paid_by']}"))
    marks.append(("text", 40, 80, 16, False, GREY, "Thank you. Questions? Call 555-0100."))
    marks.append(("text", 40, 48, 14, True, GREY, SAMPLE))
    return pdf_page(width, height, marks)


def letter_pdf(title: str, lines: list[str]) -> bytes:
    """A page of a letter or a manual: a heading and plain lines of text."""
    width, height = 595, 842
    marks: list[tuple] = [("text", 56, height - 84, 21, True, INK, title), ("rule", 56, width - 56, height - 102, 0.7)]
    y = height - 140
    for line in lines:
        if line:
            marks.append(("text", 56, y, 14, line.endswith(":"), INK, line))
        y -= 22
    marks.append(("text", 56, 50, 10.5, True, GREY, "SAMPLE - made up for illustration."))
    return pdf_page(width, height, marks)


def swatches_png() -> bytes:
    """A card of paint samples, drawn here, for a note with a picture in it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 860), (247, 244, 238))
    pen = ImageDraw.Draw(image)
    title = font(46, bold=True)
    label = font(34, bold=True)
    small = font(26)
    pen.text((70, 60), "Hall - sample pots", font=title, fill=(40, 40, 40))
    paints = [("Sage 214", "walls", (156, 175, 136)), ("Chalk white", "ceiling and trim", (240, 236, 226)),
              ("Harbour blue", "front door", (52, 84, 112)), ("Ochre 08", "cushions", (204, 150, 58))]
    for index, (name, use, colour) in enumerate(paints):
        x, y = 70 + (index % 2) * 545, 170 + (index // 2) * 330
        pen.rounded_rectangle((x + 6, y + 6, x + 516, y + 206), radius=22, fill=(225, 220, 210))
        pen.rounded_rectangle((x, y, x + 510, y + 200), radius=22, fill=colour, outline=(205, 200, 190), width=2)
        pen.text((x + 4, y + 220), name, font=label, fill=(40, 40, 40))
        pen.text((x + 4, y + 262), use, font=small, fill=(110, 110, 110))
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# --- what goes in

RECEIPTS = [
    # The first is left waiting for review: it is the one the screenshots open.
    {"provider": "Lakeside Dental Group", "address": "120 Example Avenue, Springfield", "colour": "#1f5f55", "number": "10482",
     "labels": [("Patient", "Jane Example"), ("Provider", "Lakeside Dental Group"), ("Date of Service", "10/02/2026"),
                ("Payment Date", "10/05/2026"), ("Insurance", "Acme Dental Plan")],
     "items": [("Cleaning and exam", "$185.00"), ("Insurance paid", "-$120.50")], "total": ("Amount paid:", "$64.50"),
     "paid_by": "Card ending 0000", "source": "https://portal.example.org/receipts/10482"},
    {"provider": "Maple Street Pharmacy", "address": "8 Maple Street, Springfield", "colour": "#7a3b69", "number": "RX-55817",
     "labels": [("Patient", "Sam Example"), ("Provider", "Maple Street Pharmacy"), ("Payment Date", "11/03/2026")],
     "items": [("Prescription, 30 days", "$12.40")], "total": ("Amount paid:", "$12.40"), "paid_by": "Card ending 0000"},
    {"provider": "Northside Pediatrics", "address": "45 North Road, Springfield", "colour": "#2f5d8a", "number": "S-2291",
     "labels": [("Patient", "Leo Example"), ("Provider", "Northside Pediatrics"), ("Date of Service", "10/20/2026"), ("Payment Date", "10/20/2026")],
     "items": [("Office visit", "$140.00"), ("Insurance paid", "-$115.00")], "total": ("Amount paid:", "$25.00"), "paid_by": "Card ending 0000"},
    {"provider": "Riverside Physiotherapy", "address": "3 Mill Lane, Springfield", "colour": "#8a5a2f", "number": "7713",
     "labels": [("Patient", "Jane Example"), ("Provider", "Riverside Physiotherapy"), ("Date of Service", "09/05/2026"), ("Payment Date", "09/05/2026")],
     "items": [("Physiotherapy session", "$90.00")], "total": ("Amount paid:", "$90.00"), "paid_by": "Card ending 0000", "review": True},
    {"provider": "Brightview Eye Care", "address": "17 Hill Street, Springfield", "colour": "#3c6e47", "number": "E-3307",
     "labels": [("Patient", "Leo Example"), ("Provider", "Brightview Eye Care"), ("Date of Service", "08/22/2026"), ("Payment Date", "08/22/2026")],
     "items": [("Eye exam", "$95.00"), ("Glasses", "$210.00"), ("Insurance paid", "-$160.00")], "total": ("Amount paid:", "$145.00"),
     "paid_by": "Card ending 0000", "review": True, "reimbursed": ("2026-09-01", "Claim 2026-14")},
    {"provider": "Northside Pediatrics", "address": "45 North Road, Springfield", "colour": "#2f5d8a", "number": "S-2140",
     "labels": [("Patient", "Leo Example"), ("Provider", "Northside Pediatrics"), ("Date of Service", "07/14/2026"), ("Payment Date", "07/14/2026")],
     "items": [("Office visit", "$140.00"), ("Insurance paid", "-$105.00")], "total": ("Amount paid:", "$35.00"),
     "paid_by": "Card ending 0000", "review": True, "reimbursed": ("2026-08-03", "Claim 2026-11")},
    {"provider": "Lakeside Dental Group", "address": "120 Example Avenue, Springfield", "colour": "#1f5f55", "number": "10117",
     "labels": [("Patient", "Sam Example"), ("Provider", "Lakeside Dental Group"), ("Date of Service", "06/30/2026"), ("Payment Date", "07/02/2026")],
     "items": [("Filling", "$290.00"), ("Insurance paid", "-$80.00")], "total": ("Amount paid:", "$210.00"), "paid_by": "Card ending 0000", "review": True},
    {"provider": "Maple Street Pharmacy", "address": "8 Maple Street, Springfield", "colour": "#7a3b69", "number": "RX-51002",
     "labels": [("Patient", "Jane Example"), ("Provider", "Maple Street Pharmacy"), ("Payment Date", "05/11/2026")],
     "items": [("Prescription, 30 days", "$8.75")], "total": ("Amount paid:", "$8.75"), "paid_by": "Card ending 0000",
     "review": True, "reimbursed": ("2026-06-01", "Claim 2026-07")},
    {"provider": "Riverside Physiotherapy", "address": "3 Mill Lane, Springfield", "colour": "#8a5a2f", "number": "6920",
     "labels": [("Patient", "Jane Example"), ("Provider", "Riverside Physiotherapy"), ("Date of Service", "11/20/2025"), ("Payment Date", "11/20/2025")],
     "items": [("Physiotherapy session", "$90.00")], "total": ("Amount paid:", "$90.00"), "paid_by": "Card ending 0000",
     "review": True, "reimbursed": ("2025-12-15", "Claim 2025-22")},
    {"provider": "Brightview Eye Care", "address": "17 Hill Street, Springfield", "colour": "#3c6e47", "number": "E-2875",
     "labels": [("Patient", "Sam Example"), ("Provider", "Brightview Eye Care"), ("Date of Service", "12/02/2025"), ("Payment Date", "12/02/2025")],
     "items": [("Contact lens fitting", "$60.00")], "total": ("Amount paid:", "$60.00"), "paid_by": "Card ending 0000", "review": True},
]

BOILER_MANUAL = ["Normal pressure:", "1 to 1.5 bar on the gauge when the heating is cold.", "",
                 "Topping up:", "Open the filling loop slowly until the gauge reads 1.5 bar,", "then close it again.", "",
                 "Fault E119:", "Low pressure. Top up, then hold Reset for 3 seconds.", "",
                 "Servicing:", "Once a year, by a registered engineer."]
SCHOOL_LETTER = ["Dear parents and carers,", "", "Parents' evening for Year 4 is on Thursday 15 October,",
                 "from 4pm to 7pm in the main hall. Book a ten-minute slot", "at the school office or reply to this letter.", "",
                 "The autumn trip to the science museum is on 22 October.", "Please return the consent form by 9 October.", "",
                 "With best wishes,", "Example Primary School"]

NOTES = [
    {"title": "Paint colours for the hall", "tags": ["home", "decorating"],
     "files": [("hall-swatches.png", "picture")],
     "body": "Sample pots from the shop on Saturday. Tried them on the wall by the stairs.\n\n"
             "{hall-swatches.png}\n\n"
             "- **Sage 214** on the walls, two coats\n- **Chalk white** on the ceiling and the trim\n"
             "- Front door in **Harbour blue**, if there is any left\n\n"
             "About 3 litres for the walls. Order before the 20th for the weekend."},
    {"title": "Boiler and radiators", "tags": ["home"], "files": [("boiler-manual.pdf", "file")],
     "body": "## Boiler\n\nPressure should be **1.5 bar** when the heating is cold. Below 1 bar it stops and shows E119.\n\n"
             "Serviced every October; the engineer's number is on the side of the boiler.\n\n"
             "## Radiators\n\nBleed them each October, upstairs first. The key is in the kitchen drawer.\n\n"
             "| Room | Valve |\n|---|---|\n| Hall | 3 |\n| Kitchen | 4 |\n| Bedrooms | 2 |\n\n{boiler-manual.pdf}"},
    {"title": "Car service history", "tags": ["car"],
     "body": "| Date | Miles | What was done |\n|---|---|---|\n| 2024-10-12 | 18,400 | Full service |\n"
             "| 2025-10-04 | 26,950 | Full service, front tyres |\n| 2026-04-18 | 31,200 | Brake pads |\n\n"
             "Next full service due in October. Tyre pressure 36 psi front, 33 rear."},
    {"title": "School term dates 2026-27", "tags": ["family", "school"],
     "body": "- Autumn term starts **Wednesday 2 September**\n- Half term: 26 to 30 October\n- Last day before the holidays: 18 December\n"
             "- Spring term starts 5 January\n\nInset days still to be confirmed."},
    {"title": "Letter from the school", "tags": ["family", "school"], "files": [("school-letter.pdf", "file")],
     "body": "Scanned when it came home in the book bag.\n\n{school-letter.pdf}"},
    {"title": "Passport renewal checklist", "tags": ["travel"],
     "body": "1. New photo, plain background, no glasses\n2. Old passport goes in with the form\n"
             "3. Countersignature not needed for a renewal\n4. Allow three weeks before travelling\n\nApplied online; reference on the email."},
    {"title": "Lentil soup", "tags": ["recipes"],
     "body": "Serves four.\n\n- 1 onion, 2 carrots, 2 sticks of celery\n- 250 g red lentils\n- 1.5 litres of stock\n- Cumin, smoked paprika, a lemon\n\n"
             "Soften the vegetables for ten minutes, add the spices, then the lentils and stock. Simmer 25 minutes. Lemon at the end."},
    {"title": "Wi-Fi and router", "tags": ["home"],
     "body": "Network: **Example-Home**\n\nThe password is on the sticker under the router; it was never changed.\n\n"
             "If it drops: unplug the router for 30 seconds. The fibre box in the hall should show four green lights."},
]


def attachment_bytes(name: str) -> tuple[bytes, str]:
    if name == "hall-swatches.png":
        return swatches_png(), "image/png"
    if name == "boiler-manual.pdf":
        return letter_pdf("Example Combi 30 - Quick guide", BOILER_MANUAL), "application/pdf"
    return letter_pdf("Example Primary School", SCHOOL_LETTER), "application/pdf"


# --- talking to Kaal


class Kaal:
    """Just enough of Kaal's API to fill a vault, with a token."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, *, json_body: object = None, file: tuple[str, bytes, str] | None = None, fields: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        data = None
        if file is not None:
            boundary = "kaal-demo-" + uuid.uuid4().hex
            parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode() for name, value in (fields or {}).items()]
            name, content, kind = file
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {kind}\r\n\r\n'.encode() + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            told = error.read().decode(errors="replace")[:300]
            raise SystemExit(f"Kaal refused {method} {path} ({error.code}): {told}") from None


def already_filled(kaal: Kaal) -> bool:
    """Whether the vault already has the first of the made-up notes."""
    title = NOTES[0]["title"]
    found = kaal.call("GET", "/v1/library/notes?q=" + urllib.parse.quote(title))["result"]
    return any(note.get("title") == title for note in found)


def read_receipt(kaal: Kaal, receipt_id: str, seconds: float = 300, pause: float = 0.5) -> dict:
    """The receipt, once Kaal has read it.

    A Kaal server stores a receipt at once and reads it afterwards, in the
    background, so a receipt that was just added has no amount or date yet and
    cannot be marked reviewed. (The throwaway Kaal that `serve` starts reads as
    it stores, and answers the first time.)
    """
    deadline = time.monotonic() + seconds
    while True:
        receipt = kaal.call("GET", f"/v1/medical/receipts/{receipt_id}")["result"]["receipt"]
        if receipt.get("status") != "extracting":
            return receipt
        if time.monotonic() >= deadline:
            raise SystemExit(f"Kaal had not read a receipt after {seconds:.0f} seconds. Look at the server's log, empty the vault "
                             "(or pass --again) and fill it again.")
        time.sleep(pause)


def fill(kaal: Kaal, again: bool = False) -> dict:
    """Put the made-up family, notes and receipts into the vault the token opens."""
    if not again and already_filled(kaal):
        raise SystemExit(f"This vault already holds the made-up notes (it has “{NOTES[0]['title']}”), so nothing was added. "
                         "Fill an empty vault, or pass --again to add everything a second time.")
    kaal.call("POST", "/v1/medical/people", json_body={"add": FAMILY})
    for note in NOTES:
        made = kaal.call("POST", "/v1/library/notes", json_body={"title": note["title"], "tags": note["tags"], "body": "Adding files…"})["result"]["note"]
        body = note["body"]
        for name, kind in note.get("files", []):
            content, media = attachment_bytes(name)
            attached = kaal.call("POST", f"/v1/library/notes/{made['id']}/attachments", file=(name, content, media))["result"]["attachment"]
            stem = name.rsplit(".", 1)[0]
            reference = f"![{stem}](attachment:{attached['id']})" if kind == "picture" else f"[{name}](attachment:{attached['id']})"
            body = body.replace("{" + name + "}", reference)
        kaal.call("PUT", f"/v1/library/notes/{made['id']}", json_body={"title": note["title"], "tags": note["tags"], "body": body})
    # Oldest first, so the list sorted by when each changed shows the newest at the top.
    for receipt in reversed(RECEIPTS):
        name = f"{receipt['provider']} {receipt['number']}.pdf"
        made = kaal.call("POST", "/v1/medical/receipts", file=(name, receipt_pdf(receipt), "application/pdf"),
                         fields={"source_url": receipt.get("source", ""), "source_title": f"{receipt['provider']} - receipt"})["result"]
        # Reviewing needs the amount and a date, which are only there once Kaal has read the receipt;
        # and a vault that has just been filled should not still show "Reading…".
        read_receipt(kaal, made["id"])
        if receipt.get("review"):
            kaal.call("POST", f"/v1/medical/receipts/{made['id']}/review")
        if receipt.get("reimbursed"):
            date, what = receipt["reimbursed"]
            kaal.call("POST", "/v1/medical/receipts/reimburse", json_body={"ids": [made["id"]], "date": date, "note": what})
    return {"notes": len(NOTES), "receipts": len(RECEIPTS)}


# --- a throwaway Kaal


def _wait_for(url: str, process: subprocess.Popen, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("The Kaal server stopped before it was ready.")
        try:
            with urllib.request.urlopen(url + "/v1/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("The Kaal server did not start in time.")


def _interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def serve(args: argparse.Namespace) -> int:
    # Being told to stop (a script's `kill`, a closed terminal) is treated as Ctrl-C, from before
    # anything is started: otherwise a stop that arrives while the vault is being filled would
    # end this program and leave its server running, with its key and data still on disk.
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, _interrupt)
    home = None
    server_process = None
    try:
        home = Path(tempfile.mkdtemp(prefix="kaal-demo-"))
        os.chmod(home, 0o700)
        key = home / "master.key"
        key.write_text(base64.b64encode(os.urandom(32)).decode() + "\n", encoding="ascii")
        os.chmod(key, 0o600)
        url = f"http://localhost:{args.port}"
        # Only what this run made: never a real configuration, key, model or vault.
        env = {name: value for name, value in os.environ.items() if not name.startswith("KAAL_")}
        env.update({"KAAL_HOME": str(home), "KAAL_VAULT": str(home / "data"), "KAAL_KEY_PROVIDER": "file", "KAAL_KEY_FILE": str(key),
                    "KAAL_CONFIG": str(home / "no-config.toml"), "KAAL_LOCAL": "1", "KAAL_PUBLIC_URL": url,
                    # The session cookie is normally sent over HTTPS only; this Kaal is plain http on this machine.
                    # Receipts are read as they are stored, so the vault is full as soon as it has been filled.
                    "KAAL_COOKIE_SECURE": "0", "KAAL_BACKGROUND_EXTRACTION": "0", "PYTHONPATH": str(ROOT)})
        os.environ.update(env)
        for name in [name for name in os.environ if name.startswith("KAAL_") and name not in env]:
            del os.environ[name]
        subprocess.run([sys.executable, str(ROOT / "bin" / "kaal.py"), "init"], env=env, check=True, stdout=subprocess.DEVNULL)
        sys.path.insert(0, str(ROOT))
        from kaal import passwords, server, tokens

        settings = server.Settings.from_env(env)
        store = server.open_store(settings)
        person = store.set_login("owner", args.login)
        store.rename(person["id"], args.name)
        vault = store.vaults_of(person["id"])[0]
        store.rename_vault(vault["id"], "Household")
        # Letters only: quick to type on a phone's keyboard, which is how the screenshots sign in.
        password = args.password or "".join(secrets.choice(string.ascii_lowercase) for _ in range(18))
        store.set_password(person["id"], passwords.hash_password(password))
        token_store = tokens.TokenStore(settings.tokens_file)
        filler = token_store.create("demo-data", ["read", "reveal", "write"], account=person["id"], vault=vault["id"])[1]
        extension = token_store.create("chrome-extension", ["read", "reveal", "write"], account=person["id"], vault=vault["id"])[1]

        log = open(home / "server.log", "w")  # noqa: SIM115 - kept open while the server runs
        server_process = subprocess.Popen([sys.executable, "-m", "kaal.server", "serve", "--port", str(args.port)], env=env, cwd=home, stdout=log, stderr=log)
        _wait_for(url, server_process)
        counted = fill(Kaal(url, filler))
        token_store.revoke("demo-data")
        told = {"url": url, "login": args.login, "password": password, "extension_token": extension, **counted}
        if args.info:
            info = Path(args.info)
            info.write_text(json.dumps(told, indent=2) + "\n")
            os.chmod(info, 0o600)
        print(f"Kaal with made-up data is at {url}", flush=True)
        print(f"Sign in as {args.login!r} with the password {password!r}.", flush=True)
        print(f"{counted['notes']} notes and {counted['receipts']} receipts. Ctrl-C stops it and deletes everything.", flush=True)
        while server_process.poll() is None:
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        # Nothing may cut the tidying up short.
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)
        if server_process is not None and server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(10)
            except subprocess.TimeoutExpired:
                server_process.kill()
                server_process.wait()
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


def fill_existing(args: argparse.Namespace) -> int:
    token = Path(args.token_file).read_text().strip()
    counted = fill(Kaal(args.url, token), again=args.again)
    print(f"Added {counted['notes']} made-up notes and {counted['receipts']} made-up receipts.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A Kaal holding only made-up notes and receipts.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("serve", help="Run a throwaway Kaal, filled with made-up data, until Ctrl-C")
    run.add_argument("--port", type=int, default=8831)
    run.add_argument("--login", default="alex", help="The sign-in name (default: alex)")
    run.add_argument("--name", default="Alex Example", help="The name shown for that person")
    run.add_argument("--password", default="", help="Default: a new random one, printed")
    run.add_argument("--info", default="", help="Also write the address, sign-in and a token for the browser extension to this file (mode 600)")
    run.set_defaults(func=serve)
    add = sub.add_parser("fill", help="Add the made-up data to an existing Kaal, through its API")
    add.add_argument("--url", required=True)
    add.add_argument("--token-file", required=True, help="A file holding a token with the read, reveal and write scopes")
    add.add_argument("--again", action="store_true", help="Add everything even if the made-up notes are already there")
    add.set_defaults(func=fill_existing)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
