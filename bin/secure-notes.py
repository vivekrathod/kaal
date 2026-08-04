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
import fcntl
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
from contextlib import contextmanager
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
RECEIPT_FIELD_NAMES = {"patient_name", "provider", "service_date", "paid_date", "amount", "currency", "insurer"}

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


@contextmanager
def vault_capture_lock():
    """Serialize multi-file browser captures that update the shared index."""
    lock_path = VAULT / ".medical-capture.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


def run_docling(path: Path) -> str:
    """Run the repository venv's Docling CLI and return its layout-aware Markdown."""
    cli = Path(sys.executable).with_name("docling")
    if not cli.is_file():
        return ""
    environment = os.environ.copy()
    # The Hermes shell may export a different Python environment. Docling's
    # executable must see only the Kaal venv that owns its compiled packages.
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="kaal-docling-") as td:
        try:
            proc = subprocess.run([str(cli), "convert", str(path), "--to", "md", "--to", "json", "--output", td, "--quiet"], text=True, capture_output=True, timeout=240, env=environment)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        outputs = sorted(Path(td).glob("*.md"))
        return "\n\n".join(output.read_text(encoding="utf-8") for output in outputs)


def run_docling_json(path: Path) -> dict[str, Any]:
    """Return Docling's structured local document output for a PDF, if available."""
    cli = Path(sys.executable).with_name("docling")
    if not cli.is_file():
        return {}
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="kaal-docling-json-") as td:
        try:
            proc = subprocess.run([str(cli), "convert", str(path), "--to", "json", "--output", td, "--quiet"], text=True, capture_output=True, timeout=240, env=environment)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if proc.returncode != 0:
            return {}
        outputs = sorted(Path(td).glob("*.json"))
        if len(outputs) != 1:
            return {}
        try:
            data = json.loads(outputs[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


def run_tesseract(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    proc = subprocess.run([tesseract, str(path), "stdout"], text=True, capture_output=True)
    return proc.stdout if proc.returncode == 0 else ""


def run_pdftotext(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return ""
    # Preserve column alignment: medical receipt labels and values frequently
    # share a visual row but are separated by whitespace rather than a colon.
    proc = subprocess.run([pdftotext, "-layout", str(path), "-"], text=True, capture_output=True)
    return proc.stdout if proc.returncode == 0 else ""


def run_pdf_ocr(path: Path, *, max_pages: int = 10) -> str:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm or not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory(prefix="kaal-pdf-ocr-") as td:
        prefix = Path(td) / "page"
        proc = subprocess.run([pdftoppm, "-png", "-r", "200", "-f", "1", "-l", str(max_pages), str(path), str(prefix)], text=True, capture_output=True)
        if proc.returncode != 0:
            return ""
        return "\n\n".join(text for image in sorted(Path(td).glob("page-*.png")) if (text := run_tesseract(image)).strip())


def run_macos_vision_pdf_ocr(path: Path) -> str:
    """Use macOS Vision's accurate recognizer when its local framework is available."""
    helper = Path(__file__).with_name("kaal-vision-ocr.swift")
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift or not helper.exists():
        return ""
    try:
        proc = subprocess.run([swift, str(helper), str(path)], text=True, capture_output=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def receipt_nonnegotiable_evidence_score(text: str) -> int:
    """Score only explicit paid-amount and date evidence from one extractor."""
    fields = infer_receipt_fields(text)
    return int(bool(fields.get("amount"))) + int(bool(fields.get("service_date") or fields.get("paid_date")))


def extract_attachment_markdown(path: Path, *, ocr: bool = False, extract: bool = True, receipt: bool = False) -> str:
    if not extract and not ocr:
        return ""
    suffix = path.suffix.lower()
    text = ""
    method = ""
    if extract and suffix in TEXT_ATTACHMENT_SUFFIXES:
        text = safe_read_text(path)
        method = "text"
    elif extract and suffix in PDF_ATTACHMENT_SUFFIXES:
        candidates = [("pdftotext-layout", run_pdftotext(path))]
        # A non-empty native PDF text layer is not enough if it lacks one of the
        # fields the user must verify. For PDFs, macOS Vision is the one local
        # layout-aware retry. Only if both fast sources remain insufficient do
        # we pay for the legacy conversion fallbacks.
        if receipt_nonnegotiable_evidence_score(candidates[0][1]) < 2:
            if ocr:
                candidates.append(("macos-vision", run_macos_vision_pdf_ocr(path)))
            best_local_score = max(receipt_nonnegotiable_evidence_score(candidate_text) for _candidate_method, candidate_text in candidates)
            if best_local_score < 2:
                candidates.append(("docling", run_docling(path)))
                # Medical receipts use the small, evidence-tested route:
                # native PDF text -> local Vision -> Docling. Keep the broader
                # MarkItDown/Tesseract fallbacks for non-receipt attachments.
                if not receipt:
                    candidates.append(("markitdown", run_markitdown(path)))
                    if ocr:
                        candidates.append(("tesseract-pdf", run_pdf_ocr(path)))
        candidates = [(candidate_method, candidate_text) for candidate_method, candidate_text in candidates if candidate_text.strip()]
        if candidates:
            method, text = max(
                enumerate(candidates),
                key=lambda item: (receipt_nonnegotiable_evidence_score(item[1][1]), -item[0]),
            )[1]
    elif ocr and suffix in IMAGE_ATTACHMENT_SUFFIXES:
        text = run_tesseract(path)
        method = "tesseract"
    if not text.strip():
        return ""
    return f"# Extracted text: {path.name}\n\nMethod: {method}\n\n```text\n{text.strip()}\n```\n"


def attachment_original_bytes(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> bytes:
    nid = note_meta["id"]
    att_dir = attachment_note_dir(nid) / attachment_meta["id"]
    if attachment_meta.get("storage", "encrypted") == "plaintext":
        return (att_dir / attachment_meta.get("stored_name", attachment_meta["name"])).read_bytes()
    payload_path = att_dir / "original.json"
    if not payload_path.exists():
        payload_path = attachment_note_dir(nid) / f"{attachment_meta['id']}.json"
    data = json.loads(payload_path.read_text())
    data_name = data["name"]
    aad = f"{nid}:{attachment_meta['id']}:{data_name}".encode()
    return decrypt_payload(data["encrypted"], aad=aad)


def attachment_extracted_sidecar_exists(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> bool:
    att_dir = attachment_note_dir(note_meta["id"]) / attachment_meta["id"]
    return (att_dir / "extracted.md").exists() or (att_dir / "extracted.md.json").exists()


def write_attachment_extracted_sidecar(note_meta: dict[str, Any], attachment_meta: dict[str, Any], extracted_md: str) -> None:
    nid = note_meta["id"]
    att_id = attachment_meta["id"]
    att_dir = attachment_note_dir(nid) / att_id
    att_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if attachment_meta.get("storage", "encrypted") == "encrypted":
        sidecar_payload = encrypt_bytes(extracted_md.encode(), aad=f"{nid}:{att_id}:extracted.md".encode())
        secure_write_json(att_dir / "extracted.md.json", {"name": "extracted.md", "encrypted": sidecar_payload})
        (att_dir / "extracted.md").unlink(missing_ok=True)
    else:
        sidecar = att_dir / "extracted.md"
        sidecar.write_text(extracted_md, encoding="utf-8")
        os.chmod(sidecar, 0o600)
        (att_dir / "extracted.md.json").unlink(missing_ok=True)


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


def create_note(title: str, body: str, *, tags: list[str] | None = None, sensitivity: str = "auto", source: dict[str, Any] | None = None) -> dict[str, Any]:
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
    if source:
        note_obj["source"] = source
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
    if source:
        meta["source"] = source
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


def safe_attachment_filename(name: str, att_id: str, *, max_bytes: int = 180) -> str:
    """Return a filesystem-safe storage filename while preserving the display name in metadata."""
    fallback = f"attachment-{att_id}"
    cleaned = Path(name).name.strip().replace("/", "_").replace(":", "_") or fallback
    cleaned = re.sub(r"[\x00-\x1f]", "_", cleaned)
    if cleaned in {".", ".."}:
        cleaned = fallback
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned
    suffix = Path(cleaned).suffix
    if len(suffix.encode("utf-8")) > 20:
        suffix = ""
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:12]
    stem_budget = max_bytes - len(suffix.encode("utf-8")) - len(digest) - 2
    stem = Path(cleaned).stem
    out = ""
    used = 0
    for ch in stem:
        b = len(ch.encode("utf-8"))
        if used + b > max(1, stem_budget):
            break
        out += ch
        used += b
    return f"{out or 'attachment'}-{digest}{suffix}"


def attach_file_to_note(meta: dict[str, Any], src: Path, *, sensitivity: str = "auto", extract: bool = False, ocr: bool = False, attachment_name: str | None = None, source: dict[str, Any] | None = None, receipt: bool = False) -> dict[str, Any]:
    data = src.read_bytes()
    stored_name = attachment_name or src.name
    nid = meta["id"]
    att_id = secrets.token_hex(6)
    disk_name = safe_attachment_filename(stored_name, att_id)
    extracted_md = extract_attachment_markdown(src, ocr=ocr, extract=extract, receipt=receipt)
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
        "stored_name": disk_name,
        "size": len(data),
        "created": now_iso(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage": storage,
        "sensitivity": classification["sensitivity"],
        "classification": classification,
        "extracted_markdown": bool(extracted_md),
    }
    if source:
        attachment_meta["source"] = source
    if storage == "encrypted":
        aad = f"{nid}:{att_id}:{stored_name}".encode()
        payload = encrypt_bytes(data, aad=aad)
        secure_write_json(out_dir / "original.json", {"name": stored_name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encrypted": payload})
    else:
        original = out_dir / disk_name
        original.write_bytes(data)
        os.chmod(original, 0o600)
    if extracted_md:
        write_attachment_extracted_sidecar(meta, attachment_meta, extracted_md)
    if extracted_md and src.suffix.lower() in PDF_ATTACHMENT_SUFFIXES and "Method: docling" in extracted_md:
        docling_json = run_docling_json(src)
        if docling_json:
            payload = json.dumps(docling_json, ensure_ascii=False).encode("utf-8")
            if storage == "encrypted":
                secure_write_json(out_dir / "docling.json.enc", {"encrypted": encrypt_bytes(payload, aad=f"{nid}:{att_id}:docling.json".encode())})
            else:
                (out_dir / "docling.json").write_bytes(payload)
                os.chmod(out_dir / "docling.json", 0o600)
            attachment_meta["docling_json"] = True
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
    plaintext = attachment_original_bytes(meta, att)
    out = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / att.get("name", "attachment")
    if out.exists() and not args.force:
        die(f"Output exists: {out}; pass --force to overwrite.")
    out.write_bytes(plaintext)
    os.chmod(out, 0o600)
    print(json.dumps({"status": "exported", "path": str(out), "bytes": len(plaintext), "sha256": hashlib.sha256(plaintext).hexdigest()}, indent=2))


def ocr_attachments(args: argparse.Namespace) -> None:
    init_if_needed()
    index = load_index()
    query = getattr(args, "query", "") or ""
    force = getattr(args, "force", False)
    dry_run = getattr(args, "dry_run", False)
    extract = not getattr(args, "no_extract", False)
    ocr = not getattr(args, "no_ocr", False)
    limit = getattr(args, "limit", 0) or 0
    notes = index.get("notes", [])
    if query:
        q = query.lower()
        notes = [n for n in notes if n.get("id") == query or q in n.get("title", "").lower()]
    scanned = 0
    eligible = 0
    skipped_existing = 0
    skipped_unsupported = 0
    extracted = 0
    no_text = 0
    errors: list[dict[str, str]] = []
    for note in notes:
        for att in note.get("attachments", []):
            scanned += 1
            suffix = Path(att.get("name", "")).suffix.lower()
            supported = (extract and suffix in TEXT_ATTACHMENT_SUFFIXES | PDF_ATTACHMENT_SUFFIXES) or (ocr and suffix in IMAGE_ATTACHMENT_SUFFIXES)
            if not supported:
                skipped_unsupported += 1
                continue
            if not force and (att.get("extracted_markdown") or attachment_extracted_sidecar_exists(note, att)):
                if not dry_run and attachment_extracted_sidecar_exists(note, att) and not att.get("extracted_markdown"):
                    att["extracted_markdown"] = True
                    secure_write_json(attachment_note_dir(note["id"]) / att["id"] / "metadata.json", att)
                skipped_existing += 1
                continue
            if limit and eligible >= limit:
                continue
            eligible += 1
            if dry_run:
                continue
            is_medical_receipt = "medical" in note.get("tags", []) and "receipt" in note.get("tags", [])
            try:
                docling_json: dict[str, Any] = {}
                with tempfile.TemporaryDirectory(prefix="kaal-ocr-") as td:
                    os.chmod(td, 0o700)
                    tmp = Path(td) / (att.get("stored_name") or att.get("name") or f"attachment-{att['id']}")
                    tmp.write_bytes(attachment_original_bytes(note, att))
                    os.chmod(tmp, 0o600)
                    extracted_md = extract_attachment_markdown(tmp, ocr=ocr, extract=extract, receipt=is_medical_receipt)
                    if extracted_md and tmp.suffix.lower() in PDF_ATTACHMENT_SUFFIXES and "Method: docling" in extracted_md:
                        docling_json = run_docling_json(tmp)
                if extracted_md:
                    write_attachment_extracted_sidecar(note, att, extracted_md)
                    att["extracted_markdown"] = True
                    att["extracted_at"] = now_iso()
                    extracted += 1
                    if docling_json:
                        out_dir = attachment_note_dir(note["id"]) / att["id"]
                        payload = json.dumps(docling_json, ensure_ascii=False).encode("utf-8")
                        if att.get("storage") == "encrypted":
                            secure_write_json(out_dir / "docling.json.enc", {"encrypted": encrypt_bytes(payload, aad=f"{note['id']}:{att['id']}:docling.json".encode())})
                        else:
                            (out_dir / "docling.json").write_bytes(payload)
                            os.chmod(out_dir / "docling.json", 0o600)
                        att["docling_json"] = True
                else:
                    att["extracted_markdown"] = False
                    att["extraction_attempted_at"] = now_iso()
                    no_text += 1
                if "medical" in note.get("tags", []) and "receipt" in note.get("tags", []):
                    prior_receipt = note.get("receipt", {})
                    note["receipt"] = build_receipt_state(extracted_md, docling_json, prior_receipt)
                secure_write_json(attachment_note_dir(note["id"]) / att["id"] / "metadata.json", att)
            except Exception as e:
                if "medical" in note.get("tags", []) and "receipt" in note.get("tags", []):
                    note["receipt"] = receipt_extraction_state("", str(e))
                errors.append({"note_id": note.get("id", ""), "attachment_id": att.get("id", ""), "name": att.get("name", ""), "error": str(e)})
    if not dry_run and (extracted or no_text or skipped_existing or errors):
        save_index(index)
    print(json.dumps({
        "status": "dry-run" if dry_run else "updated",
        "scanned_attachments": scanned,
        "eligible_attachments": eligible,
        "extracted_attachments": extracted,
        "no_text_attachments": no_text,
        "skipped_existing": skipped_existing,
        "skipped_unsupported": skipped_unsupported,
        "errors": errors,
    }, indent=2, ensure_ascii=False))


def infer_receipt_fields(extracted_text: str) -> dict[str, str]:
    """Conservative label-based field inference; users can correct every value."""
    text = re.sub(r"```(?:text)?|# Extracted text:.*|Method:.*", "", extracted_text, flags=re.IGNORECASE)
    approved_card_sale = bool(re.search(r"\b(?:sale|payment)\s*[-–]\s*approved\b", text, flags=re.IGNORECASE))
    lines = text.splitlines()

    def labelled_next_line_date(label: str) -> str:
        """Resolve a date directly below an OCR label without guessing roles."""
        for index, line in enumerate(lines[:-1]):
            if not re.search(label, line, flags=re.IGNORECASE):
                continue
            match = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", lines[index + 1])
            if match:
                return match.group(1)
        return ""
    patterns = {
        "patient_name": r"(?:patient|member)\s*(?:name)?\s*(?:[:#-]|\||\s{2,})\s*([^|\n]+)",
        "provider": r"(?:provider|facility|practice)\s*(?:name)?\s*(?:[:#-]|\||\s{2,})\s*([^|\n]+)",
        "service_date": r"(?:date\s+of\s+service|service\s+date|visit\s+date)\s*(?:[:#-]|\||\s{2,})\s*([^|\n]+)",
        "paid_date": r"(?:payment\s+date|date\s+paid|paid\s+on)\s*(?:[:#-]|\||\s{2,})\s*([^|\n]+)",
        "insurer": r"(?:insurance|insurer|plan)\s*(?:name)?\s*(?:[:#-]|\||\s{2,})\s*([^|\n]+)",
    }
    fields = {name: "" for name in patterns}
    for name, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip()[:200]
            if name != "provider" or not re.match(r"(?:dr\.?|doctor)\s+", value, flags=re.IGNORECASE):
                fields[name] = value
    if not fields["paid_date"]:
        fields["paid_date"] = labelled_next_line_date(r"\b(?:payment\s+date|date\s+paid|paid\s+on)\b")
    if not fields["service_date"]:
        fields["service_date"] = labelled_next_line_date(r"\b(?:date\s+of\s+service|service\s+date|visit\s+date)\b")
    # A generic Date is ambiguous on invoices and statements. On a completed
    # card-sale receipt, however, it is the card-payment transaction date.
    if approved_card_sale and not fields["paid_date"]:
        approved_date = re.search(r"\bdate\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", text, flags=re.IGNORECASE)
        if approved_date:
            fields["paid_date"] = approved_date.group(1)
    # A generic "Total" is commonly the billed charge, not the payment. Only
    # infer an amount when the receipt explicitly identifies a payment value.
    # A card transaction receipt marked "SALE - APPROVED" is a narrow additional
    # case: its labelled "Total Amount" is the completed card payment, not an
    # invoice/balance total. Bare "Total Amount" remains rejected.
    amount = re.search(r"(?:amount\s+paid|payment\s+amount|payment\s+received|paid\s+today|amount\s+collected|you\s+paid)\s*[:#-]?\s*\$?([0-9][0-9,]*\.[0-9]{2})\b", text, flags=re.IGNORECASE)
    if not amount and approved_card_sale:
        amount = re.search(r"\btotal\s+amount\s*[:#-]?\s*\$([0-9][0-9,]*\.[0-9]{2})", text, flags=re.IGNORECASE)
    fields["amount"] = amount.group(1).replace(",", "") if amount else ""
    fields["currency"] = "USD" if "$" in text else ""
    return fields


def infer_docling_fields(document: dict[str, Any]) -> dict[str, str]:
    """Resolve labelled Docling text cells while preserving conservative defaults."""
    entries = [str(item.get("text", "")).strip() for item in document.get("texts", []) if isinstance(item, dict) and str(item.get("text", "")).strip()]
    result: dict[str, str] = {}
    labels = {
        "patient_name": r"\b(?:patient|member)\s*(?:name)?\b",
        "provider": r"\b(?:provider|facility|practice)\s*(?:name)?\b",
        "service_date": r"\b(?:date\s+of\s+service|service\s+date|visit\s+date)\b",
        "paid_date": r"\b(?:payment\s+date|date\s+paid|paid\s+on)\b",
        "insurer": r"\b(?:insurance|insurer|plan)\s*(?:name)?\b",
    }
    generic_labels = {"date", "patient", "patient name", "member", "member name", "provider", "service date", "date of service", "payment date", "amount", "amount paid", "total", "total paid"}

    def plausible_value(field: str, value: str) -> bool:
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized or normalized.casefold() in generic_labels:
            return False
        if field == "patient_name":
            words = re.findall(r"[A-Za-z][A-Za-z'’-]*", normalized)
            return len(words) >= 2
        if field == "provider" and re.match(r"(?:dr\.?|doctor)\s+", normalized, flags=re.IGNORECASE):
            return False
        return True

    for index, entry in enumerate(entries):
        normalized = re.sub(r"\s+", " ", entry).strip()
        for field, pattern in labels.items():
            if field in result or not re.search(pattern, normalized, flags=re.IGNORECASE):
                continue
            inline = re.split(r"[:|]\s*", normalized, maxsplit=1)
            if len(inline) == 2 and plausible_value(field, inline[1]):
                result[field] = inline[1].strip()[:200]
                continue
            for candidate in entries[index + 1:index + 4]:
                candidate = re.sub(r"\s+", " ", candidate).strip()
                if plausible_value(field, candidate) and not any(re.match(other, candidate, flags=re.IGNORECASE) for other in labels.values()):
                    result[field] = candidate[:200]
                    break
    return result


def receipt_extraction_state(extracted_text: str = "", error: str = "", ai_fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Create candidates only from explicit receipt evidence, never semantic guesses."""
    fields = infer_receipt_fields(extracted_text) if extracted_text else {}
    sources: dict[str, str] = {key: "labelled-text" for key, value in fields.items() if value}
    return {"state": "error" if error else "extracted" if extracted_text.strip() else "no_text", "attempted_at": now_iso(), "error": error[:500], "fields": fields, "field_sources": sources}


def build_receipt_state(extracted_text: str, docling_json: dict[str, Any] | None = None, prior_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use one conservative field path for capture and re-extraction.

    Text/layout candidates are suggestions. Only explicitly manual values survive
    re-extraction, so a stale automatic result cannot become a durable fact.
    """
    receipt = receipt_extraction_state(extracted_text)
    for field, value in infer_docling_fields(docling_json or {}).items():
        if value and not receipt["fields"].get(field):
            receipt["fields"][field] = value
            receipt["field_sources"][field] = "docling-structured"
    prior = prior_receipt or {}
    prior_fields = prior.get("fields", {})
    prior_sources = prior.get("field_sources", {})
    for field, source in prior_sources.items():
        if source == "manual" and prior_fields.get(field):
            receipt["fields"][field] = prior_fields[field]
            receipt["field_sources"][field] = "manual"
    if prior.get("manually_updated_at"):
        receipt["manually_updated_at"] = prior["manually_updated_at"]
    return receipt


def receipt_confirmation_issues(receipt: dict[str, Any]) -> list[str]:
    fields = receipt.get("fields", {})
    issues = []
    if not fields.get("amount"):
        issues.append("amount")
    if not fields.get("service_date") and not fields.get("paid_date"):
        issues.append("date")
    return issues


def medical_capture(args: argparse.Namespace) -> None:
    """Create a plaintext medical-receipt inbox record from a browser capture.

    Receipt handling intentionally overrides Kaal's classifier: the user has
    chosen normal local filesystem protection for medical receipts rather than
    application-level encryption.
    """
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"Receipt file not found: {src}")
    source_url = (getattr(args, "source_url", "") or "").strip()
    source_title = (getattr(args, "source_title", "") or "").strip()
    captured_at = (getattr(args, "captured_at", "") or "").strip() or now_iso()
    title = (getattr(args, "title", "") or "").strip() or source_title or src.stem.replace("_", " ").replace("-", " ")
    source: dict[str, Any] = {"type": "browser-receipt-capture", "captured_at": captured_at}
    if source_url:
        source["url"] = source_url
    if source_title:
        source["title"] = source_title
    body_lines = [
        "# Medical receipt",
        "",
        "Status: inbox",
        f"Captured at: {captured_at}",
        f"Original file: {src.name}",
    ]
    if source_title:
        body_lines.append(f"Source title: {source_title}")
    if source_url:
        body_lines.append(f"Source URL: {source_url}")
    body_lines.extend([
        "",
        "Captured from the browser. Review or enrich this record later if needed.",
        "",
    ])
    with vault_capture_lock():
        meta = create_note(title, "\n".join(body_lines), tags=["medical", "receipt", "inbox"], sensitivity="public", source=source)
        try:
            attachment = attach_file_to_note(
                meta,
                src,
                sensitivity="public",
                extract=True,
                ocr=True,
                source=source,
                receipt=True,
            )
        except Exception:
            # Do not leave an empty inbox entry if copying or extracting the
            # receipt fails. The browser staging file remains in place so a
            # retry can start from the original artifact.
            note_path(meta["id"]).unlink(missing_ok=True)
            shutil.rmtree(attachment_note_dir(meta["id"]), ignore_errors=True)
            index = load_index()
            index["notes"] = [note for note in index.get("notes", []) if note.get("id") != meta["id"]]
            save_index(index)
            raise
        # Keep the field-state update in the same vault transaction as note
        # creation and attachment persistence. A browser capture must not see an
        # index between those steps and lose its just-created record.
        index = load_index()
        saved = medical_receipt_meta(index, meta["id"])
        sidecar = attachment_note_dir(meta["id"]) / attachment["id"] / "extracted.md"
        extracted_text = sidecar.read_text(encoding="utf-8") if sidecar.exists() else ""
        docling_json: dict[str, Any] = {}
        docling_sidecar = attachment_note_dir(meta["id"]) / attachment["id"] / "docling.json"
        if docling_sidecar.exists():
            try:
                docling_json = json.loads(docling_sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                docling_json = {}
        saved["receipt"] = build_receipt_state(extracted_text, docling_json)
        saved["updated"] = now_iso()
        save_index(index)
    print(json.dumps({
        "status": "captured",
        "id": meta["id"],
        "title": meta["title"],
        "storage": meta["storage"],
        "sensitivity": meta["sensitivity"],
        "attachment_id": attachment["id"],
        "attachment_name": attachment["name"],
        "extracted_markdown": attachment["extracted_markdown"],
    }, indent=2, ensure_ascii=False))


def medical_list(args: argparse.Namespace) -> None:
    """List receipt metadata without mixing in unrelated Kaal notes."""
    init_if_needed()
    notes = [n for n in load_index().get("notes", []) if "medical" in n.get("tags", []) and "receipt" in n.get("tags", [])]
    if getattr(args, "trash", False):
        notes = [n for n in notes if n.get("trashed_at")]
    else:
        notes = [n for n in notes if not n.get("trashed_at")]
    if getattr(args, "inbox", False):
        notes = [n for n in notes if "inbox" in n.get("tags", [])]
    notes.sort(key=lambda note: note.get("updated", ""), reverse=True)
    limit = getattr(args, "limit", 0) or 0
    if limit:
        notes = notes[:limit]
    if getattr(args, "json", False):
        print(json.dumps(notes, indent=2, ensure_ascii=False))
        return
    for note in notes:
        tags = " ".join(f"#{tag}" for tag in note.get("tags", []))
        print(f"{note['id']}  {note.get('updated', '')}  {note.get('title', '')}  {tags}  attachments:{len(note.get('attachments', []))}")


def medical_receipt_meta(index: dict[str, Any], note_id: str) -> dict[str, Any]:
    for note in index.get("notes", []):
        if note.get("id") == note_id:
            if "medical" not in note.get("tags", []) or "receipt" not in note.get("tags", []):
                die("That note is not a medical receipt.")
            return note
    die("Medical receipt not found.")


def medical_review(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        receipt = meta.get("receipt", {})
        if receipt_confirmation_issues(receipt):
            die("Cannot mark reviewed until the paid amount and a date are confirmed.")
        fields = receipt.setdefault("fields", {})
        sources = receipt.setdefault("field_sources", {})
        for field in ("amount", "service_date", "paid_date"):
            if fields.get(field) and sources.get(field) != "manual":
                sources[field] = "confirmed"
        meta["tags"] = [tag for tag in meta.get("tags", []) if tag != "inbox"]
        meta["reviewed_at"] = now_iso()
        meta["updated"] = meta["reviewed_at"]
        save_index(index)
    print(json.dumps({"status": "reviewed", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False))


def medical_reopen(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        meta["tags"] = sorted(set(meta.get("tags", []) + ["inbox"]))
        meta.pop("reviewed_at", None)
        meta["updated"] = now_iso()
        save_index(index)
    print(json.dumps({"status": "inbox", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False))


def medical_extracted_text(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = medical_receipt_meta(load_index(), args.note_id)
    attachment_id = getattr(args, "attachment_id", "") or ""
    matches = [att for att in meta.get("attachments", []) if not attachment_id or att.get("id") == attachment_id]
    if len(matches) != 1:
        die("Specify one receipt attachment id.")
    att = matches[0]
    sidecar = attachment_note_dir(meta["id"]) / att["id"] / "extracted.md"
    encrypted_sidecar = attachment_note_dir(meta["id"]) / att["id"] / "extracted.md.json"
    if sidecar.exists():
        text = sidecar.read_text(encoding="utf-8")
    elif encrypted_sidecar.exists():
        payload = json.loads(encrypted_sidecar.read_text())
        text = decrypt_payload(payload["encrypted"], aad=f"{meta['id']}:{att['id']}:extracted.md".encode()).decode()
    else:
        die("No extracted text is available. Run extraction first.")
    print(json.dumps({"status": "ok", "id": meta["id"], "attachment_id": att["id"], "text": text}, ensure_ascii=False))


def medical_update(args: argparse.Namespace) -> None:
    init_if_needed()
    try:
        updates = json.loads(args.fields_json)
    except json.JSONDecodeError:
        die("Receipt fields must be valid JSON.")
    allowed = {"patient_name", "provider", "service_date", "paid_date", "insurer", "amount", "currency"}
    if not isinstance(updates, dict) or any(key not in allowed or not isinstance(value, str) or len(value) > 200 for key, value in updates.items()):
        die("Receipt fields are invalid.")
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        receipt = meta.setdefault("receipt", receipt_extraction_state())
        fields = receipt.setdefault("fields", {})
        sources = receipt.setdefault("field_sources", {})
        fields.update(updates)
        for key, value in updates.items():
            if value:
                sources[key] = "manual"
            else:
                sources.pop(key, None)
        receipt["manually_updated_at"] = now_iso()
        meta["updated"] = receipt["manually_updated_at"]
        save_index(index)
    print(json.dumps({"status": "updated", "id": meta["id"], "fields": receipt["fields"]}, ensure_ascii=False))


def medical_report(args: argparse.Namespace) -> None:
    init_if_needed()
    notes = [note for note in load_index().get("notes", []) if "medical" in note.get("tags", []) and "receipt" in note.get("tags", []) and not note.get("trashed_at")]
    patient = (args.patient_name or "").lower()
    start, end, year = args.from_date or "", args.to_date or "", args.year or ""
    rows = []
    total = 0.0
    for note in notes:
        fields = note.get("receipt", {}).get("fields", {})
        date = fields.get("service_date") or fields.get("paid_date") or ""
        if patient and patient not in fields.get("patient_name", "").lower(): continue
        if year and not date.startswith(year): continue
        if start and date < start: continue
        if end and date > end: continue
        try: total += float(fields.get("amount") or 0)
        except ValueError: pass
        rows.append({"id": note["id"], "title": note.get("title", ""), "fields": fields})
    print(json.dumps({"status": "ok", "count": len(rows), "total_amount": f"{total:.2f}", "receipts": rows}, ensure_ascii=False))


def medical_trash(args: argparse.Namespace) -> None:
    """Soft-delete a receipt while preserving its Kaal-managed files for restore."""
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        if meta.get("trashed_at"):
            die("Medical receipt is already in trash.")
        meta["trashed_at"] = now_iso()
        meta["updated"] = meta["trashed_at"]
        save_index(index)
    print(json.dumps({"status": "trashed", "id": meta["id"], "title": meta["title"], "attachments": len(meta.get("attachments", []))}, indent=2, ensure_ascii=False))


def medical_restore(args: argparse.Namespace) -> None:
    """Restore a soft-deleted medical receipt to the active inbox/list."""
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        if not meta.get("trashed_at"):
            die("Medical receipt is not in trash.")
        meta.pop("trashed_at", None)
        meta["updated"] = now_iso()
        save_index(index)
    print(json.dumps({"status": "restored", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False))


def medical_purge(args: argparse.Namespace) -> None:
    """Irreversibly delete a receipt already placed in the medical trash."""
    init_if_needed()
    if not getattr(args, "yes", False):
        die("Permanent receipt deletion requires --yes.")
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        if not meta.get("trashed_at"):
            die("Move the medical receipt to trash before permanently deleting it.")
        note_id = meta["id"]
        # Only Kaal-managed copies and sidecars are removed. The original browser
        # download/source file is never stored under these vault paths.
        purge_dir = VAULT / ".receipt-purge" / note_id
        moves = [
            (plaintext_note_path(note_id), purge_dir / "note.md"),
            (encrypted_note_path(note_id), purge_dir / "note.enc.json"),
            (attachment_note_dir(note_id), purge_dir / "attachments"),
        ]
        moved: list[tuple[Path, Path]] = []
        try:
            for source, staged in moves:
                if source.exists():
                    staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.replace(source, staged)
                    moved.append((source, staged))
            index["notes"] = [note for note in index.get("notes", []) if note.get("id") != note_id]
            save_index(index)
        except Exception:
            for source, staged in reversed(moved):
                if staged.exists():
                    os.replace(staged, source)
            if purge_dir.exists():
                shutil.rmtree(purge_dir)
            raise
        try:
            shutil.rmtree(purge_dir, ignore_errors=False)
        except Exception as cleanup_error:
            # The durable index change succeeded but physical erasure did not.
            # Restore the receipt to trash so it stays visible and recoverable
            # instead of stranding a hidden copy under .receipt-purge.
            for source, staged in reversed(moved):
                if staged.exists():
                    os.replace(staged, source)
            index["notes"].append(meta)
            save_index(index)
            die(f"Permanent deletion could not be completed; receipt was restored to trash: {cleanup_error}")
        try:
            purge_dir.parent.rmdir()
        except OSError:
            pass
    print(json.dumps({"status": "purged", "id": note_id, "title": meta["title"]}, indent=2, ensure_ascii=False))


def medical_purge_bulk(args: argparse.Namespace) -> None:
    """Irreversibly delete multiple receipts that are already in medical trash."""
    init_if_needed()
    note_ids = list(getattr(args, "note_ids", []))
    if not getattr(args, "yes", False):
        die("Permanent receipt deletion requires --yes.")
    if not note_ids:
        die("Select at least one trashed medical receipt to permanently delete.")
    if len(note_ids) != len(set(note_ids)):
        die("Each receipt can be permanently deleted only once per bulk action.")
    with vault_capture_lock():
        index = load_index()
        metas = [medical_receipt_meta(index, note_id) for note_id in note_ids]
        if any(not meta.get("trashed_at") for meta in metas):
            die("Move every selected medical receipt to trash before permanently deleting it.")
        batch_dir = VAULT / ".receipt-purge" / f"bulk-{secrets.token_hex(8)}"
        moved: list[tuple[Path, Path]] = []
        try:
            for meta in metas:
                note_id = meta["id"]
                receipt_dir = batch_dir / note_id
                for source, staged in (
                    (plaintext_note_path(note_id), receipt_dir / "note.md"),
                    (encrypted_note_path(note_id), receipt_dir / "note.enc.json"),
                    (attachment_note_dir(note_id), receipt_dir / "attachments"),
                ):
                    if source.exists():
                        staged.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        os.replace(source, staged)
                        moved.append((source, staged))
            selected_ids = set(note_ids)
            index["notes"] = [note for note in index.get("notes", []) if note.get("id") not in selected_ids]
            save_index(index)
        except Exception:
            for source, staged in reversed(moved):
                if staged.exists():
                    os.replace(staged, source)
            if batch_dir.exists():
                shutil.rmtree(batch_dir)
            raise
        try:
            shutil.rmtree(batch_dir, ignore_errors=False)
        except Exception as cleanup_error:
            # As with a single purge, make every still-staged record visible in
            # Trash rather than leaving recoverable Kaal data hidden on disk.
            for source, staged in reversed(moved):
                if staged.exists():
                    os.replace(staged, source)
            index["notes"].extend(metas)
            save_index(index)
            die(f"Bulk permanent deletion could not be completed; receipts were restored to trash: {cleanup_error}")
        try:
            batch_dir.parent.rmdir()
        except OSError:
            pass
    print(json.dumps({"status": "purged", "count": len(note_ids), "ids": note_ids}, indent=2, ensure_ascii=False))


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
    for path in sorted(p for p in export_dir.glob("*.md") if p.is_file() and not p.name.startswith("._")):
        try:
            item = parse_joplin_raw_item(path.read_text(encoding="utf-8", errors="replace"))
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


def joplin_import_tags(note: dict[str, Any], parsed: dict[str, Any], extra_tags: list[str]) -> list[str]:
    props = note["props"]
    notebook_path = joplin_notebook_path(props.get("parent_id", ""), parsed["folders"])
    tags = {"imported", "joplin", *extra_tags}
    tags.update(slug_tag(t) for t in parsed["note_tags"].get(props["id"], []))
    tags.update(f"joplin-notebook-{slug_tag(part)}" for part in notebook_path)
    return sorted(tags)


def find_existing_joplin_note(index: dict[str, Any], note: dict[str, Any], expected_tags: list[str], used_ids: set[str] | None = None) -> dict[str, Any] | None:
    used_ids = used_ids or set()
    joplin_id = note["props"]["id"]
    source_matches = [
        n for n in index.get("notes", [])
        if n.get("id") not in used_ids and n.get("source", {}).get("type") == "joplin" and n.get("source", {}).get("id") == joplin_id
    ]
    if len(source_matches) == 1:
        return source_matches[0]
    title = note.get("title") or "Untitled Joplin Note"
    expected = set(expected_tags)
    legacy_matches = [
        n for n in index.get("notes", [])
        if n.get("id") not in used_ids and n.get("title") == title and {"imported", "joplin"}.issubset(set(n.get("tags", []))) and expected.issubset(set(n.get("tags", [])))
    ]
    if len(legacy_matches) == 1:
        return legacy_matches[0]
    fallback_matches = [
        n for n in index.get("notes", [])
        if n.get("id") not in used_ids and n.get("title") == title and {"imported", "joplin"}.issubset(set(n.get("tags", [])))
    ]
    if len(fallback_matches) == 1:
        return fallback_matches[0]
    expected_body_hash = hashlib.sha256((note.get("body", "") or "<!-- Imported empty Joplin note body -->\n").encode("utf-8")).hexdigest()
    body_matches = []
    for candidate in fallback_matches or legacy_matches:
        try:
            candidate_body = load_note(candidate).get("body", "")
        except Exception:
            continue
        if hashlib.sha256(candidate_body.encode("utf-8")).hexdigest() == expected_body_hash:
            body_matches.append(candidate)
    if body_matches:
        return sorted(body_matches, key=lambda n: n.get("id", ""))[0]
    return None


def note_has_resource_attachment(note_meta: dict[str, Any], rid: str, blob: Path) -> bool:
    blob_hash = hashlib.sha256(blob.read_bytes()).hexdigest()
    for attachment in note_meta.get("attachments", []):
        source = attachment.get("source", {})
        if source.get("type") == "joplin" and source.get("resource_id") == rid:
            return True
        if attachment.get("sha256") == blob_hash:
            return True
    return False


def import_joplin_raw(args: argparse.Namespace) -> None:
    init_if_needed()
    export_dir = Path(args.path).expanduser().resolve()
    parsed = load_joplin_raw_export(export_dir)
    referenced_count = sum(len(ids) for ids in parsed["referenced"].values())
    incremental = getattr(args, "incremental", False)
    extra_tags = parse_tags(getattr(args, "tags", ""))
    index = load_index()
    planned_notes = 0
    planned_attachments = 0
    skipped_existing_notes = 0
    skipped_existing_attachments = 0
    plan: list[tuple[dict[str, Any], dict[str, Any] | None, list[str]]] = []
    used_existing_note_ids: set[str] = set()
    for note in parsed["notes"]:
        tags = joplin_import_tags(note, parsed, extra_tags)
        existing = find_existing_joplin_note(index, note, tags, used_existing_note_ids) if incremental else None
        if existing:
            used_existing_note_ids.add(existing["id"])
            skipped_existing_notes += 1
        else:
            planned_notes += 1
        plan.append((note, existing, tags))
        note_id = note["props"]["id"]
        for rid in parsed["referenced"].get(note_id, []):
            resource = parsed["resources"].get(rid)
            blob = joplin_resource_blob(export_dir, resource) if resource else None
            if not blob:
                continue
            if existing and note_has_resource_attachment(existing, rid, blob):
                skipped_existing_attachments += 1
            else:
                planned_attachments += 1
    summary = {
        "status": "dry-run" if args.dry_run else "imported",
        "mode": "incremental" if incremental else "full",
        "notes": len(parsed["notes"]),
        "folders": len(parsed["folders"]),
        "resources": len(parsed["resources"]),
        "tags": len(parsed["tags"]),
        "referenced_resources": referenced_count,
        "unresolved_resources": parsed["unresolved_resources"],
        "likely_sensitive_notes": parsed["likely_sensitive_notes"],
        "warnings": parsed["warnings"],
    }
    if incremental:
        summary.update({
            "existing_notes": skipped_existing_notes,
            "pending_notes": planned_notes,
            "existing_attachments": skipped_existing_attachments,
            "pending_attachments": planned_attachments,
        })
    if args.dry_run:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    imported_notes = 0
    imported_attachments = 0
    skipped_notes = 0
    skipped_attachments = 0
    for note, existing, tags in plan:
        props = note["props"]
        note_id = props["id"]
        if existing:
            meta = existing
            skipped_notes += 1
        else:
            body = note.get("body", "") or "<!-- Imported empty Joplin note body -->\n"
            meta = create_note(
                note.get("title") or "Untitled Joplin Note",
                body,
                tags=tags,
                sensitivity=getattr(args, "sensitivity", "auto"),
                source={"type": "joplin", "id": note_id},
            )
            imported_notes += 1
        for rid in parsed["referenced"].get(note_id, []):
            resource = parsed["resources"].get(rid)
            blob = joplin_resource_blob(export_dir, resource) if resource else None
            if not blob:
                continue
            if incremental and existing and note_has_resource_attachment(meta, rid, blob):
                skipped_attachments += 1
                continue
            attach_file_to_note(
                meta,
                blob,
                sensitivity=getattr(args, "sensitivity", "auto"),
                extract=not getattr(args, "no_extract", False),
                ocr=not getattr(args, "no_ocr", False),
                attachment_name=resource.get("title") or blob.name,
                source={"type": "joplin", "note_id": note_id, "resource_id": rid},
            )
            imported_attachments += 1
    summary["imported_notes"] = imported_notes
    summary["imported_attachments"] = imported_attachments
    if incremental:
        summary["skipped_existing_notes"] = skipped_notes
        summary["skipped_existing_attachments"] = skipped_attachments
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

    sp = sub.add_parser("ocr-attachments", help="Extract/OCR text from existing attachments into separate sidecars")
    sp.add_argument("--query", default="", help="Optional note id or title substring to limit processing")
    sp.add_argument("--dry-run", action="store_true", help="Report eligible attachments without writing OCR sidecars")
    sp.add_argument("--force", action="store_true", help="Reprocess attachments that already have extracted Markdown")
    sp.add_argument("--limit", type=int, default=0, help="Maximum eligible attachments to process")
    sp.add_argument("--no-extract", action="store_true", help="Do not extract text/PDF attachments")
    sp.add_argument("--no-ocr", action="store_true", help="Do not OCR image attachments")
    sp.set_defaults(func=ocr_attachments)

    sp = sub.add_parser("import-joplin-raw", help="Import notes and referenced resources from a Joplin RAW export directory")
    sp.add_argument("path", help="Joplin RAW export directory")
    sp.add_argument("--dry-run", action="store_true", help="Scan and report counts without writing to the Kaal vault")
    sp.add_argument("--incremental", "--repair", action="store_true", help="Repair/import only missing Joplin notes and attachments; skip existing imported notes")
    sp.add_argument("--tags", default="", help="Comma-separated extra tags for imported notes")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="Classification override for imported notes/resources")
    sp.add_argument("--no-extract", action="store_true", help="Do not extract text/Markdown from text/PDF resources; extraction is on by default")
    sp.add_argument("--no-ocr", action="store_true", help="Do not OCR image resources; OCR is on by default")
    sp.set_defaults(func=import_joplin_raw)

    sp = sub.add_parser("medical", help="Capture and manage plaintext medical expense receipts")
    medical_sub = sp.add_subparsers(dest="medical_cmd", required=True)
    capture = medical_sub.add_parser("capture", help="Capture a browser-downloaded receipt into the plaintext medical inbox")
    capture.add_argument("file", help="Downloaded PDF, image, or receipt file")
    capture.add_argument("--title", default="", help="Receipt title; defaults to browser title or filename")
    capture.add_argument("--source-url", default="", help="Original browser page or PDF URL")
    capture.add_argument("--source-title", default="", help="Original browser tab title")
    capture.add_argument("--captured-at", default="", help="ISO-8601 capture time; defaults to now")
    capture.set_defaults(func=medical_capture)
    medical_list_parser = medical_sub.add_parser("list", help="List medical receipt records")
    medical_list_filter = medical_list_parser.add_mutually_exclusive_group()
    medical_list_filter.add_argument("--inbox", action="store_true", help="Show only receipts awaiting review")
    medical_list_filter.add_argument("--trash", action="store_true", help="Show only receipts in the recoverable trash")
    medical_list_parser.add_argument("--json", action="store_true")
    medical_list_parser.add_argument("--limit", type=int, default=0, help="Maximum newest records to return; default all")
    medical_list_parser.set_defaults(func=medical_list)
    medical_review_parser = medical_sub.add_parser("review", help="Mark a receipt as reviewed")
    medical_review_parser.add_argument("note_id")
    medical_review_parser.set_defaults(func=medical_review)
    medical_reopen_parser = medical_sub.add_parser("reopen", help="Return a reviewed receipt to the inbox")
    medical_reopen_parser.add_argument("note_id")
    medical_reopen_parser.set_defaults(func=medical_reopen)
    medical_text_parser = medical_sub.add_parser("extracted-text", help="Print extracted text for one receipt attachment")
    medical_text_parser.add_argument("note_id")
    medical_text_parser.add_argument("--attachment-id", default="")
    medical_text_parser.set_defaults(func=medical_extracted_text)
    medical_update_parser = medical_sub.add_parser("update", help="Manually correct extracted receipt fields")
    medical_update_parser.add_argument("note_id")
    medical_update_parser.add_argument("--fields-json", required=True)
    medical_update_parser.set_defaults(func=medical_update)
    medical_report_parser = medical_sub.add_parser("report", help="Summarize active receipts by field filters")
    medical_report_parser.add_argument("--year", default="")
    medical_report_parser.add_argument("--patient-name", default="")
    medical_report_parser.add_argument("--from-date", default="")
    medical_report_parser.add_argument("--to-date", default="")
    medical_report_parser.set_defaults(func=medical_report)
    medical_trash_parser = medical_sub.add_parser("trash", help="Move a receipt to recoverable trash")
    medical_trash_parser.add_argument("note_id")
    medical_trash_parser.set_defaults(func=medical_trash)
    medical_restore_parser = medical_sub.add_parser("restore", help="Restore a receipt from recoverable trash")
    medical_restore_parser.add_argument("note_id")
    medical_restore_parser.set_defaults(func=medical_restore)
    medical_purge_parser = medical_sub.add_parser("purge", help="Permanently delete a receipt already in trash")
    medical_purge_parser.add_argument("note_id")
    medical_purge_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion")
    medical_purge_parser.set_defaults(func=medical_purge)
    medical_purge_bulk_parser = medical_sub.add_parser("purge-bulk", help="Permanently delete multiple receipts already in trash")
    medical_purge_bulk_parser.add_argument("note_ids", nargs="+")
    medical_purge_bulk_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion")
    medical_purge_bulk_parser.set_defaults(func=medical_purge_bulk)

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
