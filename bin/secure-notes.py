#!/usr/bin/env python3
"""Local encrypted secure notes for sensitive personal data.

Vault default: ~/.secure-notes
Key storage: macOS Keychain
Encryption: AES-256-GCM via Python cryptography when available, otherwise OpenSSL enc/CLI is NOT used.

This script intentionally keeps plaintext out of the vault. Index metadata contains
only ids, titles, tags, timestamps, and attachment filenames.
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VAULT = Path(os.environ.get("SECURE_NOTES_VAULT", str(Path.home() / ".secure-notes"))).expanduser()
NOTES_DIR = VAULT / "notes"
ATTACH_DIR = VAULT / "attachments"
INDEX_PATH = VAULT / "index.json"
SERVICE = "Hermes Secure Notes"
ACCOUNT = "vault-key-v1"
KEY_BYTES = 32

SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)(\d{3})[- ]?(?!00)(\d{2})[- ]?(?!0000)(\d{4})\b")),
    ("CreditCard", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("Passport", re.compile(r"\bpassport\s*(?:number|#|no\.?|:)?\s*([A-Z0-9]{6,9})\b", re.I)),
    ("DriverLicense", re.compile(r"\b(?:driver'?s?\s+license|dl)\s*(?:number|#|no\.?|:)?\s*([A-Z0-9-]{5,20})\b", re.I)),
    ("TaxId", re.compile(r"\b(?:ein|itin|tax\s*id)\b\s*(?:number|#|no\.?|:)?\s*([0-9A-Z-]{5,20})\b", re.I)),
]
SENSITIVE_THRESHOLD = 5
TEXT_ATTACHMENT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm"}
IMAGE_ATTACHMENT_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
PDF_ATTACHMENT_SUFFIXES = {".pdf"}

CLASSIFIER_RULES: list[tuple[str, int, re.Pattern[str]]] = [
    ("SSN", 8, SENSITIVE_PATTERNS[0][1]),
    ("Passport", 6, SENSITIVE_PATTERNS[2][1]),
    ("DriverLicense", 6, SENSITIVE_PATTERNS[3][1]),
    ("TaxId", 6, SENSITIVE_PATTERNS[4][1]),
    ("DOB", 4, re.compile(r"\b(?:dob|date of birth|birthdate)\s*[:=]\s*\d{1,4}[-/ ]\d{1,2}[-/ ]\d{1,4}\b", re.I)),
    ("BankRouting", 6, re.compile(r"\b(?:routing|aba)\s*(?:number|#|no\.?|:)?\s*\d{9}\b", re.I)),
    ("BankAccount", 5, re.compile(r"\b(?:account|acct)\s*(?:number|#|no\.?|:)?\s*\d{6,17}\b", re.I)),
    ("Credential", 7, re.compile(r"\b(?:password|passcode|api[_ -]?key|secret|recovery code|private key|access token|refresh token)\b", re.I)),
    ("TaxDocument", 5, re.compile(r"\b(?:w-?2|1099|irs|tax return|agi|form 1040)\b", re.I)),
    ("Medical", 4, re.compile(r"\b(?:medical record|patient id|health insurance|member id|medicare|medicaid)\b", re.I)),
    ("Email", 1, re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("Phone", 1, re.compile(r"(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}")),
    ("AddressHint", 2, re.compile(r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+\s+(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|court|ct|blvd|boulevard)\b", re.I)),
]


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_macos_keychain() -> None:
    if not shutil.which("security"):
        die("macOS security CLI not found; this tool currently requires macOS Keychain.")


def run_security(args: list[str], *, input_text: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["security", *args], input=input_text, text=True, capture_output=True, check=check)


def get_key(create: bool = False) -> bytes:
    ensure_macos_keychain()
    proc = run_security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"])
    if proc.returncode == 0:
        try:
            key = base64.b64decode(proc.stdout.strip(), validate=True)
        except Exception as e:
            die(f"Keychain item exists but is not a valid key: {e}")
        if len(key) != KEY_BYTES:
            die(f"Keychain key has wrong size ({len(key)} bytes); expected {KEY_BYTES}.")
        return key
    if not create:
        die("Vault key not found. Run: secure-notes init")
    key = secrets.token_bytes(KEY_BYTES)
    encoded = base64.b64encode(key).decode()
    add = run_security([
        "add-generic-password",
        "-U",
        "-s", SERVICE,
        "-a", ACCOUNT,
        "-w", encoded,
        "-T", "/usr/bin/security",
        "-T", sys.executable,
    ])
    if add.returncode != 0:
        die(f"Could not store key in Keychain: {add.stderr.strip() or add.stdout.strip()}")
    return key


def import_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
        return AESGCM
    except Exception as e:
        die(
            "Python package 'cryptography' is required. Install with: "
            "uv pip install --system cryptography  (or run with a venv that has cryptography). "
            f"Import error: {e}"
        )


def encrypt_bytes(data: bytes, *, aad: bytes = b"") -> dict[str, str]:
    AESGCM = import_crypto()
    key = get_key(create=False)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, data, aad)
    return {
        "version": "secure-notes-v1",
        "alg": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode(),
        "aad": base64.b64encode(aad).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    }


def decrypt_payload(payload: dict[str, str], *, aad: bytes = b"") -> bytes:
    if payload.get("alg") != "AES-256-GCM":
        die(f"Unsupported encryption algorithm: {payload.get('alg')}")
    AESGCM = import_crypto()
    key = get_key(create=False)
    nonce = base64.b64decode(payload["nonce"])
    ct = base64.b64decode(payload["ciphertext"])
    stored_aad = base64.b64decode(payload.get("aad", ""))
    if stored_aad != aad:
        die("Encrypted payload metadata mismatch; refusing to decrypt.")
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except Exception as e:
        die(f"Decryption failed: {e}")


def secure_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return {"version": 1, "created": now_iso(), "notes": []}
    try:
        return json.loads(INDEX_PATH.read_text())
    except Exception as e:
        die(f"Could not read index: {e}")


def save_index(index: dict[str, Any]) -> None:
    index["updated"] = now_iso()
    secure_write_json(INDEX_PATH, index)


def init_vault(args: argparse.Namespace) -> None:
    VAULT.mkdir(mode=0o700, parents=True, exist_ok=True)
    NOTES_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    ATTACH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(VAULT, 0o700)
    os.chmod(NOTES_DIR, 0o700)
    os.chmod(ATTACH_DIR, 0o700)
    key = get_key(create=True)
    if not INDEX_PATH.exists():
        save_index({"version": 1, "created": now_iso(), "notes": []})
    print(json.dumps({"status": "initialized", "vault": str(VAULT), "keychain_service": SERVICE, "key_sha256_prefix": hashlib.sha256(key).hexdigest()[:12]}, indent=2))


def parse_tags(raw: str) -> list[str]:
    return sorted({t.strip().lstrip("#") for t in raw.split(",") if t.strip()})


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def classify_text(text: str, *, context: str = "") -> dict[str, Any]:
    """Best-effort local sensitivity classifier.

    False negatives are the dangerous case, so callers should encrypt when the
    result is sensitive or when extraction/classification is uncertain.
    """
    haystack = f"{context}\n{text}"
    # Joplin embeds 32-hex resource ids in Markdown links (:/<id>). Those ids
    # can look like long payment-card numbers to the Luhn heuristic, but they
    # are opaque local attachment references and should not make a note private.
    haystack = JOPLIN_RESOURCE_LINK_RE.sub(":/[joplin-resource]", haystack)
    reasons: list[str] = []
    score = 0
    for name, weight, pattern in CLASSIFIER_RULES:
        if pattern.search(haystack):
            score += weight
            reasons.append(f"matched {name}")
    for match in SENSITIVE_PATTERNS[1][1].finditer(haystack):
        candidate = match.group(0)
        if luhn_valid(candidate):
            score += 7
            reasons.append("matched CreditCard with valid Luhn checksum")
            break
    sensitivity = "sensitive" if score >= SENSITIVE_THRESHOLD else "public"
    return {"sensitivity": sensitivity, "score": score, "threshold": SENSITIVE_THRESHOLD, "reasons": reasons}


def decide_storage(text: str, *, sensitivity: str = "auto", context: str = "") -> tuple[str, dict[str, Any]]:
    requested = (sensitivity or "auto").lower().strip()
    if requested in {"sensitive", "encrypted", "encrypt"}:
        result = classify_text(text, context=context)
        result["sensitivity"] = "sensitive"
        result["reasons"] = ["forced sensitive"] + result.get("reasons", [])
        return "encrypted", result
    if requested in {"public", "plaintext", "plain"}:
        result = classify_text(text, context=context)
        result["sensitivity"] = "public"
        result["reasons"] = ["forced public"] + result.get("reasons", [])
        return "plaintext", result
    if requested != "auto":
        die("sensitivity must be one of: auto, sensitive, public")
    result = classify_text(text, context=context)
    return ("encrypted" if result["sensitivity"] == "sensitive" else "plaintext"), result


def safe_read_text(path: Path, limit: int = 1_000_000) -> str:
    data = path.read_bytes()[:limit]
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_markitdown(path: Path) -> str:
    try:
        from markitdown import MarkItDown  # type: ignore
        result = MarkItDown().convert(str(path))
        return getattr(result, "text_content", "") or ""
    except Exception:
        cli = shutil.which("markitdown")
        if not cli:
            return ""
        proc = subprocess.run([cli, str(path)], text=True, capture_output=True)
        return proc.stdout if proc.returncode == 0 else ""


def run_tesseract(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    proc = subprocess.run([tesseract, str(path), "stdout"], text=True, capture_output=True)
    return proc.stdout if proc.returncode == 0 else ""


def extract_attachment_markdown(path: Path, *, ocr: bool = False, extract: bool = True) -> str:
    if not extract and not ocr:
        return ""
    suffix = path.suffix.lower()
    text = ""
    method = ""
    if extract and suffix in TEXT_ATTACHMENT_SUFFIXES:
        text = safe_read_text(path)
        method = "text"
    elif extract and suffix in PDF_ATTACHMENT_SUFFIXES:
        text = run_markitdown(path)
        method = "markitdown"
    elif ocr and suffix in IMAGE_ATTACHMENT_SUFFIXES:
        text = run_tesseract(path)
        method = "tesseract"
    if not text.strip():
        return ""
    return f"# Extracted text: {path.name}\n\nMethod: {method}\n\n```text\n{text.strip()}\n```\n"


def read_stdin_or_arg(value: str | None, body_file: str | None = None) -> str:
    if body_file:
        return safe_read_text(Path(body_file).expanduser().resolve())
    if value is not None:
        return value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Enter note body. Finish with Ctrl-D:", file=sys.stderr)
    return sys.stdin.read()


def note_path(note_id: str) -> Path:
    md = NOTES_DIR / f"{note_id}.md"
    if md.exists():
        return md
    return NOTES_DIR / f"{note_id}.json"


def encrypted_note_path(note_id: str) -> Path:
    return NOTES_DIR / f"{note_id}.json"


def plaintext_note_path(note_id: str) -> Path:
    return NOTES_DIR / f"{note_id}.md"


def attachment_note_dir(note_id: str) -> Path:
    return ATTACH_DIR / note_id


def find_notes(query: str) -> list[dict[str, Any]]:
    index = load_index()
    notes = index.get("notes", [])
    q = query.lower()
    matches = [n for n in notes if n.get("id") == query or q in n.get("title", "").lower()]
    if len(matches) == 1:
        return matches
    exact_title = [n for n in notes if n.get("title", "").lower() == q]
    return exact_title or matches


def require_one_note(query: str) -> dict[str, Any]:
    matches = find_notes(query)
    if not matches:
        die(f"No note found matching: {query}")
    if len(matches) > 1:
        print("Multiple notes match:", file=sys.stderr)
        for n in matches:
            print(f"  {n['id']}  {n.get('title','')}", file=sys.stderr)
        die("Use the note id to disambiguate.")
    return matches[0]


def redact_value(match: re.Match[str]) -> str:
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return f"[REDACTED ending {digits[-4:]}]"
    return "[REDACTED]"


def redact_text(text: str) -> str:
    redacted = text
    for _name, pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub(redact_value, redacted)
    # Common key/value sensitive fields: leave field name, hide value.
    redacted = re.sub(
        r"(?im)^(\s*(?:ssn|social security|driver'?s? license|license number|passport|ein|itin|tax id|account(?: number)?|routing(?: number)?|dob|date of birth)\s*[:=]\s*)(.+)$",
        lambda m: m.group(1) + "[REDACTED]",
        redacted,
    )
    return redacted


def create_note(title: str, body: str, *, tags: list[str] | None = None, sensitivity: str = "auto") -> dict[str, Any]:
    if not body.strip():
        die("Refusing to create an empty note.")
    note_id = secrets.token_hex(6)
    ts = now_iso()
    note_tags = sorted(set(tags or []))
    storage, classification = decide_storage(body, sensitivity=sensitivity, context=title)
    note_obj = {
        "id": note_id,
        "title": title,
        "tags": note_tags,
        "created": ts,
        "updated": ts,
        "body": body,
        "format": "markdown",
        "sensitivity": classification["sensitivity"],
        "storage": storage,
        "classification": classification,
    }
    if storage == "encrypted":
        payload = encrypt_bytes(json.dumps(note_obj, ensure_ascii=False).encode(), aad=note_id.encode())
        secure_write_json(encrypted_note_path(note_id), payload)
    else:
        path = plaintext_note_path(note_id)
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o600)
    meta = {
        "id": note_id,
        "title": title,
        "tags": note_tags,
        "created": ts,
        "updated": ts,
        "attachments": [],
        "format": "markdown",
        "sensitivity": classification["sensitivity"],
        "storage": storage,
        "classification": classification,
    }
    index = load_index()
    index.setdefault("notes", []).append(meta)
    save_index(index)
    return meta


def add_note(args: argparse.Namespace) -> None:
    init_if_needed()
    body = read_stdin_or_arg(args.body, getattr(args, "body_file", None))
    meta = create_note(args.title, body, tags=parse_tags(args.tags), sensitivity=getattr(args, "sensitivity", "auto"))
    print(json.dumps({
        "status": "created",
        "id": meta["id"],
        "title": meta["title"],
        "tags": meta["tags"],
        "format": "markdown",
        "sensitivity": meta["sensitivity"],
        "storage": meta["storage"],
        "classification_reasons": meta.get("classification", {}).get("reasons", []),
    }, indent=2, ensure_ascii=False))


def init_if_needed() -> None:
    if not VAULT.exists() or not INDEX_PATH.exists():
        die("Vault not initialized. Run: secure-notes init")


def load_note(note_meta: dict[str, Any]) -> dict[str, Any]:
    nid = note_meta["id"]
    storage = note_meta.get("storage", "encrypted")
    if storage == "plaintext":
        path = plaintext_note_path(nid)
        if not path.exists():
            die(f"Plaintext note file missing: {path}")
        return {
            "id": nid,
            "title": note_meta.get("title", ""),
            "tags": note_meta.get("tags", []),
            "created": note_meta.get("created", ""),
            "updated": note_meta.get("updated", ""),
            "body": path.read_text(encoding="utf-8"),
            "format": note_meta.get("format", "markdown"),
            "sensitivity": note_meta.get("sensitivity", "public"),
            "storage": "plaintext",
        }
    path = encrypted_note_path(nid)
    if not path.exists():
        die(f"Encrypted note file missing: {path}")
    payload = json.loads(path.read_text())
    return json.loads(decrypt_payload(payload, aad=nid.encode()).decode())


def list_notes(args: argparse.Namespace) -> None:
    init_if_needed()
    notes = load_index().get("notes", [])
    if args.tag:
        notes = [n for n in notes if args.tag.lstrip("#") in n.get("tags", [])]
    if args.json:
        print(json.dumps(notes, indent=2, ensure_ascii=False))
        return
    for n in notes:
        tags = " ".join(f"#{t}" for t in n.get("tags", []))
        att = len(n.get("attachments", []))
        print(f"{n['id']}  {n.get('updated','')}  {n.get('title','')}  {tags}  attachments:{att}")


def show_note(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    note = load_note(meta)
    body = note.get("body", "")
    if not args.full:
        body = redact_text(body)
    if args.json:
        out = {**{k: note.get(k) for k in ["id", "title", "tags", "created", "updated"]}, "body": body, "redacted": not args.full, "attachments": meta.get("attachments", [])}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"# {note.get('title','')} ({note.get('id','')})")
        if note.get("tags"):
            print("Tags: " + ", ".join(note["tags"]))
        if meta.get("attachments"):
            print("Attachments: " + ", ".join(a.get("name", "") for a in meta.get("attachments", [])))
        print()
        print(body)
        if not args.full:
            print("\n[redacted output; use --full only when you explicitly need plaintext]", file=sys.stderr)


def search_notes(args: argparse.Namespace) -> None:
    init_if_needed()
    q = args.query.lower()
    notes = load_index().get("notes", [])
    results = [n for n in notes if q in n.get("title", "").lower() or any(q in t.lower() for t in n.get("tags", []))]
    print(json.dumps(results, indent=2, ensure_ascii=False))


def append_note(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    note = load_note(meta)
    extra = read_stdin_or_arg(args.body)
    if not extra.strip():
        die("No append text provided.")
    note["body"] = note.get("body", "").rstrip() + "\n\n" + extra.strip() + "\n"
    note["updated"] = now_iso()
    # Re-classify appended content in auto mode. A note that was previously public
    # must be upgraded if the appended text introduces sensitive data.
    storage, classification = decide_storage(note["body"], sensitivity="auto", context=note.get("title", ""))
    # Once encrypted, do not downgrade automatically on append.
    if meta.get("storage") == "encrypted":
        storage = "encrypted"
        classification["sensitivity"] = "sensitive"
    note["storage"] = storage
    note["sensitivity"] = classification["sensitivity"]
    note["classification"] = classification
    if storage == "encrypted":
        old_plain = plaintext_note_path(note["id"])
        if old_plain.exists():
            old_plain.unlink()
        payload = encrypt_bytes(json.dumps(note, ensure_ascii=False).encode(), aad=note["id"].encode())
        secure_write_json(encrypted_note_path(note["id"]), payload)
    else:
        plaintext_note_path(note["id"]).write_text(note["body"], encoding="utf-8")
        os.chmod(plaintext_note_path(note["id"]), 0o600)
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == note["id"]:
            n["updated"] = note["updated"]
            n["storage"] = storage
            n["sensitivity"] = classification["sensitivity"]
            n["classification"] = classification
    save_index(index)
    print(json.dumps({"status": "appended", "id": note["id"], "title": note.get("title", ""), "storage": storage, "sensitivity": classification["sensitivity"]}, indent=2, ensure_ascii=False))


def extract_field(body: str, field: str) -> str:
    names = [re.escape(field)]
    aliases = {
        "ssn": ["ssn", "social security", "social security number"],
        "drivers_license": ["driver license", "drivers license", "driver's license", "dl", "license number"],
        "driver_license": ["driver license", "drivers license", "driver's license", "dl", "license number"],
        "passport": ["passport", "passport number"],
        "dob": ["dob", "date of birth", "birthdate"],
    }.get(field.lower().replace("-", "_"))
    if aliases:
        names = [re.escape(a) for a in aliases]
    pat = re.compile(rf"(?im)^\s*(?:{'|'.join(names)})\s*(?:number|#|no\.)?\s*[:=]\s*(.+?)\s*$")
    m = pat.search(body)
    if not m:
        die(f"Could not find field '{field}' in note. Use exact 'field: value' lines for copy support.")
    return m.group(1).strip()


def copy_field(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    note = load_note(meta)
    value = extract_field(note.get("body", ""), args.field)
    pbcopy = shutil.which("pbcopy")
    if not pbcopy:
        die("pbcopy not found; cannot copy to clipboard.")
    subprocess.run([pbcopy], input=value, text=True, check=True)
    print(json.dumps({"status": "copied", "note": note.get("id"), "field": args.field, "characters": len(value)}, indent=2))


def attach_file_to_note(meta: dict[str, Any], src: Path, *, sensitivity: str = "auto", extract: bool = False, ocr: bool = False, attachment_name: str | None = None) -> dict[str, Any]:
    data = src.read_bytes()
    stored_name = attachment_name or src.name
    nid = meta["id"]
    att_id = secrets.token_hex(6)
    extracted_md = extract_attachment_markdown(src, ocr=ocr, extract=extract)
    storage, classification = decide_storage(extracted_md, sensitivity=sensitivity, context=f"{stored_name}\n{meta.get('title','')}")
    # If the parent note is encrypted/sensitive, attachments inherit encryption.
    if meta.get("storage") == "encrypted" or meta.get("sensitivity") == "sensitive":
        storage = "encrypted"
        classification["sensitivity"] = "sensitive"
        classification.setdefault("reasons", []).insert(0, "parent note is sensitive")
    out_dir = attachment_note_dir(nid) / att_id
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    attachment_meta = {
        "id": att_id,
        "name": stored_name,
        "size": len(data),
        "created": now_iso(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage": storage,
        "sensitivity": classification["sensitivity"],
        "classification": classification,
        "extracted_markdown": bool(extracted_md),
    }
    if storage == "encrypted":
        aad = f"{nid}:{att_id}:{stored_name}".encode()
        payload = encrypt_bytes(data, aad=aad)
        secure_write_json(out_dir / "original.json", {"name": stored_name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encrypted": payload})
        if extracted_md:
            sidecar_payload = encrypt_bytes(extracted_md.encode(), aad=f"{nid}:{att_id}:extracted.md".encode())
            secure_write_json(out_dir / "extracted.md.json", {"name": "extracted.md", "encrypted": sidecar_payload})
    else:
        original = out_dir / stored_name
        original.write_bytes(data)
        os.chmod(original, 0o600)
        if extracted_md:
            sidecar = out_dir / "extracted.md"
            sidecar.write_text(extracted_md, encoding="utf-8")
            os.chmod(sidecar, 0o600)
    secure_write_json(out_dir / "metadata.json", attachment_meta)
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == nid:
            n.setdefault("attachments", []).append(attachment_meta)
            n["updated"] = now_iso()
            meta = n
    save_index(index)
    return attachment_meta


def attach_file(args: argparse.Namespace) -> None:
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"Attachment file not found: {src}")
    meta = require_one_note(args.query)
    attachment_meta = attach_file_to_note(
        meta,
        src,
        sensitivity=getattr(args, "sensitivity", "auto"),
        extract=getattr(args, "extract", False),
        ocr=getattr(args, "ocr", False),
    )
    print(json.dumps({
        "status": "attached",
        "note": meta["id"],
        "attachmentId": attachment_meta["id"],
        "name": attachment_meta["name"],
        "size": attachment_meta["size"],
        "storage": attachment_meta["storage"],
        "sensitivity": attachment_meta["sensitivity"],
        "extracted_markdown": attachment_meta["extracted_markdown"],
        "classification_reasons": attachment_meta.get("classification", {}).get("reasons", []),
    }, indent=2, ensure_ascii=False))


def export_attachment(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    nid = meta["id"]
    attachments = meta.get("attachments", [])
    matches = [a for a in attachments if a.get("id") == args.attachment or args.attachment.lower() in a.get("name", "").lower()]
    if not matches:
        die(f"No attachment matching: {args.attachment}")
    if len(matches) > 1:
        die("Multiple attachments match; use attachment id.")
    att = matches[0]
    att_dir = attachment_note_dir(nid) / att["id"]
    if att.get("storage", "encrypted") == "plaintext":
        data_name = att["name"]
        plaintext = (att_dir / data_name).read_bytes()
    else:
        payload_path = att_dir / "original.json"
        if not payload_path.exists():
            # Backward compatibility with the old single-json attachment layout.
            payload_path = attachment_note_dir(nid) / f"{att['id']}.json"
        data = json.loads(payload_path.read_text())
        data_name = data["name"]
        aad = f"{nid}:{att['id']}:{data_name}".encode()
        plaintext = decrypt_payload(data["encrypted"], aad=aad)
    out = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / data_name
    if out.exists() and not args.force:
        die(f"Output exists: {out}; pass --force to overwrite.")
    out.write_bytes(plaintext)
    os.chmod(out, 0o600)
    print(json.dumps({"status": "exported", "path": str(out), "bytes": len(plaintext), "sha256": hashlib.sha256(plaintext).hexdigest()}, indent=2))


def delete_note(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    if not args.yes:
        die("Deletion requires --yes.")
    nid = meta["id"]
    note_path(nid).unlink(missing_ok=True)
    if attachment_note_dir(nid).exists():
        shutil.rmtree(attachment_note_dir(nid))
    index = load_index()
    index["notes"] = [n for n in index.get("notes", []) if n.get("id") != nid]
    save_index(index)
    print(json.dumps({"status": "deleted", "id": nid}, indent=2))


def status(args: argparse.Namespace) -> None:
    has_key = run_security(["find-generic-password", "-s", SERVICE, "-a", ACCOUNT, "-w"]).returncode == 0 if shutil.which("security") else False
    print(json.dumps({
        "vault": str(VAULT),
        "vault_exists": VAULT.exists(),
        "index_exists": INDEX_PATH.exists(),
        "keychain_key_exists": has_key,
        "notes": len(load_index().get("notes", [])) if INDEX_PATH.exists() else 0,
    }, indent=2))


def classify_command(args: argparse.Namespace) -> None:
    if args.file:
        text = safe_read_text(Path(args.file).expanduser().resolve())
        context = Path(args.file).name
    else:
        text = read_stdin_or_arg(None)
        context = ""
    print(json.dumps(classify_text(text, context=context), indent=2, ensure_ascii=False))


JOPLIN_RESOURCE_LINK_RE = re.compile(r":/([0-9a-fA-F]{32})")
JOPLIN_PROP_RE = re.compile(r"^([A-Za-z0-9_]+):\s?(.*)$")


def parse_joplin_raw_item(text: str) -> dict[str, Any]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    props: dict[str, str] = {}
    i = len(lines) - 1
    while i >= 0:
        match = JOPLIN_PROP_RE.match(lines[i])
        if not match:
            break
        props[match.group(1)] = match.group(2)
        i -= 1
    if i >= 0 and lines[i] == "" and props:
        content_lines = lines[:i]
    elif props:
        content_lines = lines[: i + 1]
    else:
        content_lines = lines
    title = content_lines[0].strip() if content_lines else ""
    body_lines = content_lines[1:]
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    return {"title": title, "body": "\n".join(body_lines).strip("\n"), "props": props}


def slug_tag(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "untitled"


def joplin_notebook_path(folder_id: str, folders: dict[str, dict[str, Any]]) -> list[str]:
    path: list[str] = []
    seen: set[str] = set()
    current = folder_id
    while current and current in folders and current not in seen:
        seen.add(current)
        folder = folders[current]
        if folder.get("title"):
            path.append(folder["title"])
        current = folder.get("props", {}).get("parent_id", "")
    return list(reversed(path))


def joplin_resource_blob(export_dir: Path, resource: dict[str, Any]) -> Path | None:
    rid = resource.get("props", {}).get("id", "")
    ext = resource.get("props", {}).get("file_extension", "").strip().lstrip(".")
    resources_dir = export_dir / "resources"
    candidates = []
    if ext:
        candidates.append(resources_dir / f"{rid}.{ext}")
    candidates.extend(resources_dir.glob(f"{rid}.*") if resources_dir.exists() else [])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_joplin_raw_export(export_dir: Path) -> dict[str, Any]:
    if not export_dir.exists() or not export_dir.is_dir():
        die(f"Joplin RAW export directory not found: {export_dir}")
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in sorted(p for p in export_dir.glob("*.md") if p.is_file()):
        try:
            item = parse_joplin_raw_item(safe_read_text(path))
        except Exception as e:
            warnings.append(f"could not parse {path.name}: {e}")
            continue
        item["path"] = str(path)
        props = item.get("props", {})
        if not props.get("id"):
            warnings.append(f"skipping {path.name}: missing Joplin id")
            continue
        items.append(item)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_type.setdefault(item.get("props", {}).get("type_", ""), []).append(item)
    folders = {i["props"]["id"]: i for i in by_type.get("2", [])}
    notes = by_type.get("1", [])
    resources = {i["props"]["id"]: i for i in by_type.get("4", [])}
    tags = {i["props"]["id"]: i for i in by_type.get("5", [])}
    note_tags: dict[str, list[str]] = {}
    for rel in by_type.get("6", []):
        note_id = rel.get("props", {}).get("note_id", "")
        tag_id = rel.get("props", {}).get("tag_id", "")
        if note_id and tag_id in tags:
            note_tags.setdefault(note_id, []).append(tags[tag_id].get("title", tag_id))
    referenced: dict[str, list[str]] = {}
    unresolved = 0
    for note in notes:
        note_id = note["props"]["id"]
        ids = sorted(set(JOPLIN_RESOURCE_LINK_RE.findall(note.get("body", ""))))
        referenced[note_id] = ids
        for rid in ids:
            blob = joplin_resource_blob(export_dir, resources[rid]) if rid in resources else None
            if not blob:
                unresolved += 1
                warnings.append(f"missing resource {rid} referenced by note {note_id}")
    likely_sensitive = sum(1 for note in notes if classify_text(note.get("body", ""), context=note.get("title", ""))["sensitivity"] == "sensitive")
    return {
        "notes": notes,
        "folders": folders,
        "resources": resources,
        "tags": tags,
        "note_tags": note_tags,
        "referenced": referenced,
        "unresolved_resources": unresolved,
        "likely_sensitive_notes": likely_sensitive,
        "warnings": warnings,
    }


def import_joplin_raw(args: argparse.Namespace) -> None:
    init_if_needed()
    export_dir = Path(args.path).expanduser().resolve()
    parsed = load_joplin_raw_export(export_dir)
    referenced_count = sum(len(ids) for ids in parsed["referenced"].values())
    summary = {
        "status": "dry-run" if args.dry_run else "imported",
        "notes": len(parsed["notes"]),
        "folders": len(parsed["folders"]),
        "resources": len(parsed["resources"]),
        "tags": len(parsed["tags"]),
        "referenced_resources": referenced_count,
        "unresolved_resources": parsed["unresolved_resources"],
        "likely_sensitive_notes": parsed["likely_sensitive_notes"],
        "warnings": parsed["warnings"],
    }
    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    imported_notes = 0
    imported_attachments = 0
    extra_tags = parse_tags(getattr(args, "tags", ""))
    for note in parsed["notes"]:
        props = note["props"]
        note_id = props["id"]
        notebook_path = joplin_notebook_path(props.get("parent_id", ""), parsed["folders"])
        tags = {"imported", "joplin", *extra_tags}
        tags.update(slug_tag(t) for t in parsed["note_tags"].get(note_id, []))
        tags.update(f"joplin-notebook-{slug_tag(part)}" for part in notebook_path)
        body = note.get("body", "") or "<!-- Imported empty Joplin note body -->\n"
        meta = create_note(note.get("title") or "Untitled Joplin Note", body, tags=sorted(tags), sensitivity=getattr(args, "sensitivity", "auto"))
        imported_notes += 1
        for rid in parsed["referenced"].get(note_id, []):
            resource = parsed["resources"].get(rid)
            blob = joplin_resource_blob(export_dir, resource) if resource else None
            if not blob:
                continue
            attach_file_to_note(
                meta,
                blob,
                sensitivity=getattr(args, "sensitivity", "auto"),
                extract=getattr(args, "extract", False),
                ocr=getattr(args, "ocr", False),
                attachment_name=resource.get("title") or blob.name,
            )
            imported_attachments += 1
    summary["imported_notes"] = imported_notes
    summary["imported_attachments"] = imported_attachments
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    prog = os.environ.get("KAAL_CLI_NAME", Path(sys.argv[0]).name)
    description = "Kaal: tiny local encrypted notes vault for sensitive data" if prog == "kaal" else "Tiny local encrypted notes vault for sensitive data"
    p = argparse.ArgumentParser(prog=prog, description=description)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Initialize vault and Keychain key")
    sp.set_defaults(func=init_vault)

    sp = sub.add_parser("status", help="Show vault status")
    sp.set_defaults(func=status)

    sp = sub.add_parser("add", help="Add Markdown note; auto-encrypts when sensitive")
    sp.add_argument("--title", required=True)
    sp.add_argument("--tags", default="", help="Comma-separated tags")
    sp.add_argument("--body", help="Note body; omit to read stdin")
    sp.add_argument("--body-file", help="Markdown/text file to use as the note body")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="Classification override; default auto")
    sp.set_defaults(func=add_note)

    sp = sub.add_parser("classify", help="Classify text as public or sensitive without saving")
    sp.add_argument("file", nargs="?", help="File to classify; omit to read stdin")
    sp.set_defaults(func=classify_command)

    sp = sub.add_parser("list", help="List note metadata only")
    sp.add_argument("--tag", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=list_notes)

    sp = sub.add_parser("search", help="Search titles/tags only")
    sp.add_argument("query")
    sp.set_defaults(func=search_notes)

    sp = sub.add_parser("show", help="Show note; redacted by default")
    sp.add_argument("query", help="Note id or title substring")
    sp.add_argument("--full", action="store_true", help="Print full plaintext to terminal/chat")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=show_note)

    sp = sub.add_parser("append", help="Append encrypted note content")
    sp.add_argument("query")
    sp.add_argument("--body", help="Text to append; omit to read stdin")
    sp.set_defaults(func=append_note)

    sp = sub.add_parser("copy", help="Copy a field value from a note to clipboard without printing it")
    sp.add_argument("query")
    sp.add_argument("--field", required=True, help="Field name, e.g. ssn, passport, dob")
    sp.set_defaults(func=copy_field)

    sp = sub.add_parser("attach", help="Attach a local file; preserves original and can extract/OCR Markdown")
    sp.add_argument("query")
    sp.add_argument("file")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="Classification override; default auto")
    sp.add_argument("--extract", action="store_true", help="Extract text/Markdown when supported, e.g. text files and PDFs via MarkItDown")
    sp.add_argument("--ocr", action="store_true", help="OCR image attachments with Tesseract when available")
    sp.set_defaults(func=attach_file)

    sp = sub.add_parser("export-attachment", help="Decrypt attachment to an output path")
    sp.add_argument("query")
    sp.add_argument("attachment")
    sp.add_argument("--output", default="")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=export_attachment)

    sp = sub.add_parser("import-joplin-raw", help="Import notes and referenced resources from a Joplin RAW export directory")
    sp.add_argument("path", help="Joplin RAW export directory")
    sp.add_argument("--dry-run", action="store_true", help="Scan and report counts without writing to the Kaal vault")
    sp.add_argument("--tags", default="", help="Comma-separated extra tags for imported notes")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="Classification override for imported notes/resources")
    sp.add_argument("--extract", action="store_true", help="Extract text/Markdown from attached resources when supported")
    sp.add_argument("--ocr", action="store_true", help="OCR image resources with Tesseract when available")
    sp.set_defaults(func=import_joplin_raw)

    sp = sub.add_parser("delete", help="Delete note and attachments")
    sp.add_argument("query")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=delete_note)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
