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
    ("TaxId", re.compile(r"\b(?:ein|itin|tax\s*id)\s*(?:number|#|no\.?|:)?\s*([0-9A-Z-]{5,20})\b", re.I)),
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


def read_stdin_or_arg(value: str | None) -> str:
    if value is not None:
        return value
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Enter note body. Finish with Ctrl-D:", file=sys.stderr)
    return sys.stdin.read()


def note_path(note_id: str) -> Path:
    return NOTES_DIR / f"{note_id}.json"


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


def add_note(args: argparse.Namespace) -> None:
    init_if_needed()
    body = read_stdin_or_arg(args.body)
    if not body.strip():
        die("Refusing to create an empty note.")
    note_id = secrets.token_hex(6)
    ts = now_iso()
    note_obj = {"id": note_id, "title": args.title, "tags": parse_tags(args.tags), "created": ts, "updated": ts, "body": body}
    payload = encrypt_bytes(json.dumps(note_obj, ensure_ascii=False).encode(), aad=note_id.encode())
    secure_write_json(note_path(note_id), payload)
    index = load_index()
    index.setdefault("notes", []).append({"id": note_id, "title": args.title, "tags": parse_tags(args.tags), "created": ts, "updated": ts, "attachments": []})
    save_index(index)
    print(json.dumps({"status": "created", "id": note_id, "title": args.title, "tags": parse_tags(args.tags)}, indent=2, ensure_ascii=False))


def init_if_needed() -> None:
    if not VAULT.exists() or not INDEX_PATH.exists():
        die("Vault not initialized. Run: secure-notes init")


def load_note(note_meta: dict[str, Any]) -> dict[str, Any]:
    nid = note_meta["id"]
    path = note_path(nid)
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
    payload = encrypt_bytes(json.dumps(note, ensure_ascii=False).encode(), aad=note["id"].encode())
    secure_write_json(note_path(note["id"]), payload)
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == note["id"]:
            n["updated"] = note["updated"]
    save_index(index)
    print(json.dumps({"status": "appended", "id": note["id"], "title": note.get("title", "")}, indent=2, ensure_ascii=False))


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


def attach_file(args: argparse.Namespace) -> None:
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"Attachment file not found: {src}")
    meta = require_one_note(args.query)
    nid = meta["id"]
    data = src.read_bytes()
    att_id = secrets.token_hex(6)
    aad = f"{nid}:{att_id}:{src.name}".encode()
    payload = encrypt_bytes(data, aad=aad)
    out_dir = attachment_note_dir(nid)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_path = out_dir / f"{att_id}.json"
    secure_write_json(out_path, {"name": src.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encrypted": payload})
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == nid:
            n.setdefault("attachments", []).append({"id": att_id, "name": src.name, "size": len(data), "created": now_iso(), "sha256": hashlib.sha256(data).hexdigest()})
            n["updated"] = now_iso()
    save_index(index)
    print(json.dumps({"status": "attached", "note": nid, "attachmentId": att_id, "name": src.name, "size": len(data)}, indent=2, ensure_ascii=False))


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
    payload_path = attachment_note_dir(nid) / f"{att['id']}.json"
    data = json.loads(payload_path.read_text())
    aad = f"{nid}:{att['id']}:{data['name']}".encode()
    plaintext = decrypt_payload(data["encrypted"], aad=aad)
    out = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / data["name"]
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


def build_parser() -> argparse.ArgumentParser:
    prog = os.environ.get("KAAL_CLI_NAME", Path(sys.argv[0]).name)
    description = "Kaal: tiny local encrypted notes vault for sensitive data" if prog == "kaal" else "Tiny local encrypted notes vault for sensitive data"
    p = argparse.ArgumentParser(prog=prog, description=description)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Initialize vault and Keychain key")
    sp.set_defaults(func=init_vault)

    sp = sub.add_parser("status", help="Show vault status")
    sp.set_defaults(func=status)

    sp = sub.add_parser("add", help="Add encrypted note")
    sp.add_argument("--title", required=True)
    sp.add_argument("--tags", default="", help="Comma-separated tags")
    sp.add_argument("--body", help="Note body; omit to read stdin")
    sp.set_defaults(func=add_note)

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

    sp = sub.add_parser("attach", help="Encrypt and attach a local file to a note")
    sp.add_argument("query")
    sp.add_argument("file")
    sp.set_defaults(func=attach_file)

    sp = sub.add_parser("export-attachment", help="Decrypt attachment to an output path")
    sp.add_argument("query")
    sp.add_argument("attachment")
    sp.add_argument("--output", default="")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=export_attachment)

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
