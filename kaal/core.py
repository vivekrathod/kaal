#!/usr/bin/env python3
"""Kaal core: the encrypted notes vault and its command handlers.

Vault: the folder named by KAAL_VAULT (default ~/.kaal)
Key storage: kaal.keys (an owner-only key file named by KAAL_KEY_FILE)
Encryption: AES-256-GCM via Python cryptography.

This script intentionally keeps plaintext out of the vault. Index metadata contains
only ids, titles, tags, timestamps, and attachment filenames.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Iterator, NoReturn
from urllib.parse import urlsplit, urlunsplit

from kaal import keys as key_providers
from kaal import receipt_ai, receipts, tools

VAULT = Path(os.environ.get("KAAL_VAULT") or str(Path.home() / ".kaal")).expanduser()
NOTES_DIR = VAULT / "notes"
ATTACH_DIR = VAULT / "attachments"
INDEX_PATH = VAULT / "index.json"
VAULT_LOCK_NAME = ".vault.lock"
# A vault with a key of its own keeps it here, wrapped by the installation's key.
VAULT_KEY_NAME = "vault.key.json"


def configure_vault(path: str | os.PathLike[str]) -> Path:
    """Point the core at a vault directory (kaal-server calls this at startup)."""
    global VAULT, NOTES_DIR, ATTACH_DIR, INDEX_PATH
    VAULT = Path(path).expanduser()
    NOTES_DIR = VAULT / "notes"
    ATTACH_DIR = VAULT / "attachments"
    INDEX_PATH = VAULT / "index.json"
    return VAULT


class Vault:
    """One vault: a folder of encrypted notes and files, with its key and locks.

    A process can hold several. Code reaches the one it is working on through
    ``current_vault()``; ``use_vault()`` chooses it for a request or a command.
    """

    def __init__(self, root: str | os.PathLike[str], *, key: bytes | None = None) -> None:
        self._root = Path(root).expanduser()
        # This vault's own key. None means the installation's key (KAAL_KEY_FILE).
        self.key = key
        # In-process half of vault_capture_lock(); the other half is a file lock.
        self.thread_lock = threading.RLock()
        self.lock_state = threading.local()
        # kaal-server holds this around each request that touches the vault.
        self.request_lock = threading.Lock()
        # Decrypted text kept for searching (see note_text_pieces). Memory only.
        self.text_cache: dict[str, tuple[Any, list[tuple[str, str, str]]]] = {}
        self.text_sizes: dict[str, int] = {}
        self.text_total = 0

    @property
    def root(self) -> Path:
        return self._root

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def attach_dir(self) -> Path:
        return self.root / "attachments"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def __repr__(self) -> str:
        return f"Vault({str(self.root)!r})"


class _DefaultVault(Vault):
    """The vault named by KAAL_VAULT, used when no other has been chosen.

    Its location is read from this module's VAULT, NOTES_DIR, ATTACH_DIR and
    INDEX_PATH each time, so configure_vault() moves it.
    """

    def __init__(self) -> None:
        super().__init__(VAULT)

    @property
    def root(self) -> Path:
        return VAULT

    @property
    def notes_dir(self) -> Path:
        return NOTES_DIR

    @property
    def attach_dir(self) -> Path:
        return ATTACH_DIR

    @property
    def index_path(self) -> Path:
        return INDEX_PATH


_DEFAULT_VAULT = _DefaultVault()
_ACTIVE_VAULT: ContextVar[Vault | None] = ContextVar("kaal_vault", default=None)


def default_vault() -> Vault:
    return _DEFAULT_VAULT


def current_vault() -> Vault:
    """The vault this thread of work is using."""
    return _ACTIVE_VAULT.get() or _DEFAULT_VAULT


@contextmanager
def use_vault(vault: Vault) -> Iterator[Vault]:
    """Work on ``vault`` inside the block, then go back to the one before."""
    token = _ACTIVE_VAULT.set(vault)
    try:
        yield vault
    finally:
        _ACTIVE_VAULT.reset(token)


def note_by_id(note_id: str) -> dict[str, Any]:
    """Return the note with exactly this id; never falls back to title matching."""
    for note in load_index().get("notes", []):
        if note.get("id") == note_id:
            return note
    die("Note not found.", kind="not_found")
KEY_BYTES = key_providers.KEY_BYTES

SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("SSN", re.compile(r"\b(?!000|666|9\d\d)(\d{3})[- ]?(?!00)(\d{2})[- ]?(?!0000)(\d{4})\b")),
    ("CreditCard", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("Passport", re.compile(r"\bpassport\s*(?:number|#|no\.?|:)?\s*([A-Z0-9]{6,9})\b", re.I)),
    ("DriverLicense", re.compile(r"\b(?:driver'?s?\s+license|dl)\s*(?:number|#|no\.?|:)?\s*([A-Z0-9-]{5,20})\b", re.I)),
    ("TaxId", re.compile(r"\b(?:ein|itin|tax\s*id)\b\s*(?:number|#|no\.?|:)?\s*([0-9A-Z-]{5,20})\b", re.I)),
]
TEXT_ATTACHMENT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm"}
IMAGE_ATTACHMENT_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
PDF_ATTACHMENT_SUFFIXES = {".pdf"}
RECEIPT_FIELD_NAMES = {"patient_name", "provider", "service_date", "paid_date", "amount", "currency", "insurer"}




class KaalError(SystemExit):
    """A user-facing failure.

    It subclasses SystemExit so the CLI keeps exiting with status 1. ``kind``
    lets kaal-server map it to an HTTP status: invalid, not_found, conflict or
    unavailable. ``details`` carries structured context such as the notes an
    ambiguous query matched.
    """

    def __init__(self, message: str, *, code: int = 1, kind: str = "invalid", details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.message = message
        self.kind = kind
        self.details = details or {}

    def __str__(self) -> str:
        return self.message


# When set, handlers hand their result to this list instead of printing it,
# and errors are raised without being printed. kaal-server uses this so it runs
# exactly the same handlers as the CLI.
_RESULT_SINK: ContextVar[list[tuple[Any, dict[str, Any]]] | None] = ContextVar("kaal_result_sink", default=None)


def capturing() -> bool:
    return _RESULT_SINK.get() is not None


def die(msg: str, code: int = 1, *, kind: str = "invalid", details: dict[str, Any] | None = None) -> NoReturn:
    if not capturing():
        print(f"ERROR: {msg}", file=sys.stderr)
    raise KaalError(msg, code=code, kind=kind, details=details)


def emit(payload: Any, *, indent: int | None = None, ensure_ascii: bool = True, meta: dict[str, Any] | None = None) -> None:
    """Deliver a handler's result: printed as JSON on the CLI, captured by the server.

    ``meta`` is extra information about the result, such as the total behind a
    page of a list. The server returns it beside the result; the CLI drops it.
    """
    sink = _RESULT_SINK.get()
    if sink is not None:
        sink.append((payload, {"indent": indent, "ensure_ascii": ensure_ascii, **({"meta": meta} if meta else {})}))
        return
    print(json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii))


def call_captured(handler: Callable[[Any], None], args: Any) -> tuple[Any, dict[str, Any]]:
    """Run a command handler and return (result, json options) instead of printing."""
    sink: list[tuple[Any, dict[str, Any]]] = []
    token = _RESULT_SINK.set(sink)
    try:
        handler(args)
    finally:
        _RESULT_SINK.reset(token)
    if len(sink) != 1:
        raise RuntimeError(f"{getattr(handler, '__name__', 'handler')} produced {len(sink)} results; expected one")
    return sink[0]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_provider() -> key_providers.KeyProvider:
    try:
        return key_providers.provider_from_env()
    except key_providers.KeyProviderError as error:
        die(str(error), kind="unavailable")


def installation_key(create: bool = False) -> bytes:
    """The key in KAAL_KEY_FILE."""
    provider = key_provider()
    try:
        return provider.get_key(create=create)
    except key_providers.KeyProviderError as error:
        die(str(error), kind="unavailable")


def get_key(create: bool = False) -> bytes:
    """The key the current vault's contents are encrypted with."""
    vault = current_vault()
    if vault.key is not None:
        return vault.key
    key = installation_key(create=create)
    wrapped_path = vault.root / VAULT_KEY_NAME
    if not wrapped_path.is_file():
        return key
    try:
        return key_providers.unwrap_key(key, json.loads(wrapped_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        die("This vault's own key file could not be read.", kind="unavailable")
    except key_providers.KeyProviderError as error:
        die(str(error), kind="unavailable")


def create_vault_folder(root: str | os.PathLike[str], *, own_key: bool = True) -> Vault:
    """Make a new, empty vault at ``root`` and return it.

    With ``own_key`` it gets a fresh key, saved in the folder wrapped by the
    installation's key (which must already exist).
    """
    vault = Vault(root)
    if vault.index_path.exists() or (vault.root.exists() and any(vault.root.iterdir())):
        die(f"There is already something at {vault.root}.", kind="conflict")
    _mkdirs_700(vault.root)
    for folder in (vault.root, vault.notes_dir, vault.attach_dir):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
    if own_key:
        wrapped = key_providers.wrap_key(installation_key(), key_providers.new_key())
        secure_write_json(vault.root / VAULT_KEY_NAME, wrapped)
    with use_vault(vault):
        save_index({"version": 1, "created": now_iso(), "notes": []})
    return vault


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


def vault_lock_held() -> bool:
    return getattr(current_vault().lock_state, "depth", 0) > 0


@contextmanager
def vault_capture_lock():
    """Serialize every change to the vault.

    The lock is an flock on ``.vault.lock``, so separate processes such as the
    CLI, kaal-server and the nightly backup exclude each other. Within one process it is also guarded
    by a thread lock, and nested use in the same thread is allowed.
    """
    vault = current_vault()
    held = vault.lock_state
    if getattr(held, "depth", 0) > 0:
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
        return
    with vault.thread_lock:
        lock_path = vault.root / VAULT_LOCK_NAME
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            held.depth = 1
            try:
                yield
            finally:
                held.depth = 0
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


vault_lock = vault_capture_lock


def holds_vault_lock(handler: Callable[[Any], None]) -> Callable[[Any], None]:
    """Run a write handler under the vault lock, after checking the vault exists."""

    @functools.wraps(handler)
    def wrapper(args: Any) -> None:
        init_if_needed()
        with vault_capture_lock():
            handler(args)

    return wrapper


INDEX_AAD = b"kaal:index"
INDEX_FORMAT = "kaal-index-encrypted-v1"


def _is_encrypted_envelope(stored: Any) -> bool:
    return isinstance(stored, dict) and isinstance(stored.get("encrypted"), dict) and "ciphertext" in stored["encrypted"]


def index_is_encrypted() -> bool:
    """Whether the index on disk is encrypted. A vault with no index yet counts as encrypted."""
    if not current_vault().index_path.exists():
        return True
    try:
        return _is_encrypted_envelope(json.loads(current_vault().index_path.read_text()))
    except (OSError, json.JSONDecodeError):
        return False


def read_index_file() -> dict[str, Any]:
    """The index exactly as stored, decrypted when it is encrypted. Nothing is upgraded."""
    stored = json.loads(current_vault().index_path.read_text())
    if _is_encrypted_envelope(stored):
        return json.loads(decrypt_payload(stored["encrypted"], aad=INDEX_AAD).decode())
    return stored


def load_index() -> dict[str, Any]:
    if not current_vault().index_path.exists():
        return {"version": 1, "created": now_iso(), "notes": []}
    try:
        index = read_index_file()
    except KaalError:
        raise
    except Exception as e:
        die(f"Could not read index: {e}")
    # Readers always see current-shape receipts; the upgrade reaches the disk
    # with the next save, which every caller makes under the vault lock.
    upgrade_receipts(index)
    return index


def save_index(index: dict[str, Any], *, encrypted: bool | None = None) -> None:
    """Write the index. It holds titles, tags, attachment names and receipt fields.

    A new vault's index is encrypted from the start. An existing index keeps
    the form it has on disk, so that an older Kaal can still read a vault
    until `kaal encrypt-all` converts it; that command passes encrypted=True.
    """
    learn_provider_names(index)
    upgrade_receipts(index)
    index["updated"] = now_iso()
    if index_is_encrypted() if encrypted is None else encrypted:
        payload = encrypt_bytes(json.dumps(index, ensure_ascii=False).encode(), aad=INDEX_AAD)
        secure_write_json(current_vault().index_path, {"format": INDEX_FORMAT, "encrypted": payload})
    else:
        secure_write_json(current_vault().index_path, index)


def write_attachment_metadata(note_id: str, attachment_meta: dict[str, Any]) -> None:
    """Keep an encrypted copy of an attachment's metadata beside it, as a second record of what it is."""
    att_id = attachment_meta["id"]
    payload = encrypt_bytes(json.dumps(attachment_meta, ensure_ascii=False).encode(), aad=f"{note_id}:{att_id}:metadata.json".encode())
    secure_write_json(attachment_note_dir(note_id) / att_id / "metadata.json", {"encrypted": payload})


def read_attachment_metadata(note_id: str, att_id: str) -> dict[str, Any]:
    """An attachment's metadata copy, whichever way it is stored."""
    stored = json.loads((attachment_note_dir(note_id) / att_id / "metadata.json").read_text())
    if _is_encrypted_envelope(stored):
        return json.loads(decrypt_payload(stored["encrypted"], aad=f"{note_id}:{att_id}:metadata.json".encode()).decode())
    return stored


def medical_people_names(index: dict[str, Any]) -> list[str]:
    """Family members that receipts can be assigned to, as the user entered them."""
    people = index.get("medical", {}).get("people", []) if isinstance(index.get("medical"), dict) else []
    return [name for name in people if isinstance(name, str) and name]


def medical_provider_names(index: dict[str, Any]) -> dict[str, str]:
    """Printed provider name (as ``receipts.name_key``) -> the wording the user chose for it."""
    names = index.get("medical", {}).get("provider_names", {}) if isinstance(index.get("medical"), dict) else {}
    return {key: value for key, value in names.items() if isinstance(key, str) and isinstance(value, str) and key and value} if isinstance(names, dict) else {}


def learn_provider_names(index: dict[str, Any]) -> None:
    """Remember how the user words a provider the model read differently.

    A receipt whose provider a person typed or confirmed, and for which the
    model read some other name, is evidence that this printed name should be
    shown the user's way. Names the user told Kaal to forget are not relearnt.
    """
    medical = index.get("medical") if isinstance(index.get("medical"), dict) else {}
    forgotten = {key for key in medical.get("provider_names_forgotten", []) if isinstance(key, str)}
    names = medical_provider_names(index)
    learnt = False
    for note in index.get("notes", []):
        if not isinstance(note, dict) or not is_medical_receipt(note) or not isinstance(note.get("receipt"), dict):
            continue
        receipt = note["receipt"]
        chosen = (receipt.get("fields") or {}).get("provider") or ""
        printed = receipts.name_key(receipt.get("model_provider") or "")
        if not chosen or not printed or (receipt.get("field_sources") or {}).get("provider") not in receipts.TRUSTED_SOURCES:
            continue
        if printed in forgotten or printed == receipts.name_key(chosen) or names.get(printed) == chosen:
            continue
        names[printed] = chosen
        learnt = True
    if learnt:
        medical["provider_names"] = names
        index["medical"] = medical


def upgrade_receipts(index: dict[str, Any]) -> bool:
    """Normalise every medical receipt record in an index; True if any changed."""
    people = medical_people_names(index)
    provider_names = medical_provider_names(index)
    changed = False
    for note in index.get("notes", []):
        if not isinstance(note, dict) or not is_medical_receipt(note):
            continue
        if not isinstance(note.get("receipt"), dict):
            note["receipt"] = {}
        changed |= receipts.upgrade(note["receipt"], reviewed="inbox" not in note.get("tags", []), people=people, provider_names=provider_names)
    return changed


def init_vault(args: argparse.Namespace) -> None:
    current_vault().root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current_vault().notes_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    current_vault().attach_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(current_vault().root, 0o700)
    os.chmod(current_vault().notes_dir, 0o700)
    os.chmod(current_vault().attach_dir, 0o700)
    provider = key_provider()
    key = get_key(create=True)
    if not current_vault().index_path.exists():
        save_index({"version": 1, "created": now_iso(), "notes": []})
    described = provider.describe()
    result = {"status": "initialized", "vault": str(current_vault().root), "key_provider": described["provider"], "key_file": described["path"]}
    result["key_sha256_prefix"] = hashlib.sha256(key).hexdigest()[:12]
    emit(result, indent=2)


def parse_tags(raw: str) -> list[str]:
    return sorted({t.strip().lstrip("#") for t in raw.split(",") if t.strip()})


def sensitivity_label(requested: str | None = "auto") -> str:
    """The label a note or a file carries.

    Everything is stored encrypted whatever the label says. It records what the
    caller stated and nothing is inferred from the text: content is labelled
    sensitive unless the caller says it is public.
    """
    wanted = (requested or "auto").lower().strip()
    if wanted in {"public", "plaintext", "plain"}:
        return "public"
    if wanted in {"auto", "sensitive", "encrypted", "encrypt"}:
        return "sensitive"
    die("sensitivity must be one of: auto, sensitive, public")


def safe_read_text(path: Path, limit: int = 1_000_000) -> str:
    data = path.read_bytes()[:limit]
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_tesseract(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    proc = tools.run([tesseract, str(path), "stdout"], text=True, timeout=60)
    return proc.stdout if proc is not None and proc.returncode == 0 else ""


def run_pdftotext(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return ""
    # Preserve column alignment: medical receipt labels and values frequently
    # share a visual row but are separated by whitespace rather than a colon.
    proc = tools.run([pdftotext, "-layout", "-l", "50", str(path), "-"], text=True, timeout=30)
    return proc.stdout if proc is not None and proc.returncode == 0 else ""


def run_pdf_ocr(path: Path, *, max_pages: int = 10) -> str:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm or not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory(prefix="kaal-pdf-ocr-") as td:
        prefix = Path(td) / "page"
        # 3000 pixels on the longest side reads as well as 300 dpi for a letter page, and a
        # page drawn absurdly large cannot turn into an absurdly large picture.
        proc = tools.run([pdftoppm, "-png", "-scale-to", "3000", "-f", "1", "-l", str(max_pages), str(path), str(prefix)], text=True, timeout=90)
        if proc is None or proc.returncode != 0:
            return ""
        return "\n\n".join(text for image in sorted(Path(td).glob("page-*.png")) if (text := run_tesseract(image)).strip())


def text_layer_is_real(text: str) -> bool:
    """Whether a PDF's own text is worth using: more than a stray header, and mostly letters and digits."""
    solid = re.sub(r"\s+", "", text or "")
    return len(solid) >= 50 and sum(char.isalnum() for char in solid) / len(solid) >= 0.5


def pdf_text(path: Path, *, ocr: bool) -> tuple[str, str]:
    """Text of a PDF as (method, text).

    A real text layer is used as it is. Whether the label rules can find
    fields in it does not matter here: reading fields is a later step. A PDF
    with no usable text layer is a scan, so it is read by OCR.
    """
    text = run_pdftotext(path)
    if text_layer_is_real(text):
        return "pdftotext-layout", text
    if ocr:
        read = run_pdf_ocr(path)
        if read.strip():
            return "tesseract-pdf", read
    return ("pdftotext-layout", text) if text.strip() else ("", "")


def extracted_plain_text(extracted_md: str) -> str:
    """The text inside an ``extracted.md`` sidecar, without its heading and code fence."""
    return re.sub(r"```(?:text)?|# Extracted text:.*|Method:.*", "", extracted_md or "", flags=re.IGNORECASE).strip()


def extract_attachment_markdown(path: Path, *, ocr: bool = False, extract: bool = True) -> str:
    """The text of a file as an ``extracted.md`` sidecar: a text file as it is, a
    PDF's own text or OCR when it is a scan, and OCR for a picture."""
    if not extract and not ocr:
        return ""
    suffix = path.suffix.lower()
    text = ""
    method = ""
    if extract and suffix in TEXT_ATTACHMENT_SUFFIXES:
        text = safe_read_text(path)
        method = "text"
    elif extract and suffix in PDF_ATTACHMENT_SUFFIXES:
        method, text = pdf_text(path, ocr=ocr)
    elif ocr and suffix in IMAGE_ATTACHMENT_SUFFIXES:
        text = run_tesseract(path)
        method = "tesseract"
    if not text.strip():
        return ""
    return f"# Extracted text: {path.name}\n\nMethod: {method}\n\n```text\n{text.strip()}\n```\n"


def attachment_container(name: str, data: bytes, payload: dict[str, str]) -> dict[str, Any]:
    """What ``original.json`` holds. The name is left out once the index is encrypted,
    since a readable name beside the ciphertext would undo that; it is kept while the
    index is still readable so an older Kaal can open the file."""
    container: dict[str, Any] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "encrypted": payload}
    return container if index_is_encrypted() else {"name": name, **container}


def attachment_original_bytes(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> bytes:
    nid = note_meta["id"]
    att_dir = attachment_note_dir(nid) / attachment_meta["id"]
    if attachment_meta.get("storage", "encrypted") == "plaintext":
        return (att_dir / attachment_meta.get("stored_name", attachment_meta["name"])).read_bytes()
    payload_path = att_dir / "original.json"
    if not payload_path.exists():
        payload_path = attachment_note_dir(nid) / f"{attachment_meta['id']}.json"
    data = json.loads(payload_path.read_text())
    # Older containers carry the attachment's name in the clear; newer ones
    # leave it to the index, which is encrypted.
    data_name = data.get("name") or attachment_meta["name"]
    aad = f"{nid}:{attachment_meta['id']}:{data_name}".encode()
    return decrypt_payload(data["encrypted"], aad=aad)


def attachment_extracted_sidecar_exists(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> bool:
    att_dir = attachment_note_dir(note_meta["id"]) / attachment_meta["id"]
    return (att_dir / "extracted.md").exists() or (att_dir / "extracted.md.json").exists()


def read_attachment_extracted_sidecar(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> str:
    """An attachment's extracted text, whichever way it is stored; "" when there is none."""
    nid, att_id = note_meta["id"], attachment_meta["id"]
    att_dir = attachment_note_dir(nid) / att_id
    encrypted = att_dir / "extracted.md.json"
    if encrypted.exists():
        payload = json.loads(encrypted.read_text())
        return decrypt_payload(payload["encrypted"], aad=f"{nid}:{att_id}:extracted.md".encode()).decode()
    plain = att_dir / "extracted.md"
    return plain.read_text(encoding="utf-8") if plain.exists() else ""


def read_attachment_docling_sidecar(note_meta: dict[str, Any], attachment_meta: dict[str, Any]) -> dict[str, Any]:
    """An attachment's stored Docling structure, whichever way it is stored; {} when there is none."""
    nid, att_id = note_meta["id"], attachment_meta["id"]
    att_dir = attachment_note_dir(nid) / att_id
    try:
        if (att_dir / "docling.json.enc").exists():
            payload = json.loads((att_dir / "docling.json.enc").read_text())
            data = json.loads(decrypt_payload(payload["encrypted"], aad=f"{nid}:{att_id}:docling.json".encode()))
        elif (att_dir / "docling.json").exists():
            data = json.loads((att_dir / "docling.json").read_text(encoding="utf-8"))
        else:
            return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_attachment_extracted_sidecar(note_meta: dict[str, Any], attachment_meta: dict[str, Any], extracted_md: str) -> None:
    nid = note_meta["id"]
    att_id = attachment_meta["id"]
    att_dir = attachment_note_dir(nid) / att_id
    att_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    sidecar_payload = encrypt_bytes(extracted_md.encode(), aad=f"{nid}:{att_id}:extracted.md".encode())
    secure_write_json(att_dir / "extracted.md.json", {"name": "extracted.md", "encrypted": sidecar_payload})
    (att_dir / "extracted.md").unlink(missing_ok=True)   # a readable copy from an older version


def read_stdin_or_arg(value: str | None, body_file: str | None = None) -> str:
    if body_file:
        return safe_read_text(Path(body_file).expanduser().resolve())
    if value is not None:
        return value
    if capturing():
        die("A body is required.")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Enter note body. Finish with Ctrl-D:", file=sys.stderr)
    return sys.stdin.read()


def note_path(note_id: str) -> Path:
    md = current_vault().notes_dir / f"{note_id}.md"
    if md.exists():
        return md
    return current_vault().notes_dir / f"{note_id}.json"


def encrypted_note_path(note_id: str) -> Path:
    return current_vault().notes_dir / f"{note_id}.json"


def plaintext_note_path(note_id: str) -> Path:
    return current_vault().notes_dir / f"{note_id}.md"


def attachment_note_dir(note_id: str) -> Path:
    return current_vault().attach_dir / note_id


def find_notes(query: str) -> list[dict[str, Any]]:
    index = load_index()
    notes = index.get("notes", [])
    by_id = [n for n in notes if n.get("id") == query]
    if by_id:
        return by_id
    q = query.lower()
    matches = [n for n in notes if q in n.get("title", "").lower()]
    if len(matches) == 1:
        return matches
    exact_title = [n for n in notes if n.get("title", "").lower() == q]
    return exact_title or matches


def require_one_note(query: str) -> dict[str, Any]:
    matches = find_notes(query)
    if not matches:
        die(f"No note found matching: {query}", kind="not_found")
    if len(matches) > 1:
        if not capturing():
            print_ambiguous_matches(matches)
        die("Use the note id to disambiguate.", kind="conflict", details={"matches": [{"id": n["id"], "title": n.get("title", "")} for n in matches]})
    return matches[0]


def print_ambiguous_matches(matches: list[dict[str, Any]]) -> None:
    print("Multiple notes match:", file=sys.stderr)
    for n in matches:
        print(f"  {n['id']}  {n.get('title','')}", file=sys.stderr)


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
    label = sensitivity_label(sensitivity)
    note_obj = {
        "id": note_id,
        "title": title,
        "tags": note_tags,
        "created": ts,
        "updated": ts,
        "body": body,
        "format": "markdown",
        "sensitivity": label,
        "storage": "encrypted",
    }
    if source:
        note_obj["source"] = source
    payload = encrypt_bytes(json.dumps(note_obj, ensure_ascii=False).encode(), aad=note_id.encode())
    secure_write_json(encrypted_note_path(note_id), payload)
    meta = {
        "id": note_id,
        "title": title,
        "tags": note_tags,
        "created": ts,
        "updated": ts,
        "attachments": [],
        "format": "markdown",
        "sensitivity": label,
        "storage": "encrypted",
    }
    if source:
        meta["source"] = source
    index = load_index()
    index.setdefault("notes", []).append(meta)
    save_index(index)
    return meta


@holds_vault_lock
def add_note(args: argparse.Namespace) -> None:
    init_if_needed()
    body = read_stdin_or_arg(args.body, getattr(args, "body_file", None))
    meta = create_note(args.title, body, tags=parse_tags(args.tags), sensitivity=getattr(args, "sensitivity", "auto"))
    emit({
        "status": "created",
        "id": meta["id"],
        "title": meta["title"],
        "tags": meta["tags"],
        "format": "markdown",
        "sensitivity": meta["sensitivity"],
        "storage": meta["storage"],
    }, indent=2, ensure_ascii=False)


def init_if_needed() -> None:
    if not current_vault().root.exists() or not current_vault().index_path.exists():
        die("Vault not initialized. Run: kaal init", kind="unavailable")


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
    if args.json or capturing():
        emit(notes, indent=2, ensure_ascii=False)
        return
    print_note_lines(notes)


def print_note_lines(notes: list[dict[str, Any]]) -> None:
    for n in notes:
        tags = " ".join(f"#{t}" for t in n.get("tags", []))
        att = len(n.get("attachments", []))
        print(f"{n['id']}  {n.get('updated','')}  {n.get('title','')}  {tags}  attachments:{att}")


def is_medical_receipt(meta: dict[str, Any]) -> bool:
    tags = meta.get("tags", [])
    return "medical" in tags and "receipt" in tags


def library_note_meta(index: dict[str, Any], note_id: str, *, allow_trashed: bool = True) -> dict[str, Any]:
    for note in index.get("notes", []):
        if note.get("id") != note_id:
            continue
        if is_medical_receipt(note):
            die("Medical receipts are managed from the Medical Receipts dashboard.")
        if not allow_trashed and note.get("trashed_at"):
            die("Restore this Library note before changing it.")
        return note
    die("Library note not found.", kind="not_found")


def library_attachment_summary(attachment: dict[str, Any]) -> dict[str, Any]:
    return {key: attachment.get(key) for key in ("id", "name", "stored_name", "size", "storage", "sensitivity", "extracted_markdown", "extracted_at", "extraction_attempted_at", "source") if key in attachment}


def library_note_summary(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: meta.get(key)
        for key in ("id", "title", "tags", "created", "updated", "storage", "sensitivity", "source", "trashed_at")
        if key in meta
    } | {"attachments": [library_attachment_summary(att) for att in meta.get("attachments", [])]}


def library_list(args: argparse.Namespace) -> None:
    """Emit bounded generic-note metadata for the local dashboard."""
    init_if_needed()
    limit = max(0, min(int(getattr(args, "limit", 100) or 100), 100))
    trash = bool(getattr(args, "trash", False))
    notes = [note for note in load_index().get("notes", []) if not is_medical_receipt(note) and bool(note.get("trashed_at")) == trash]
    query = (getattr(args, "q", "") or "").strip().casefold()
    if query:
        notes = [
            note for note in notes
            if query in " ".join([note.get("id", ""), note.get("title", ""), *note.get("tags", []), (note.get("source") or {}).get("title", "") or ""]).casefold()
        ]
    notes.sort(key=lambda note: note.get("updated", note.get("created", "")), reverse=True)
    notes, paging = page_of(notes, SimpleNamespace(offset=getattr(args, "offset", 0), limit=limit))
    emit([library_note_summary(note) for note in notes], ensure_ascii=False, meta=paging)


# --- searching inside notes
#
# Every note and every text read from a file is encrypted on disk, so finding a
# word means decrypting them. That is done once per note and kept in the
# vault object's memory (Vault.text_cache), keyed by what would change if the note or its files did.
# Nothing here is written to disk.
SEARCH_PIECE_LIMIT = 400_000   # characters of one note or one file's text that are searched


def search_memory_chars() -> int:
    """How much decrypted text one vault keeps in memory for searching (KAAL_SEARCH_MEMORY_MB, 256).
    Beyond it the text read longest ago is let go, and read again when it is next searched."""
    try:
        return max(1, int(os.environ.get("KAAL_SEARCH_MEMORY_MB") or 256)) * 1024 * 1024
    except ValueError:
        return 256 * 1024 * 1024


def _forget_text(vault: Any, note_id: str) -> None:
    if vault.text_cache.pop(note_id, None) is not None:
        vault.text_total -= vault.text_sizes.pop(note_id, 0)


def _remember_text(vault: Any, note_id: str, stamp: Any, pieces: list[tuple[str, str, str]]) -> None:
    _forget_text(vault, note_id)
    size = sum(len(text) for _where, _name, text in pieces)
    vault.text_cache[note_id] = (stamp, pieces)
    vault.text_sizes[note_id] = size
    vault.text_total += size
    limit = search_memory_chars()
    while vault.text_total > limit and len(vault.text_cache) > 1:
        _forget_text(vault, next(iter(vault.text_cache)))
SNIPPET_BEFORE, SNIPPET_AFTER = 60, 110


def _file_stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _note_text_stamp(meta: dict[str, Any]) -> tuple[Any, ...]:
    """Changes whenever the note's text or a file's text does. The files on disk are
    looked at, not only the index: two edits in one second share an ``updated`` time."""
    nid = meta["id"]
    note_file = plaintext_note_path(nid) if meta.get("storage", "encrypted") == "plaintext" else encrypted_note_path(nid)
    files = tuple(
        (att.get("id"), _file_stamp(attachment_note_dir(nid) / att["id"] / "extracted.md.json") or _file_stamp(attachment_note_dir(nid) / att["id"] / "extracted.md"))
        for att in meta.get("attachments", []) if att.get("id"))
    return (meta.get("updated", ""), _file_stamp(note_file), files)


def note_text_pieces(meta: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The searchable text of one note as (where, file name, text): its body, then the text read from each file."""
    stamp = _note_text_stamp(meta)
    cached = current_vault().text_cache.get(meta["id"])
    if cached and cached[0] == stamp:
        return cached[1]
    pieces: list[tuple[str, str, str]] = []
    try:
        body = load_note(meta).get("body", "")
    except (KaalError, OSError, ValueError):
        body = ""   # a note that cannot be read is simply not found by its text
    if body.strip():
        pieces.append(("note", "", body[:SEARCH_PIECE_LIMIT]))
    for att in meta.get("attachments", []):
        try:
            text = extracted_plain_text(read_attachment_extracted_sidecar(meta, att))
        except (KaalError, OSError, ValueError, KeyError):
            text = ""
        if text:
            pieces.append(("file", att.get("name") or "file", text[:SEARCH_PIECE_LIMIT]))
    _remember_text(current_vault(), meta["id"], stamp, pieces)
    return pieces


def search_terms(query: str) -> list[str]:
    """Words that must all be found; "quoted words" stay together."""
    terms = [quoted or word for quoted, word in re.findall(r'"([^"]+)"|(\S+)', query or "")]
    terms = [term.strip().strip('"').strip() for term in terms]   # a stray quote mark is not a word
    return [term for term in terms if term][:10]


def _snippet(text: str, found: re.Match[str]) -> dict[str, str]:
    start, end = max(0, found.start() - SNIPPET_BEFORE), min(len(text), found.end() + SNIPPET_AFTER)
    clean = lambda part: re.sub(r"\s+", " ", part)
    return {
        "before": ("…" if start else "") + clean(text[start:found.start()]).lstrip(),
        "found": clean(found.group(0)),
        "after": clean(text[found.end():end]).rstrip() + ("…" if end < len(text) else ""),
    }


def library_search(args: argparse.Namespace) -> None:
    """Find notes by their title, tags, text, or the text read from their files.

    Every word must appear somewhere in the note. Notes found by title or tag
    come first, then by their own text, then by a file's text.
    """
    init_if_needed()
    terms = search_terms(getattr(args, "query", "") or "")
    if not terms:
        die("Enter something to search for.")
    trash = bool(getattr(args, "trash", False))
    limit = max(1, min(int(getattr(args, "limit", 50) or 50), 100))
    index = load_index()
    notes = index.get("notes", [])
    cache = current_vault().text_cache
    for gone in set(cache) - {note["id"] for note in notes}:
        _forget_text(current_vault(), gone)
    patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in terms]
    hits: list[tuple[int, str, dict[str, Any]]] = []
    for note in notes:
        if is_medical_receipt(note) or bool(note.get("trashed_at")) != trash:
            continue
        head = " ".join([note.get("title", ""), *note.get("tags", []), (note.get("source") or {}).get("title", "") or ""])
        pieces: list[tuple[str, str, str]] | None = None
        match: dict[str, Any] | None = None
        for pattern in patterns:
            if pattern.search(head):
                continue
            if pieces is None:
                pieces = note_text_pieces(note)
            found = next(((where, name, text, hit) for where, name, text in pieces if (hit := pattern.search(text))), None)
            if found is None:
                break
            if match is None or (match["where"] == "file" and found[0] == "note"):
                match = {"where": found[0], **({"file": found[1]} if found[0] == "file" else {}), **_snippet(found[2], found[3])}
        else:
            match = match or {"where": "title"}
            rank = {"title": 0, "note": 1, "file": 2}[match["where"]]
            hits.append((rank, note.get("updated", note.get("created", "")), library_note_summary(note) | {"match": match}))
    hits.sort(key=lambda hit: hit[1], reverse=True)
    hits.sort(key=lambda hit: hit[0])
    page, paging = page_of([hit[2] for hit in hits], SimpleNamespace(offset=getattr(args, "offset", 0), limit=limit))
    emit(page, ensure_ascii=False, meta=paging)


def library_show(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = library_note_meta(load_index(), args.note_id)
    note = load_note(meta)
    emit({"status": "ok", "note": {**library_note_summary(meta), "body": note.get("body", "")}}, ensure_ascii=False)


def library_create(args: argparse.Namespace) -> None:
    init_if_needed()
    body = read_stdin_or_arg(getattr(args, "body", None))
    source_url = sanitized_capture_url(getattr(args, "source_url", "") or "")
    source_title = (getattr(args, "source_title", "") or "").strip()
    source = {"type": "library"}
    if source_url:
        source["url"] = source_url
    if source_title:
        source["title"] = source_title
    with vault_capture_lock():
        meta = create_note(args.title.strip(), body, tags=parse_tags(args.tags), sensitivity="auto", source=source if len(source) > 1 else None)
    emit({"status": "created", "note": library_note_summary(meta)}, ensure_ascii=False)


def write_library_note(meta: dict[str, Any], note: dict[str, Any]) -> None:
    """Persist an edited note, encrypted. Its label stays what it was."""
    note["storage"] = "encrypted"
    note["sensitivity"] = meta.get("sensitivity") or "sensitive"
    note.pop("classification", None)
    plaintext_note_path(note["id"]).unlink(missing_ok=True)   # a readable copy from an older version
    secure_write_json(encrypted_note_path(note["id"]), encrypt_bytes(json.dumps(note, ensure_ascii=False).encode(), aad=note["id"].encode()))
    for key in ("title", "tags", "updated", "storage", "sensitivity"):
        meta[key] = note[key]
    meta.pop("classification", None)


def library_update(args: argparse.Namespace) -> None:
    init_if_needed()
    body = read_stdin_or_arg(getattr(args, "body", None))
    if not body.strip():
        die("Refusing to save an empty note.")
    with vault_capture_lock():
        index = load_index()
        meta = library_note_meta(index, args.note_id, allow_trashed=False)
        note = load_note(meta)
        note["title"] = args.title.strip()
        note["tags"] = parse_tags(args.tags)
        note["body"] = body
        note["updated"] = now_iso()
        write_library_note(meta, note)
        save_index(index)
    emit({"status": "updated", "note": library_note_summary(meta)}, ensure_ascii=False)


def library_trash(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = library_note_meta(index, args.note_id, allow_trashed=True)
        if meta.get("trashed_at"):
            die("Library note is already in trash.")
        meta["trashed_at"] = now_iso()
        meta["updated"] = meta["trashed_at"]
        save_index(index)
    emit({"status": "trashed", "id": meta["id"], "title": meta["title"]}, ensure_ascii=False)


def library_restore(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = library_note_meta(index, args.note_id, allow_trashed=True)
        if not meta.get("trashed_at"):
            die("Library note is not in trash.")
        meta.pop("trashed_at", None)
        meta["updated"] = now_iso()
        save_index(index)
    emit({"status": "restored", "id": meta["id"], "title": meta["title"]}, ensure_ascii=False)


def library_purge(args: argparse.Namespace) -> None:
    init_if_needed()
    if not getattr(args, "yes", False):
        die("Permanent Library deletion requires --yes.")
    with vault_capture_lock():
        index = load_index()
        meta = library_note_meta(index, args.note_id, allow_trashed=True)
        if not meta.get("trashed_at"):
            die("Move the Library note to trash before permanently deleting it.")
        note_id = meta["id"]
        purge_dir = current_vault().root / ".library-purge" / note_id
        moves = [(plaintext_note_path(note_id), purge_dir / "note.md"), (encrypted_note_path(note_id), purge_dir / "note.enc.json"), (attachment_note_dir(note_id), purge_dir / "attachments")]
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
            shutil.rmtree(purge_dir, ignore_errors=True)
            raise
        try:
            shutil.rmtree(purge_dir, ignore_errors=False)
        except Exception as cleanup_error:
            for source, staged in reversed(moved):
                if staged.exists():
                    os.replace(staged, source)
            index["notes"].append(meta)
            save_index(index)
            die(f"Permanent deletion could not be completed; Library note was restored to trash: {cleanup_error}")
        try:
            purge_dir.parent.rmdir()
        except OSError:
            pass
    emit({"status": "purged", "id": note_id, "title": meta["title"]}, ensure_ascii=False)


def library_attach(args: argparse.Namespace) -> None:
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"Attachment file not found: {src}")
    with vault_capture_lock():
        index = load_index()
        meta = library_note_meta(index, args.note_id, allow_trashed=False)
        attachment = attach_file_to_note(meta, src, sensitivity="auto", extract=True, ocr=True)
    emit({"status": "attached", "id": meta["id"], "attachment": library_attachment_summary(attachment)}, ensure_ascii=False)


def library_extracted_text(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = library_note_meta(load_index(), args.note_id)
    attachment_id = getattr(args, "attachment_id", "") or ""
    matches = [att for att in meta.get("attachments", []) if att.get("id") == attachment_id]
    if len(matches) != 1:
        die("Specify one Library attachment id.")
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
    emit({"status": "ok", "id": meta["id"], "attachment_id": att["id"], "text": text}, ensure_ascii=False)


def library_extract(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        meta = library_note_meta(load_index(), args.note_id, allow_trashed=False)
    # Reuse Kaal's generic attachment pipeline. It never selects the medical
    # PDF route because the library predicate excludes medical receipt records.
    ocr_attachments(SimpleNamespace(query=meta["id"], force=True, dry_run=False, no_extract=False, no_ocr=False, limit=0))


def show_note(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    note = load_note(meta)
    body = note.get("body", "")
    if not args.full:
        body = redact_text(body)
    out = {**{k: note.get(k) for k in ["id", "title", "tags", "created", "updated"]}, "body": body, "redacted": not args.full, "attachments": meta.get("attachments", [])}
    if args.json or capturing():
        emit(out, indent=2, ensure_ascii=False)
    else:
        print_note_text(out)


def print_note_text(out: dict[str, Any]) -> None:
    print(f"# {out.get('title') or ''} ({out.get('id') or ''})")
    if out.get("tags"):
        print("Tags: " + ", ".join(out["tags"]))
    if out.get("attachments"):
        print("Attachments: " + ", ".join(a.get("name", "") for a in out["attachments"]))
    print()
    print(out.get("body", ""))
    if out.get("redacted"):
        print("\n[redacted output; use --full only when you explicitly need plaintext]", file=sys.stderr)


def search_notes(args: argparse.Namespace) -> None:
    init_if_needed()
    q = args.query.lower()
    notes = load_index().get("notes", [])
    results = [n for n in notes if q in n.get("title", "").lower() or any(q in t.lower() for t in n.get("tags", []))]
    emit(results, indent=2, ensure_ascii=False)


@holds_vault_lock
def append_note(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    note = load_note(meta)
    extra = read_stdin_or_arg(args.body)
    if not extra.strip():
        die("No append text provided.")
    note["body"] = note.get("body", "").rstrip() + "\n\n" + extra.strip() + "\n"
    note["updated"] = now_iso()
    note["storage"] = "encrypted"
    note["sensitivity"] = meta.get("sensitivity") or "sensitive"
    note.pop("classification", None)
    plaintext_note_path(note["id"]).unlink(missing_ok=True)   # a readable copy from an older version
    secure_write_json(encrypted_note_path(note["id"]), encrypt_bytes(json.dumps(note, ensure_ascii=False).encode(), aad=note["id"].encode()))
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == note["id"]:
            n["updated"] = note["updated"]
            n["storage"] = "encrypted"
            n["sensitivity"] = note["sensitivity"]
            n.pop("classification", None)
    save_index(index)
    emit({"status": "appended", "id": note["id"], "title": note.get("title", ""), "storage": "encrypted", "sensitivity": note["sensitivity"]}, indent=2, ensure_ascii=False)


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


def note_field_value(query: str, field: str) -> tuple[str, str]:
    """Return (note id, value) for a ``field: value`` line in one note."""
    init_if_needed()
    meta = require_one_note(query)
    note = load_note(meta)
    return note.get("id") or meta["id"], extract_field(note.get("body", ""), field)


def copy_field(args: argparse.Namespace) -> None:
    note_id, value = note_field_value(args.query, args.field)
    pbcopy = shutil.which("pbcopy")
    if not pbcopy:
        die("pbcopy not found; cannot copy to clipboard.")
    subprocess.run([pbcopy], input=value, text=True, check=True)
    emit({"status": "copied", "note": note_id, "field": args.field, "characters": len(value)}, indent=2)


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


def attach_file_to_note(meta: dict[str, Any], src: Path, *, sensitivity: str = "auto", extract: bool = False, ocr: bool = False, attachment_name: str | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    data = src.read_bytes()
    stored_name = attachment_name or src.name
    nid = meta["id"]
    att_id = secrets.token_hex(6)
    disk_name = safe_attachment_filename(stored_name, att_id)
    extracted_md = extract_attachment_markdown(src, ocr=ocr, extract=extract)
    # A file on a note labelled sensitive carries the same label.
    label = "sensitive" if meta.get("sensitivity") == "sensitive" else sensitivity_label(sensitivity)
    # Create the per-note directory explicitly: mkdir(parents=True) gives
    # intermediate directories the default mode (755), not 700.
    current_vault().attach_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    attachment_note_dir(nid).mkdir(mode=0o700, exist_ok=True)
    out_dir = attachment_note_dir(nid) / att_id
    out_dir.mkdir(mode=0o700, exist_ok=True)
    attachment_meta = {
        "id": att_id,
        "name": stored_name,
        "stored_name": disk_name,
        "size": len(data),
        "created": now_iso(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "storage": "encrypted",
        "sensitivity": label,
        "extracted_markdown": bool(extracted_md),
    }
    if source:
        attachment_meta["source"] = source
    payload = encrypt_bytes(data, aad=f"{nid}:{att_id}:{stored_name}".encode())
    secure_write_json(out_dir / "original.json", attachment_container(stored_name, data, payload))
    if extracted_md:
        write_attachment_extracted_sidecar(meta, attachment_meta, extracted_md)
    write_attachment_metadata(nid, attachment_meta)
    index = load_index()
    for n in index.get("notes", []):
        if n["id"] == nid:
            n.setdefault("attachments", []).append(attachment_meta)
            n["updated"] = now_iso()
            meta = n
    save_index(index)
    return attachment_meta


@holds_vault_lock
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
    emit({
        "status": "attached",
        "note": meta["id"],
        "attachmentId": attachment_meta["id"],
        "name": attachment_meta["name"],
        "size": attachment_meta["size"],
        "storage": attachment_meta["storage"],
        "sensitivity": attachment_meta["sensitivity"],
        "extracted_markdown": attachment_meta["extracted_markdown"],
    }, indent=2, ensure_ascii=False)


def match_attachment(meta: dict[str, Any], query: str) -> dict[str, Any]:
    """Find one attachment of a note by exact id, else by unique name substring."""
    attachments = meta.get("attachments", [])
    by_id = [a for a in attachments if a.get("id") == query]
    if by_id:
        return by_id[0]
    matches = [a for a in attachments if query.lower() in a.get("name", "").lower()]
    if not matches:
        die(f"No attachment matching: {query}", kind="not_found")
    if len(matches) > 1:
        die("Multiple attachments match; use attachment id.", kind="conflict")
    return matches[0]


def export_attachment(args: argparse.Namespace) -> None:
    init_if_needed()
    meta = require_one_note(args.query)
    nid = meta["id"]
    att = match_attachment(meta, args.attachment)
    plaintext = attachment_original_bytes(meta, att)
    out = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / att.get("name", "attachment")
    if out.exists() and not args.force:
        die(f"Output exists: {out}; pass --force to overwrite.")
    out.write_bytes(plaintext)
    os.chmod(out, 0o600)
    emit({"status": "exported", "path": str(out), "bytes": len(plaintext), "sha256": hashlib.sha256(plaintext).hexdigest()}, indent=2)


@holds_vault_lock
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
                    write_attachment_metadata(note["id"], att)
                skipped_existing += 1
                continue
            if limit and eligible >= limit:
                continue
            eligible += 1
            if dry_run:
                continue
            is_medical_receipt = "medical" in note.get("tags", []) and "receipt" in note.get("tags", [])
            try:
                with tempfile.TemporaryDirectory(prefix="kaal-ocr-") as td:
                    os.chmod(td, 0o700)
                    tmp = Path(td) / (att.get("stored_name") or att.get("name") or f"attachment-{att['id']}")
                    tmp.write_bytes(attachment_original_bytes(note, att))
                    os.chmod(tmp, 0o600)
                    extracted_md = extract_attachment_markdown(tmp, ocr=ocr, extract=extract)
                    ai_fields, ai_info = receipt_ai_candidates(tmp, extracted_md) if is_medical_receipt else ({}, {})
                if extracted_md:
                    write_attachment_extracted_sidecar(note, att, extracted_md)
                    att["extracted_markdown"] = True
                    att["extracted_at"] = now_iso()
                    extracted += 1
                else:
                    att["extracted_markdown"] = False
                    att["extraction_attempted_at"] = now_iso()
                    no_text += 1
                if "medical" in note.get("tags", []) and "receipt" in note.get("tags", []):
                    prior_receipt = note.get("receipt", {})
                    note["receipt"] = build_receipt_state(extracted_md, prior_receipt, ai_fields=ai_fields, ai_info=ai_info)
                write_attachment_metadata(note["id"], att)
            except Exception as e:
                if "medical" in note.get("tags", []) and "receipt" in note.get("tags", []):
                    # A failed re-extraction must not cost the user what they typed or confirmed.
                    failed = build_receipt_state("", note.get("receipt", {}))
                    failed["state"], failed["error"] = "error", str(e)[:500]
                    note["receipt"] = failed
                errors.append({"note_id": note.get("id", ""), "attachment_id": att.get("id", ""), "name": att.get("name", ""), "error": str(e)})
    if not dry_run and (extracted or no_text or skipped_existing or errors):
        save_index(index)
    emit({
        "status": "dry-run" if dry_run else "updated",
        "scanned_attachments": scanned,
        "eligible_attachments": eligible,
        "extracted_attachments": extracted,
        "no_text_attachments": no_text,
        "skipped_existing": skipped_existing,
        "skipped_unsupported": skipped_unsupported,
        "errors": errors,
    }, indent=2, ensure_ascii=False)


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
    amount = re.search(r"(?:amount\s+paid|total\s+paid|payment\s+amount|payment\s+received|paid\s+today|amount\s+collected|you\s+paid)\s*[:#-]?\s*\$?([0-9][0-9,]*\.[0-9]{2})\b", text, flags=re.IGNORECASE)
    if not amount and approved_card_sale:
        amount = re.search(r"\btotal\s+amount\s*[:#-]?\s*\$([0-9][0-9,]*\.[0-9]{2})", text, flags=re.IGNORECASE)
    fields["amount"] = amount.group(1).replace(",", "") if amount else ""
    fields["currency"] = "USD" if "$" in text else ""
    return fields


def receipt_extraction_state(extracted_text: str = "", error: str = "") -> dict[str, Any]:
    """Candidates from the label rules alone: explicit receipt evidence, never semantic guesses."""
    fields = infer_receipt_fields(extracted_text) if extracted_text else {}
    sources: dict[str, str] = {key: "labelled-text" for key, value in fields.items() if value}
    return {"state": "error" if error else "extracted" if extracted_text.strip() else "no_text", "attempted_at": now_iso(), "error": error[:500], "fields": fields, "field_sources": sources}


def receipt_ai_candidates(path: Path, extracted_md: str) -> tuple[dict[str, str], dict[str, str]]:
    """Ask the configured model for a receipt's fields.

    Returns (candidates, info). With the model off, both are empty. When the
    model cannot be asked or fails, candidates are empty and info names the
    kind of failure, so extraction carries on with the label rules alone.
    """
    try:
        settings = receipt_ai.settings_from_env()
        if not settings.enabled:
            return {}, {}
        info = {"backend": settings.backend, "model": settings.model}
        return receipt_ai.extract(extracted_plain_text(extracted_md), receipt_ai.receipt_images(path), settings), info
    except receipt_ai.ReceiptAIError as error:
        return {}, {"backend": os.environ.get("KAAL_RECEIPT_AI", "") or "off", "error": error.kind}


def build_receipt_state(extracted_text: str, prior_receipt: dict[str, Any] | None = None, *, ai_fields: dict[str, str] | None = None, ai_info: dict[str, str] | None = None) -> dict[str, Any]:
    """Use one conservative field path for capture and re-extraction.

    Text/layout candidates are suggestions. Only values a person typed or
    confirmed survive re-extraction, so a stale automatic result cannot become a
    durable fact. Reimbursement is the user's record and always survives.
    """
    receipt = receipt_extraction_state(extracted_text)
    if ai_fields:
        if receipt["state"] == "no_text":
            receipt["state"] = "extracted"
        conflicts = receipts.merge_model_fields(receipt["fields"], receipt["field_sources"], ai_fields, extracted_plain_text(extracted_text), receipt.setdefault("field_raw", {}))
        if conflicts:
            receipt["conflicts"] = conflicts
    if ai_info:
        receipt["ai"] = dict(ai_info)
    prior = prior_receipt or {}
    # What the model read as the provider, kept even when a person's wording is
    # shown instead: it is how Kaal learns the user's name for this provider.
    model_provider = ((ai_fields or {}).get("provider") or "").strip()
    if model_provider and not re.match(r"(?:dr\.?|doctor)\s+", model_provider, flags=re.IGNORECASE):
        receipt["model_provider"] = model_provider[:200]
    elif prior.get("model_provider"):
        receipt["model_provider"] = prior["model_provider"]
    prior_fields = prior.get("fields", {})
    prior_sources = prior.get("field_sources", {})
    prior_raw = prior.get("field_raw", {})
    for field, source in prior_sources.items():
        if source in {"manual", "confirmed"} and prior_fields.get(field):
            receipt["fields"][field] = prior_fields[field]
            receipt["field_sources"][field] = source
            receipt.get("conflicts", {}).pop(field, None)
            if prior_raw.get(field):
                receipt.setdefault("field_raw", {})[field] = prior_raw[field]
    for key in ("manually_updated_at", "reimbursed_at", "reimbursement_note"):
        if prior.get(key):
            receipt[key] = prior[key]
    if not receipt.get("conflicts"):
        receipt.pop("conflicts", None)
    return receipt


def receipt_confirmation_issues(receipt: dict[str, Any]) -> list[str]:
    fields = receipt.get("fields", {})
    issues = []
    if not fields.get("amount"):
        issues.append("amount")
    if not fields.get("service_date") and not fields.get("paid_date"):
        issues.append("date")
    return issues


def sanitized_capture_url(value: str) -> str:
    """Keep useful browser provenance without persisting transient URL secrets."""
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    # Web addresses only, and none with a backslash, which a browser and this code read differently.
    if parsed.scheme.lower() not in ("http", "https") or "\\" in value:
        return ""
    # No query, fragment or user name and password: those can hold one-time secrets. Nor what
    # follows a ";" in a part of the path, where some sites carry a session.
    try:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return ""
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    path = "/".join(part.split(";", 1)[0] for part in parsed.path.split("/"))
    return urlunsplit((parsed.scheme.lower(), f"{host}{port}", path, "", ""))


def browser_capture(args: argparse.Namespace) -> None:
    """Capture a browser artifact as a normal Kaal note and attachment.

    Unlike ``medical_capture``, this does not add medical receipt metadata or
    lifecycle tags.
    """
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"Capture file not found: {src}")
    source_url = sanitized_capture_url(getattr(args, "source_url", "") or "")
    source_title = (getattr(args, "source_title", "") or "").strip()
    captured_at = (getattr(args, "captured_at", "") or "").strip() or now_iso()
    title = (getattr(args, "title", "") or "").strip() or source_title or src.stem.replace("_", " ").replace("-", " ")
    source: dict[str, Any] = {"type": "browser-capture", "captured_at": captured_at}
    if source_url:
        source["url"] = source_url
    if source_title:
        source["title"] = source_title
    body_lines = [
        "# Browser capture",
        "",
        f"Captured at: {captured_at}",
        f"Original file: {src.name}",
    ]
    if source_title:
        body_lines.append(f"Source title: {source_title}")
    if source_url:
        body_lines.append(f"Source URL: {source_url}")
    body_lines.extend(["", "Captured from the browser.", ""])
    with vault_capture_lock():
        meta = create_note(title, "\n".join(body_lines), tags=["browser-capture"], sensitivity="auto", source=source)
        try:
            attachment = attach_file_to_note(
                meta,
                src,
                sensitivity="auto",
                extract=True,
                ocr=True,
                source=source,
            )
        except Exception:
            # Keep the browser source intact and avoid an empty generic note if
            # attachment persistence or extraction fails.
            plaintext_note_path(meta["id"]).unlink(missing_ok=True)
            encrypted_note_path(meta["id"]).unlink(missing_ok=True)
            shutil.rmtree(attachment_note_dir(meta["id"]), ignore_errors=True)
            index = load_index()
            index["notes"] = [note for note in index.get("notes", []) if note.get("id") != meta["id"]]
            save_index(index)
            raise
    emit({
        "status": "captured",
        "id": meta["id"],
        "title": meta["title"],
        "storage": meta["storage"],
        "sensitivity": meta["sensitivity"],
        "attachment_id": attachment["id"],
        "attachment_name": attachment["name"],
        "extracted_markdown": attachment["extracted_markdown"],
    }, indent=2, ensure_ascii=False)


def browser_capture_text(args: argparse.Namespace) -> None:
    """Store browser-selected text as a normal Kaal note read from stdin."""
    init_if_needed()
    max_chars = 100_000
    given = getattr(args, "text", None)
    if given is None and capturing():
        die("Selected browser text is required")
    selected_text = (given if given is not None else sys.stdin.read(max_chars + 1)).strip()
    if not selected_text:
        die("Selected browser text is required")
    if len(selected_text) > max_chars:
        die(f"Selected browser text exceeds the {max_chars:,}-character limit")
    source_url = sanitized_capture_url(getattr(args, "source_url", "") or "")
    source_title = (getattr(args, "source_title", "") or "").strip()
    captured_at = (getattr(args, "captured_at", "") or "").strip() or now_iso()
    title = (getattr(args, "title", "") or "").strip() or source_title or "Browser selection"
    source: dict[str, Any] = {"type": "browser-selection", "captured_at": captured_at}
    if source_url:
        source["url"] = source_url
    if source_title:
        source["title"] = source_title
    body_lines = [
        "# Browser selection",
        "",
        f"Captured at: {captured_at}",
    ]
    if source_title:
        body_lines.append(f"Source title: {source_title}")
    if source_url:
        body_lines.append(f"Source URL: {source_url}")
    body_lines.extend(["", "## Selected text", "", selected_text, ""])
    with vault_capture_lock():
        meta = create_note(title, "\n".join(body_lines), tags=["browser-capture", "browser-selection"], sensitivity="auto", source=source)
    emit({
        "status": "captured",
        "type": "browser-selection",
        "id": meta["id"],
        "title": meta["title"],
        "storage": meta["storage"],
        "sensitivity": meta["sensitivity"],
    }, indent=2, ensure_ascii=False)


def medical_capture(args: argparse.Namespace) -> None:
    """Create a medical-receipt inbox record from a file, encrypted like everything else."""
    init_if_needed()
    src = Path(args.file).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"Receipt file not found: {src}")
    source_url = sanitized_capture_url(getattr(args, "source_url", "") or "")
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
    # With ``defer``, capture only stores the original; reading it happens
    # afterwards, outside the vault lock (see reextract_receipt).
    defer = bool(getattr(args, "defer", False))
    with vault_capture_lock():
        meta = create_note(title, "\n".join(body_lines), tags=["medical", "receipt", "inbox"], source=source)
        try:
            attachment = attach_file_to_note(
                meta,
                src,
                extract=not defer,
                ocr=not defer,
                source=source,
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
        extracted_text = read_attachment_extracted_sidecar(meta, attachment)
        if defer:
            saved["receipt"] = {"state": receipts.STATE_PENDING, "attempted_at": now_iso(), "fields": {}, "field_sources": {}}
        else:
            ai_fields, ai_info = receipt_ai_candidates(src, extracted_text)
            saved["receipt"] = build_receipt_state(extracted_text, ai_fields=ai_fields, ai_info=ai_info)
        saved["updated"] = now_iso()
        save_index(index)
    emit({
        "status": "captured",
        "id": meta["id"],
        "title": meta["title"],
        "storage": meta["storage"],
        "sensitivity": meta["sensitivity"],
        "attachment_id": attachment["id"],
        "attachment_name": attachment["name"],
        "extracted_markdown": attachment["extracted_markdown"],
        "extraction": "pending" if defer else "done",
    }, indent=2, ensure_ascii=False)


def pending_receipt_ids() -> list[str]:
    """Active receipts whose original is stored but has not been read yet."""
    return [
        note["id"] for note in load_index().get("notes", [])
        if is_medical_receipt(note) and not note.get("trashed_at") and note.get("receipt", {}).get("state") == receipts.STATE_PENDING
    ]


def reextract_receipt(note_id: str, outer_lock: Any = None) -> str:
    """Read one stored receipt again and save the result; "done", "error" or "gone".

    The slow part (text, OCR, the model) runs without any lock, on a private
    copy of the original. The vault lock is taken only to write the result, and
    the receipt is read afresh at that point, so anything a person changed in
    the meantime is kept. ``outer_lock`` is a lock the caller's other writers
    hold around vault changes (kaal-server's request lock).
    """
    note = next((item for item in load_index().get("notes", []) if item.get("id") == note_id and is_medical_receipt(item)), None)
    attachment = (note.get("attachments") or [None])[0] if note else None
    extracted_md, ai_fields, ai_info, failure = "", {}, {}, ""
    if note is not None and attachment is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="kaal-receipt-") as directory:
                os.chmod(directory, 0o700)
                copy = Path(directory) / (attachment.get("stored_name") or attachment.get("name") or f"attachment-{attachment['id']}")
                copy.write_bytes(attachment_original_bytes(note, attachment))
                os.chmod(copy, 0o600)
                extracted_md = extract_attachment_markdown(copy, ocr=True, extract=True)
                ai_fields, ai_info = receipt_ai_candidates(copy, extracted_md)
        except tools.Busy:
            # Never got a turn at reading it: that says nothing about the receipt, which stays waiting.
            raise
        except (Exception, KaalError) as error:
            failure = type(error).__name__
    with (outer_lock or nullcontext()), vault_capture_lock():
        index = load_index()
        current = next((item for item in index.get("notes", []) if item.get("id") == note_id and is_medical_receipt(item)), None)
        if current is None:
            return "gone"
        stored = next((item for item in current.get("attachments", []) if attachment and item.get("id") == attachment["id"]), None)
        prior = current.get("receipt", {})
        if stored is None or failure:
            # Keep whatever a person typed or confirmed; only the automatic part failed.
            failed = build_receipt_state("", prior)
            failed["state"], failed["error"] = "error", failure or "the receipt has no stored original"
            current["receipt"] = failed
        else:
            if extracted_md:
                write_attachment_extracted_sidecar(current, stored, extracted_md)
                stored["extracted_markdown"] = True
                stored["extracted_at"] = now_iso()
            else:
                stored["extracted_markdown"] = False
                stored["extraction_attempted_at"] = now_iso()
            write_attachment_metadata(current["id"], stored)
            current["receipt"] = build_receipt_state(extracted_md, prior, ai_fields=ai_fields, ai_info=ai_info)
        current["updated"] = now_iso()
        save_index(index)
    return "error" if stored is None or failure else "done"


def medical_reextract(args: argparse.Namespace) -> None:
    """Read stored receipts again: the ones named, or every one not yet reviewed.

    Values a person typed or confirmed are kept. With ``defer`` the receipts
    are only queued, for kaal-server's background worker to read.
    """
    init_if_needed()
    ids = list(dict.fromkeys(getattr(args, "note_ids", None) or []))
    unreviewed = bool(getattr(args, "unreviewed", False))
    if bool(ids) == unreviewed:
        die("Give receipt ids, or --unreviewed, but not both.")
    with vault_capture_lock():
        index = load_index()
        if ids:
            chosen = [medical_receipt_meta(index, note_id) for note_id in ids]
            if any(meta.get("trashed_at") for meta in chosen):
                die("A receipt in Trash cannot be read again; restore it first.")
        else:
            chosen = [note for note in index.get("notes", []) if is_medical_receipt(note) and not note.get("trashed_at") and "inbox" in note.get("tags", [])]
        for meta in chosen:
            meta["receipt"]["state"] = receipts.STATE_PENDING
        if chosen:
            save_index(index)
    queued = [meta["id"] for meta in chosen]
    if getattr(args, "defer", False):
        emit({"status": "queued", "count": len(queued), "ids": queued}, ensure_ascii=False)
        return
    outcomes: dict[str, str] = {}
    for position, note_id in enumerate(queued, start=1):
        if not capturing():
            print(f"reading {position} of {len(queued)}: {note_id}", file=sys.stderr, flush=True)
        outcomes[note_id] = reextract_receipt(note_id)
    emit({
        "status": "updated",
        "count": len(queued),
        "done": [note_id for note_id, outcome in outcomes.items() if outcome == "done"],
        "errors": [note_id for note_id, outcome in outcomes.items() if outcome != "done"],
    }, ensure_ascii=False)


def medical_providers(args: argparse.Namespace) -> None:
    """List the provider names Kaal has learnt to show the user's way, or forget some."""
    init_if_needed()
    forget = [receipts.name_key(name) for name in (getattr(args, "forget", None) or [])]
    if any(not key for key in forget):
        die("A provider name cannot be empty.")

    def listing(names: dict[str, str]) -> list[dict[str, str]]:
        return [{"printed": printed, "shown_as": shown} for printed, shown in sorted(names.items())]

    if not forget:
        emit({"status": "ok", "providers": listing(medical_provider_names(load_index()))}, ensure_ascii=False)
        return
    with vault_capture_lock():
        index = load_index()
        medical = index.get("medical") if isinstance(index.get("medical"), dict) else {}
        names = medical_provider_names(index)
        unknown = [key for key in forget if key not in names]
        if unknown:
            die("Kaal has not learnt that provider name.", kind="not_found", details={"printed": unknown})
        for key in forget:
            names.pop(key)
        medical["provider_names"] = names
        medical["provider_names_forgotten"] = sorted({*medical.get("provider_names_forgotten", []), *forget})
        index["medical"] = medical
        save_index(index)
    emit({"status": "updated", "providers": listing(names)}, ensure_ascii=False)


def page_of(items: list[Any], args: Any) -> tuple[list[Any], dict[str, int]]:
    """One page of ``items`` and the numbers a caller needs to ask for the next."""
    offset = max(0, int(getattr(args, "offset", 0) or 0))
    limit = max(0, int(getattr(args, "limit", 0) or 0))
    return (items[offset:offset + limit] if limit else items[offset:]), {"total": len(items), "offset": offset, "limit": limit}


RECEIPT_SORTS = {
    "updated": lambda note: note.get("updated", ""),
    "created": lambda note: note.get("created", ""),
    "date": lambda note: next((note.get("receipt", {}).get("fields", {}).get(name) for name in receipts.DATE_FIELDS if note.get("receipt", {}).get("fields", {}).get(name)), ""),
    "amount": lambda note: receipts.amount_value(note.get("receipt", {}).get("fields", {}).get("amount") or "") or Decimal("-1"),
}


def medical_list(args: argparse.Namespace) -> None:
    """List receipt metadata without mixing in unrelated Kaal notes.

    Optional filters: ``status`` (a receipt status), ``q`` (text found in the
    title, person, patient, provider, insurer or id) and ``sort`` (updated,
    created, date or amount; newest or largest first), then ``offset``/``limit``.
    """
    init_if_needed()
    notes = [n for n in load_index().get("notes", []) if "medical" in n.get("tags", []) and "receipt" in n.get("tags", [])]
    if getattr(args, "trash", False):
        notes = [n for n in notes if n.get("trashed_at")]
    else:
        notes = [n for n in notes if not n.get("trashed_at")]
    if getattr(args, "inbox", False):
        notes = [n for n in notes if "inbox" in n.get("tags", [])]
    status = (getattr(args, "status", "") or "").strip()
    if status:
        notes = [n for n in notes if n.get("receipt", {}).get("status") == status]
    query = (getattr(args, "q", "") or "").strip().casefold()
    if query:
        def searchable(note: dict[str, Any]) -> str:
            receipt = note.get("receipt", {})
            fields = receipt.get("fields", {})
            return " ".join([note.get("id", ""), note.get("title", ""), receipt.get("person") or "", *(fields.get(name) or "" for name in ("patient_name", "provider", "insurer"))]).casefold()
        notes = [n for n in notes if query in searchable(n)]
    sort = (getattr(args, "sort", "") or "updated").strip()
    if sort not in RECEIPT_SORTS:
        die(f"sort must be one of: {', '.join(RECEIPT_SORTS)}")
    notes.sort(key=RECEIPT_SORTS[sort], reverse=True)
    notes, paging = page_of(notes, args)
    if getattr(args, "json", False) or capturing():
        emit(notes, indent=2, ensure_ascii=False, meta=paging)
        return
    print_note_lines(notes)


def medical_show(args: argparse.Namespace) -> None:
    """One receipt in full, with the line of its text that each value came from."""
    init_if_needed()
    meta = medical_receipt_meta(load_index(), args.note_id)
    receipt = meta.get("receipt", {})
    attachments = meta.get("attachments", [])
    text = extracted_plain_text(read_attachment_extracted_sidecar(meta, attachments[0])) if attachments else ""
    evidence: dict[str, str] = {}
    for name in receipts.EVALUATED_FIELDS:
        value = receipt.get("fields", {}).get(name) or ""
        printed = receipt.get("field_raw", {}).get(name) or ""
        line = (printed and receipts.evidence_line(name, printed, text)) or (value and receipts.evidence_line(name, value, text))
        if line:
            evidence[name] = line
    emit({
        "status": "ok",
        "note": library_note_summary(meta) | {key: meta[key] for key in ("reviewed_at",) if key in meta},
        "receipt": receipt,
        "evidence": evidence,
    }, ensure_ascii=False)


def attachment_preview(note_meta: dict[str, Any], attachment_meta: dict[str, Any], page: int = 1) -> tuple[str, bytes]:
    """A page of an attachment as an image a browser can show: (media type, bytes)."""
    # One of a few turns, taken before the file is even decrypted, and only the page asked for
    # is drawn. tools.Busy (no turn in a couple of seconds) is the caller's to answer.
    with tools.turn("previews"), tempfile.TemporaryDirectory(prefix="kaal-preview-") as directory:
        os.chmod(directory, 0o700)
        copy = Path(directory) / f"preview{Path(attachment_meta.get('name', '')).suffix.lower()}"
        copy.write_bytes(attachment_original_bytes(note_meta, attachment_meta))
        os.chmod(copy, 0o600)
        images = receipt_ai.receipt_images(copy, page=max(1, page), pool=None)
    if not images:
        die("No preview is available for that page.", kind="not_found")
    return images[0]


def medical_receipt_meta(index: dict[str, Any], note_id: str) -> dict[str, Any]:
    for note in index.get("notes", []):
        if note.get("id") == note_id:
            if "medical" not in note.get("tags", []) or "receipt" not in note.get("tags", []):
                die("That note is not a medical receipt.")
            return note
    die("Medical receipt not found.", kind="not_found")


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
        for field in receipts.EVALUATED_FIELDS:
            if fields.get(field) and sources.get(field) != "manual":
                sources[field] = "confirmed"
        receipt.pop("conflicts", None)
        meta["tags"] = [tag for tag in meta.get("tags", []) if tag != "inbox"]
        meta["reviewed_at"] = now_iso()
        meta["updated"] = meta["reviewed_at"]
        save_index(index)
    emit({"status": "reviewed", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False)


def medical_reopen(args: argparse.Namespace) -> None:
    init_if_needed()
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        meta["tags"] = sorted(set(meta.get("tags", []) + ["inbox"]))
        meta.pop("reviewed_at", None)
        meta["updated"] = now_iso()
        save_index(index)
    emit({"status": "inbox", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False)


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
    emit({"status": "ok", "id": meta["id"], "attachment_id": att["id"], "text": text}, ensure_ascii=False)


def medical_export_original(args: argparse.Namespace) -> None:
    """Export one exact Kaal-managed receipt attachment without altering it."""
    init_if_needed()
    meta = medical_receipt_meta(load_index(), args.note_id)
    matches = [att for att in meta.get("attachments", []) if att.get("id") == args.attachment_id]
    if len(matches) != 1:
        die("Specify one exact receipt attachment id.")
    attachment = matches[0]
    output = Path(args.output).expanduser().resolve()
    if not output.parent.is_dir():
        die("Export destination directory does not exist.")
    if output.exists() or output.is_symlink():
        die(f"Output exists: {output}")
    plaintext = attachment_original_bytes(meta, attachment)
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(file_descriptor, "wb") as exported:
            file_descriptor = None
            exported.write(plaintext)
    except Exception:
        if file_descriptor is not None:
            os.close(file_descriptor)
        output.unlink(missing_ok=True)
        raise
    emit({
        "status": "exported",
        "id": meta["id"],
        "attachment_id": attachment["id"],
        "path": str(output),
        "bytes": len(plaintext),
        "sha256": hashlib.sha256(plaintext).hexdigest(),
    }, ensure_ascii=False)


def medical_update(args: argparse.Namespace) -> None:
    init_if_needed()
    try:
        updates = json.loads(args.fields_json)
    except json.JSONDecodeError:
        die("Receipt fields must be valid JSON.")
    allowed = {"patient_name", "provider", "service_date", "paid_date", "insurer", "amount", "currency"}
    if not isinstance(updates, dict) or any(key not in allowed or not isinstance(value, str) or len(value) > 200 for key, value in updates.items()):
        die("Receipt fields are invalid.")
    stored: dict[str, str] = {}
    for key, value in updates.items():
        value = value.strip()
        stored[key] = value
        if value and key in receipts.NORMALISED_FIELDS:
            is_date = key in receipts.DATE_FIELDS
            stored[key] = receipts.normalize_date(value) if is_date else receipts.normalize_amount(value)
            if not stored[key]:
                die(f"{key} is not {'a date' if is_date else 'an amount'} Kaal can read. Use {'YYYY-MM-DD' if is_date else 'a plain amount such as 123.45'}.")
    with vault_capture_lock():
        index = load_index()
        meta = medical_receipt_meta(index, args.note_id)
        receipt = meta.setdefault("receipt", receipt_extraction_state())
        fields = receipt.setdefault("fields", {})
        sources = receipt.setdefault("field_sources", {})
        raw = receipt.setdefault("field_raw", {})
        for key, value in stored.items():
            fields[key] = value
            receipt.get("conflicts", {}).pop(key, None)
            typed = updates[key].strip()
            if typed != value:
                raw[key] = typed
            else:
                raw.pop(key, None)
            if value:
                sources[key] = "manual"
            else:
                sources.pop(key, None)
        receipt["manually_updated_at"] = now_iso()
        meta["updated"] = receipt["manually_updated_at"]
        save_index(index)
    emit({"status": "updated", "id": meta["id"], "fields": receipt["fields"]}, ensure_ascii=False)


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _report_date_filter(raw: str, label: str) -> str:
    if not raw:
        return ""
    normalised = receipts.normalize_date(raw)
    if not normalised:
        die(f"{label} is not a date Kaal can read. Use YYYY-MM-DD.")
    return normalised


def medical_report(args: argparse.Namespace) -> None:
    """Totals for active receipts: overall, per person, per year, and not yet reimbursed.

    A receipt is dated by its service date, or its payment date when it has no
    service date. Receipts with no usable date are listed under ``undated``
    rather than dropped; they are left out of the totals only when a year or
    date range is asked for, because they cannot be placed in it.
    """
    init_if_needed()
    index = load_index()
    notes = [note for note in index.get("notes", []) if is_medical_receipt(note) and not note.get("trashed_at")]
    patient = (getattr(args, "patient_name", "") or "").strip().casefold()
    year = (getattr(args, "year", "") or "").strip()
    if year and not re.fullmatch(r"\d{4}", year):
        die("Year must be four digits, for example 2026.")
    start = _report_date_filter((getattr(args, "from_date", "") or "").strip(), "The start date")
    end = _report_date_filter((getattr(args, "to_date", "") or "").strip(), "The end date")
    reimbursed_filter = (getattr(args, "reimbursed", "") or "all").strip().lower()
    if reimbursed_filter not in {"all", "yes", "no"}:
        die("reimbursed must be one of: all, yes, no")
    dated_filter = bool(year or start or end)

    rows: list[dict[str, Any]] = []
    undated: list[str] = []
    missing_amount: list[str] = []
    unverified_amount: list[str] = []
    zero = Decimal("0")
    totals = {"total": zero, "reimbursed": zero, "unreimbursed": zero, "needs_review": zero}
    needs_review_count = 0
    by_person: dict[str, dict[str, Any]] = {}
    by_year: dict[str, dict[str, Any]] = {}

    def add(groups: dict[str, dict[str, Any]], key: str, amount: Decimal, reimbursed: bool) -> None:
        group = groups.setdefault(key, {"count": 0, "total": zero, "unreimbursed": zero})
        group["count"] += 1
        group["total"] += amount
        if not reimbursed:
            group["unreimbursed"] += amount

    for note in notes:
        receipt = note.get("receipt", {})
        fields = receipt.get("fields", {})
        person = receipt.get("person") or ""
        if patient and patient not in person.casefold() and patient not in (fields.get("patient_name") or "").casefold():
            continue
        reimbursed = bool(receipt.get("reimbursed_at"))
        if reimbursed_filter != "all" and reimbursed != (reimbursed_filter == "yes"):
            continue
        sources = receipt.get("field_sources", {})
        # A value the model read but Kaal could not find in the receipt text
        # does not count until a person confirms it.
        when = next((fields[name] for name in receipts.DATE_FIELDS if fields.get(name) and sources.get(name) not in receipts.UNVERIFIED_SOURCES), "")
        if not when:
            undated.append(note["id"])
            if dated_filter:
                continue
        elif (year and not when.startswith(year)) or (start and when < start) or (end and when > end):
            continue
        amount = receipts.amount_value(fields.get("amount") or "")
        if amount is None:
            missing_amount.append(note["id"])
            amount = zero
        elif sources.get("amount") in receipts.UNVERIFIED_SOURCES:
            unverified_amount.append(note["id"])
            amount = zero
        reviewed = receipt.get("status") == receipts.STATUS_REVIEWED
        totals["total"] += amount
        totals["reimbursed" if reimbursed else "unreimbursed"] += amount
        if not reviewed:
            needs_review_count += 1
            totals["needs_review"] += amount
        add(by_person, person, amount, reimbursed)
        add(by_year, when[:4], amount, reimbursed)
        rows.append({
            "id": note["id"],
            "title": note.get("title", ""),
            "status": receipt.get("status", ""),
            "person": person,
            "reimbursed_at": receipt.get("reimbursed_at", ""),
            "fields": fields,
        })

    def grouped(groups: dict[str, dict[str, Any]], label: str) -> list[dict[str, Any]]:
        return [
            {label: key, "count": group["count"], "total_amount": _money(group["total"]), "unreimbursed_amount": _money(group["unreimbursed"])}
            for key, group in sorted(groups.items())
        ]

    emit({
        "status": "ok",
        "count": len(rows),
        "total_amount": _money(totals["total"]),
        "reimbursed_amount": _money(totals["reimbursed"]),
        "unreimbursed_amount": _money(totals["unreimbursed"]),
        "needs_review": {"count": needs_review_count, "amount": _money(totals["needs_review"])},
        "by_person": grouped(by_person, "person"),
        "by_year": grouped(by_year, "year"),
        "undated": undated,
        "missing_amount": missing_amount,
        "unverified_amount": unverified_amount,
        "receipts": rows,
    }, ensure_ascii=False)


def extract_receipt_fields(path: Path) -> tuple[dict[str, str], str]:
    """Run the receipt pipeline on one file, exactly as capture does.

    Returns the normalised candidate fields and the name of the text method
    that was used. Nothing is stored.
    """
    extracted = extract_attachment_markdown(path, ocr=True, extract=True)
    ai_fields, ai_info = receipt_ai_candidates(path, extracted)
    receipt = build_receipt_state(extracted, ai_fields=ai_fields, ai_info=ai_info)
    receipts.upgrade(receipt, reviewed=False, people=[], provider_names=medical_provider_names(load_index()))
    method = re.search(r"^Method: (.+)$", extracted, flags=re.MULTILINE)
    return receipt["fields"], method.group(1).strip() if method else "none"


# The extractor that `kaal medical evaluate` measures. Tests replace it.
receipt_extractor: Callable[[Path], tuple[dict[str, str], str]] = extract_receipt_fields


def _receipt_file_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in PDF_ATTACHMENT_SUFFIXES:
        return "pdf"
    return "image" if suffix in IMAGE_ATTACHMENT_SUFFIXES else "other"


def medical_evaluate(args: argparse.Namespace) -> None:
    """Measure extraction against values a person typed or confirmed.

    Each receipt's stored original is run through the extractor again and the
    result is compared, field by field, with the values whose source is
    ``manual`` or ``confirmed``. The output holds receipt ids and outcomes
    only, never a field value, and the vault is not changed.
    """
    init_if_needed()
    wanted = set(getattr(args, "ids", None) or [])
    limit = getattr(args, "limit", 0) or 0
    notes = [note for note in load_index().get("notes", []) if is_medical_receipt(note) and not note.get("trashed_at")]
    if wanted:
        notes = [note for note in notes if note["id"] in wanted]
        missing = wanted - {note["id"] for note in notes}
        if missing:
            die("Not an active medical receipt.", kind="not_found", details={"ids": sorted(missing)})

    def tally() -> dict[str, dict[str, int]]:
        return {name: {"truth": 0, "correct": 0, "wrong": 0, "missing": 0} for name in receipts.EVALUATED_FIELDS}

    overall = tally()
    by_kind: dict[str, dict[str, dict[str, int]]] = {}
    evaluated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    started = datetime.now(timezone.utc)
    for note in sorted(notes, key=lambda item: item["id"]):
        if limit and len(evaluated) >= limit:
            break
        receipt = note.get("receipt", {})
        sources = receipt.get("field_sources", {})
        truth = {
            name: receipt.get("fields", {}).get(name) or ""
            for name in receipts.EVALUATED_FIELDS
            if sources.get(name) in receipts.TRUSTED_SOURCES and receipt.get("fields", {}).get(name)
        }
        attachments = note.get("attachments", [])
        if not truth:
            skipped.append({"id": note["id"], "reason": "no confirmed values"})
            continue
        if len(attachments) != 1:
            skipped.append({"id": note["id"], "reason": "needs exactly one attachment"})
            continue
        attachment = attachments[0]
        kind = _receipt_file_kind(attachment.get("name", ""))
        if not capturing():
            print(f"evaluating {len(evaluated) + 1}: {note['id']}", file=sys.stderr, flush=True)
        began = datetime.now(timezone.utc)
        try:
            with tempfile.TemporaryDirectory(prefix="kaal-evaluate-") as directory:
                copy = Path(directory) / f"receipt{Path(attachment.get('name', '')).suffix.lower()}"
                copy.write_bytes(attachment_original_bytes(note, attachment))
                os.chmod(copy, 0o600)
                candidate, method = receipt_extractor(copy)
        except KaalError:
            raise
        except Exception as error:
            skipped.append({"id": note["id"], "reason": f"extraction failed: {type(error).__name__}"})
            continue
        outcomes = {name: receipts.compare_field(name, value, candidate.get(name) or "") for name, value in truth.items()}
        for name, outcome in outcomes.items():
            for counts in (overall, by_kind.setdefault(kind, tally())):
                counts[name]["truth"] += 1
                counts[name][outcome] += 1
        evaluated.append({
            "id": note["id"],
            "kind": kind,
            "method": method,
            "seconds": round((datetime.now(timezone.utc) - began).total_seconds(), 1),
            "results": outcomes,
        })

    def summarise(counts: dict[str, dict[str, int]]) -> dict[str, Any]:
        fields = {name: {**value, "accuracy": f"{value['correct']}/{value['truth']}"} for name, value in counts.items() if value["truth"]}
        truth = sum(value["truth"] for value in counts.values())
        correct = sum(value["correct"] for value in counts.values())
        return {"correct": correct, "truth": truth, "percent": round(100 * correct / truth) if truth else 0, "fields": fields}

    emit({
        "status": "ok",
        "receipt_ai": receipt_ai_status(),
        "evaluated": len(evaluated),
        "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "overall": summarise(overall),
        "by_kind": {kind: summarise(counts) for kind, counts in sorted(by_kind.items())},
        "receipts": evaluated,
        "skipped": skipped,
    }, indent=2, ensure_ascii=False)


def medical_reimburse(args: argparse.Namespace) -> None:
    """Record that receipts were reimbursed (for example from an HSA), or undo that."""
    init_if_needed()
    ids = list(dict.fromkeys(getattr(args, "note_ids", []) or []))
    if not ids:
        die("Give at least one receipt id.")
    undo = bool(getattr(args, "undo", False))
    note_text = (getattr(args, "note", "") or "").strip()
    if len(note_text) > 500:
        die("The reimbursement note is limited to 500 characters.")
    when = ""
    if not undo:
        typed = (getattr(args, "date", "") or "").strip()
        when = receipts.normalize_date(typed) if typed else date.today().isoformat()
        if not when:
            die("The reimbursement date is not a date Kaal can read. Use YYYY-MM-DD.")
    with vault_capture_lock():
        index = load_index()
        chosen = [medical_receipt_meta(index, note_id) for note_id in ids]
        total = Decimal("0")
        for meta in chosen:
            receipt = meta["receipt"]
            if meta.get("trashed_at"):
                die("A receipt in Trash cannot be changed; restore it first.", details={"id": meta["id"]})
            if undo:
                continue
            if receipt.get("status") != receipts.STATUS_REVIEWED:
                die("Review a receipt before marking it reimbursed.", details={"id": meta["id"]})
            amount = receipts.amount_value(receipt.get("fields", {}).get("amount") or "")
            if amount is None:
                die("A receipt needs a paid amount before it can be marked reimbursed.", details={"id": meta["id"]})
            total += amount
        stamp = now_iso()
        for meta in chosen:
            receipt = meta["receipt"]
            if undo:
                receipt.pop("reimbursed_at", None)
                receipt.pop("reimbursement_note", None)
            else:
                receipt["reimbursed_at"] = when
                if note_text:
                    receipt["reimbursement_note"] = note_text
                else:
                    receipt.pop("reimbursement_note", None)
            meta["updated"] = stamp
        save_index(index)
    result: dict[str, Any] = {"status": "unreimbursed" if undo else "reimbursed", "count": len(chosen), "ids": [meta["id"] for meta in chosen]}
    if not undo:
        result.update({"reimbursed_at": when, "total_amount": _money(total)})
    emit(result, ensure_ascii=False)


def medical_sources(args: argparse.Namespace) -> None:
    """The sites receipts were saved from, most recent first.

    Only host names: no addresses, titles or receipt values. The browser
    extension uses it to offer "Save receipt" first on those sites.
    """
    init_if_needed()
    latest: dict[str, str] = {}
    for note in load_index().get("notes", []):
        if not is_medical_receipt(note) or note.get("trashed_at"):
            continue
        try:
            parts = urlsplit((note.get("source") or {}).get("url") or "")
        except ValueError:
            continue
        host = (parts.hostname or "").removeprefix("www.")
        if parts.scheme in {"http", "https"} and host:
            latest[host] = max(latest.get(host, ""), note.get("created", ""))
    emit({"hosts": [host for host, _when in sorted(latest.items(), key=lambda item: item[1], reverse=True)][:200]}, indent=2, ensure_ascii=False)


def medical_people(args: argparse.Namespace) -> None:
    """List, add or remove the family members receipts are assigned to."""
    init_if_needed()
    add = [receipts.clean_person_name(name) for name in (getattr(args, "add", None) or [])]
    remove = [receipts.clean_person_name(name) for name in (getattr(args, "remove", None) or [])]
    if any(not name for name in add + remove):
        die("A person's name cannot be empty.")
    if not add and not remove:
        emit({"status": "ok", "people": medical_people_names(load_index())}, ensure_ascii=False)
        return
    with vault_capture_lock():
        index = load_index()
        people = medical_people_names(index)
        gone = {name.casefold() for name in remove}
        people = [name for name in people if name.casefold() not in gone]
        known = {name.casefold() for name in people}
        for name in add:
            if name.casefold() not in known:
                people.append(name)
                known.add(name.casefold())
        if len(people) > 50:
            die("The family list is limited to 50 people.")
        medical = index.get("medical") if isinstance(index.get("medical"), dict) else {}
        medical["people"] = people
        index["medical"] = medical
        # Saving re-matches every receipt against the new list.
        save_index(index)
    emit({"status": "updated", "people": people}, ensure_ascii=False)


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
    emit({"status": "trashed", "id": meta["id"], "title": meta["title"], "attachments": len(meta.get("attachments", []))}, indent=2, ensure_ascii=False)


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
    emit({"status": "restored", "id": meta["id"], "title": meta["title"]}, indent=2, ensure_ascii=False)


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
        purge_dir = current_vault().root / ".receipt-purge" / note_id
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
    emit({"status": "purged", "id": note_id, "title": meta["title"]}, indent=2, ensure_ascii=False)


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
        batch_dir = current_vault().root / ".receipt-purge" / f"bulk-{secrets.token_hex(8)}"
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
    emit({"status": "purged", "count": len(note_ids), "ids": note_ids}, indent=2, ensure_ascii=False)



def _file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _decrypts(payload: dict[str, Any], aad: bytes, key: bytes) -> bytes | None:
    """Decrypt without dying: verify reports failures instead of stopping."""
    try:
        if payload.get("alg") != "AES-256-GCM" or base64.b64decode(payload.get("aad", "")) != aad:
            return None
        AESGCM = import_crypto()
        return AESGCM(key).decrypt(base64.b64decode(payload["nonce"]), base64.b64decode(payload["ciphertext"]), aad)
    except Exception:
        return None


@holds_vault_lock
def encrypt_vault(args: argparse.Namespace) -> None:
    """Encrypt every note, attachment and extracted text an older Kaal stored readable.

    For each item the encrypted copy is written and read back, then the index
    is saved, and only then is the readable copy removed. A run that stops at
    any point therefore loses nothing, and the next run finishes the job,
    including removing readable copies an interrupted run left behind. An
    attachment whose bytes no longer match its recorded hash is left alone and
    reported. Output holds ids only.
    """
    dry_run = bool(getattr(args, "dry_run", False))
    index = load_index()
    counts = {"notes": 0, "attachments": 0, "extracted_text": 0, "docling": 0, "metadata": 0, "attachment_names": 0, "index": 0}
    problems: list[dict[str, str]] = []

    def remove_readable_sidecars(att_dir: Path) -> None:
        if (att_dir / "extracted.md.json").exists():
            (att_dir / "extracted.md").unlink(missing_ok=True)
        if (att_dir / "docling.json.enc").exists():
            (att_dir / "docling.json").unlink(missing_ok=True)

    for meta in index.get("notes", []):
        nid = meta["id"]
        plain = plaintext_note_path(nid)
        if meta.get("storage", "encrypted") == "plaintext":
            if not plain.is_file():
                problems.append({"kind": "note_file_missing", "note_id": nid})
            else:
                counts["notes"] += 1
                if not dry_run:
                    note = load_note(meta)
                    note["storage"] = "encrypted"
                    secure_write_json(encrypted_note_path(nid), encrypt_bytes(json.dumps(note, ensure_ascii=False).encode(), aad=nid.encode()))
                    back = json.loads(decrypt_payload(json.loads(encrypted_note_path(nid).read_text()), aad=nid.encode()).decode())
                    if back.get("body") != note["body"]:
                        die(f"Note {nid} did not read back the same after encryption; nothing was removed.", kind="unavailable")
                    meta["storage"] = "encrypted"
                    save_index(index)
                    plain.unlink()
        elif not dry_run and plain.is_file() and encrypted_note_path(nid).is_file():
            # Left behind by a run that stopped after saving the index.
            plain.unlink()
        for att in meta.get("attachments", []):
            att_id = att["id"]
            att_dir = attachment_note_dir(nid) / att_id
            container = att_dir / "original.json"
            readable = att_dir / att.get("stored_name", att["name"])
            if att.get("storage", "encrypted") != "plaintext":
                if not dry_run and container.is_file():
                    if readable != container and readable.name != "metadata.json" and readable.is_file():
                        readable.unlink()
                    remove_readable_sidecars(att_dir)
                continue
            if not readable.is_file():
                problems.append({"kind": "attachment_file_missing", "note_id": nid, "attachment_id": att_id})
                continue
            data = readable.read_bytes()
            if att.get("sha256") and hashlib.sha256(data).hexdigest() != att["sha256"]:
                problems.append({"kind": "attachment_hash_mismatch", "note_id": nid, "attachment_id": att_id})
                continue
            text = (att_dir / "extracted.md").read_text(encoding="utf-8") if (att_dir / "extracted.md").is_file() and readable.name != "extracted.md" else ""
            docling = read_attachment_docling_sidecar(meta, att) if readable.name != "docling.json" else {}
            counts["attachments"] += 1
            counts["extracted_text"] += bool(text)
            counts["docling"] += bool(docling)
            if dry_run:
                continue
            name = att["name"]
            aad = f"{nid}:{att_id}:{name}".encode()
            secure_write_json(container, attachment_container(name, data, encrypt_bytes(data, aad=aad)))
            if decrypt_payload(json.loads(container.read_text())["encrypted"], aad=aad) != data:
                die(f"Attachment {att_id} did not read back the same after encryption; nothing was removed.", kind="unavailable")
            if text:
                secure_write_json(att_dir / "extracted.md.json", {"name": "extracted.md", "encrypted": encrypt_bytes(text.encode(), aad=f"{nid}:{att_id}:extracted.md".encode())})
            if docling:
                secure_write_json(att_dir / "docling.json.enc", {"encrypted": encrypt_bytes(json.dumps(docling, ensure_ascii=False).encode(), aad=f"{nid}:{att_id}:docling.json".encode())})
            att["storage"] = "encrypted"
            write_attachment_metadata(nid, att)
            counts["metadata"] += 1
            save_index(index)
            if readable != container:
                readable.unlink()
            remove_readable_sidecars(att_dir)
    # The copies of attachment metadata, then the index itself: these hold
    # titles, tags, attachment names and receipt fields.
    for meta in index.get("notes", []):
        for att in meta.get("attachments", []):
            copy = attachment_note_dir(meta["id"]) / att["id"] / "metadata.json"
            if not copy.is_file():
                continue
            try:
                already = _is_encrypted_envelope(json.loads(copy.read_text()))
            except (OSError, json.JSONDecodeError):
                already = False
            if not already:
                counts["metadata"] += 1
                if not dry_run:
                    write_attachment_metadata(meta["id"], att)
            # The encrypted original still names the attachment in the clear.
            container = copy.with_name("original.json")
            if att.get("storage", "encrypted") != "encrypted" or not container.is_file():
                continue
            try:
                held = json.loads(container.read_text())
            except (OSError, json.JSONDecodeError):
                problems.append({"kind": "attachment_unreadable", "note_id": meta["id"], "attachment_id": att["id"]})
                continue
            if "name" not in held:
                continue
            if held["name"] != att.get("name"):
                # Without the name the file could no longer be opened: leave it and say so.
                problems.append({"kind": "attachment_name_mismatch", "note_id": meta["id"], "attachment_id": att["id"]})
                continue
            counts["attachment_names"] += 1
            if not dry_run:
                held.pop("name")
                secure_write_json(container, held)
    if not index_is_encrypted():
        counts["index"] = 1
        if not dry_run:
            save_index(index, encrypted=True)
            if read_index_file().get("notes") != index.get("notes"):
                die("The index did not read back the same after encryption.", kind="unavailable")
    emit({
        "status": "dry-run" if dry_run else ("problems" if problems else "ok"),
        "encrypted" if not dry_run else "would_encrypt": counts,
        "problems": problems,
    }, indent=2, ensure_ascii=False)


def verify_vault(args: argparse.Namespace) -> None:
    """Check the index against the files on disk. Reports ids only, never titles or names.

    Problems (exit 2 on the CLI): duplicate ids, missing note or attachment
    files, plaintext attachments whose sha256 differs from the index and, with
    --deep, encrypted notes or attachments that do not decrypt or match.
    Warnings: orphaned files or directories and loose permissions.
    """
    init_if_needed()
    deep = bool(getattr(args, "deep", False))
    with vault_capture_lock():
        index = load_index()
        notes = index.get("notes", [])
        key = get_key() if deep else b""
        problems: list[dict[str, str]] = []
        seen: set[str] = set()
        attachments = hashes_checked = decrypted = 0
        known_attachments: dict[str, set[str]] = {}
        for note in notes:
            nid = note.get("id", "")
            if nid in seen:
                problems.append({"kind": "duplicate_note_id", "note_id": nid})
                continue
            seen.add(nid)
            if note.get("storage", "encrypted") == "plaintext":
                if not plaintext_note_path(nid).is_file():
                    problems.append({"kind": "missing_note_file", "note_id": nid})
            else:
                path = encrypted_note_path(nid)
                if not path.is_file():
                    problems.append({"kind": "missing_note_file", "note_id": nid})
                elif deep:
                    try:
                        payload = json.loads(path.read_text())
                    except (OSError, json.JSONDecodeError):
                        payload = {}
                    if _decrypts(payload, nid.encode(), key) is None:
                        problems.append({"kind": "decrypt_failed", "note_id": nid})
                    else:
                        decrypted += 1
            known_attachments[nid] = set()
            for att in note.get("attachments", []):
                attachments += 1
                aid = att.get("id", "")
                known_attachments[nid].add(aid)
                att_dir = attachment_note_dir(nid) / aid
                where = {"note_id": nid, "attachment_id": aid}
                if not (att_dir / "metadata.json").is_file():
                    problems.append({"kind": "missing_attachment_metadata", **where})
                if att.get("storage", "encrypted") == "plaintext":
                    original = att_dir / att.get("stored_name", att.get("name", ""))
                    if not original.is_file():
                        problems.append({"kind": "missing_attachment_file", **where})
                    elif att.get("sha256"):
                        hashes_checked += 1
                        if _file_sha256(original) != att["sha256"]:
                            problems.append({"kind": "attachment_hash_mismatch", **where})
                    continue
                payload_path = att_dir / "original.json"
                if not payload_path.is_file():
                    payload_path = attachment_note_dir(nid) / f"{aid}.json"
                if not payload_path.is_file():
                    problems.append({"kind": "missing_attachment_file", **where})
                elif deep:
                    try:
                        stored = json.loads(payload_path.read_text())
                        plain = _decrypts(stored["encrypted"], f"{nid}:{aid}:{stored.get('name') or att['name']}".encode(), key)
                    except (OSError, KeyError, TypeError, json.JSONDecodeError):
                        plain = None
                    if plain is None:
                        problems.append({"kind": "decrypt_failed", **where})
                    else:
                        decrypted += 1
                        if att.get("sha256"):
                            hashes_checked += 1
                            if hashlib.sha256(plain).hexdigest() != att["sha256"]:
                                problems.append({"kind": "attachment_hash_mismatch", **where})
        orphan_note_files = sorted(
            {entry.name.split(".", 1)[0] for entry in current_vault().notes_dir.iterdir() if entry.is_file() and entry.suffix in {".md", ".json"}} - seen
        ) if current_vault().notes_dir.is_dir() else []
        orphan_note_dirs: list[str] = []
        orphan_attachment_dirs: list[str] = []
        if current_vault().attach_dir.is_dir():
            for note_dir in sorted(entry for entry in current_vault().attach_dir.iterdir() if entry.is_dir()):
                if note_dir.name not in known_attachments:
                    orphan_note_dirs.append(note_dir.name)
                    continue
                for att_dir in sorted(entry for entry in note_dir.iterdir() if entry.is_dir()):
                    if att_dir.name not in known_attachments[note_dir.name]:
                        orphan_attachment_dirs.append(f"{note_dir.name}/{att_dir.name}")
        loose_dirs = loose_files = 0
        for root, dirs, files in os.walk(current_vault().root):
            for name in dirs:
                if stat.S_IMODE(os.lstat(os.path.join(root, name)).st_mode) != 0o700:
                    loose_dirs += 1
            for name in files:
                if stat.S_IMODE(os.lstat(os.path.join(root, name)).st_mode) != 0o600:
                    loose_files += 1
        pending_purges = sorted(name for name in (".library-purge", ".receipt-purge") if (current_vault().root / name).exists())
    result = {
        "status": "problems" if problems else "ok",
        "deep": deep,
        "notes": len(notes),
        "attachments": attachments,
        "hashes_checked": hashes_checked,
        "decrypted": decrypted,
        "problems": problems,
        "warnings": {
            "orphan_note_files": orphan_note_files,
            "orphan_note_dirs": orphan_note_dirs,
            "orphan_attachment_dirs": orphan_attachment_dirs,
            "loose_permissions": {"dirs": loose_dirs, "files": loose_files},
            "pending_purges": pending_purges,
        },
    }
    emit(result, indent=2)
    if problems and not capturing():
        raise SystemExit(2)


# The second name is the lock file's name before 0.18; one may still lie in an older vault.
EXPORT_EXCLUDED = {VAULT_LOCK_NAME, ".medical-capture.lock", ".library-purge", ".receipt-purge"}


def _anonymous(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write_vault_tar(handle: Any) -> int:
    files = 0
    with tarfile.open(fileobj=handle, mode="w") as tar:
        for root, dirs, names in os.walk(current_vault().root):
            rel_root = Path(root).relative_to(current_vault().root)
            top = rel_root == Path(".")
            dirs[:] = sorted(d for d in dirs if not (top and d in EXPORT_EXCLUDED) and not os.path.islink(os.path.join(root, d)))
            for name in dirs:
                tar.add(os.path.join(root, name), arcname=str(rel_root / name), recursive=False, filter=_anonymous)
            for name in sorted(names):
                full = os.path.join(root, name)
                if (top and name in EXPORT_EXCLUDED) or os.path.islink(full) or not os.path.isfile(full):
                    continue
                tar.add(full, arcname=str(rel_root / name), recursive=False, filter=_anonymous)
                files += 1
    return files


def export_vault(args: argparse.Namespace) -> None:
    """Write the whole vault, exactly as stored, to a new tar file (mode 600).

    Encrypted notes and attachments stay encrypted; the key is never included.
    """
    init_if_needed()
    out = Path(args.output).expanduser().resolve()
    if out.exists() or out.is_symlink():
        die(f"Output exists: {out}")
    if out.is_relative_to(current_vault().root.resolve()):
        die("Write the export outside the vault.")
    with vault_capture_lock():
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                files = _write_vault_tar(handle)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        notes = len(load_index().get("notes", []))
    emit({"status": "exported", "path": str(out), "bytes": out.stat().st_size, "sha256": _file_sha256(out), "notes": notes, "files": files}, indent=2)


READABLE_RECEIPT_COLUMNS = ("id", "title", "status", "person", "patient_name", "provider", "insurer", "service_date", "paid_date",
                            "amount", "amount_due", "reimbursed_at", "reimbursement_note", "trashed", "files")
_READABLE_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def _readable_name(text: str, fallback: str, limit: int = 80) -> str:
    """A file name anyone's computer accepts, kept recognisable."""
    cleaned = _READABLE_UNSAFE.sub(" ", text or "").strip().strip(".")
    cleaned = " ".join(cleaned.split())[:limit].strip()
    return cleaned or fallback


def _front_matter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value in (None, "", []):
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def export_readable(args: argparse.Namespace) -> None:
    """Write everything in the vault, decrypted, to a new zip file (mode 600).

    For leaving Kaal, or keeping a copy that needs no key: each note as a Markdown
    file with its files beside it, each receipt's original file, and the receipts
    as a spreadsheet (receipts.csv). Anyone who has the file can read all of it.
    """
    import csv
    import io
    import zipfile

    init_if_needed()
    out = Path(args.output).expanduser().resolve()
    if out.exists() or out.is_symlink():
        die(f"Output exists: {out}")
    if out.is_relative_to(current_vault().root.resolve()):
        die("Write the export outside the vault.")
    counts = {"notes": 0, "receipts": 0, "files": 0}
    rows: list[dict[str, Any]] = []
    with vault_capture_lock():
        index = load_index()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
                taken: set[str] = set()

                def unique(path: str) -> str:
                    stem, dot, suffix = path.rpartition(".") if "." in path.rsplit("/", 1)[-1] else (path, "", "")
                    candidate, number = path, 2
                    while candidate.lower() in taken:
                        candidate = f"{stem} ({number}){dot}{suffix}" if dot else f"{path} ({number})"
                        number += 1
                    taken.add(candidate.lower())
                    return candidate

                for meta in index.get("notes", []):
                    receipt = is_medical_receipt(meta)
                    trashed = bool(meta.get("trashed_at"))
                    top = ("Trash/" if trashed else "") + ("Receipts" if receipt else "Notes")
                    stem = unique(f"{top}/{_readable_name(meta.get('title', ''), meta['id'])}")
                    folder_name = stem.rsplit("/", 1)[-1]
                    saved: list[str] = []
                    links: dict[str, str] = {}
                    for attachment in meta.get("attachments", []):
                        data = attachment_original_bytes(meta, attachment)
                        name = _readable_name(attachment.get("name", ""), attachment["id"], 120)
                        path = unique(f"{stem}/{name}")
                        archive.writestr(path, data)
                        counts["files"] += 1
                        relative = f"{folder_name}/{path.rsplit('/', 1)[-1]}"
                        saved.append(relative)
                        # The note's text refers to its files by id; point those at the copies instead.
                        links[str(attachment["id"]).lower()] = relative
                        resource = str((attachment.get("source") or {}).get("resource_id") or "").lower()
                        if resource:
                            links[resource] = relative
                    body = load_note(meta).get("body", "")

                    def relink(match: re.Match) -> str:
                        target = links.get((match.group(2) or match.group(3) or "").lower())
                        return f"]({target.replace(' ', '%20')})" if target else match.group(0)

                    body = re.sub(r"\]\(\s*(:/([0-9a-fA-F]{32})|attachment:([0-9a-fA-F]{6,32}))\s*\)", relink, body)
                    fields: dict[str, Any] = {"title": meta.get("title", ""), "tags": meta.get("tags", []), "created": meta.get("created", ""),
                                              "updated": meta.get("updated", ""), "kaal_id": meta["id"], "trashed": meta.get("trashed_at") or ""}
                    source = meta.get("source") or {}
                    if source.get("url"):
                        fields["source"] = source["url"]
                    archive.writestr(f"{stem}.md", _front_matter(fields) + body + ("\n" if body and not body.endswith("\n") else ""))
                    counts["receipts" if receipt else "notes"] += 1
                    if receipt:
                        state = meta.get("receipt") or {}
                        values = state.get("fields") or {}
                        rows.append({"id": meta["id"], "title": meta.get("title", ""), "status": state.get("status", ""), "person": state.get("person", ""),
                                     **{key: values.get(key, "") for key in ("patient_name", "provider", "insurer", "service_date", "paid_date", "amount", "amount_due")},
                                     "reimbursed_at": state.get("reimbursed_at") or "", "reimbursement_note": state.get("reimbursement_note") or "",
                                     "trashed": "yes" if trashed else "", "files": " ; ".join(f"{top}/{item}" for item in saved)})
                sheet = io.StringIO()
                writer = csv.DictWriter(sheet, fieldnames=READABLE_RECEIPT_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    # A spreadsheet must not mistake a value for a formula.
                    writer.writerow({key: ("'" + value if isinstance(value, str) and value[:1] in "=+-@\t\r" else value) for key, value in row.items()})
                archive.writestr("receipts.csv", sheet.getvalue())
                archive.writestr("README.txt", (
                    "A copy of a Kaal vault, readable without Kaal.\n\n"
                    "Notes/     each note as a Markdown file, its pictures and files in the folder of the same name\n"
                    "Receipts/  each medical receipt: the original file, and a Markdown file with what was read from it\n"
                    "Trash/     what was in the Trash\n"
                    "receipts.csv  every receipt's values, for a spreadsheet\n\n"
                    "Nothing here is encrypted. Keep this file as safe as the vault itself.\n"))
        except BaseException:
            out.unlink(missing_ok=True)
            raise
    emit({"status": "exported", "path": str(out), "bytes": out.stat().st_size, **counts}, indent=2)


def _mkdirs_700(path: Path) -> None:
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def import_vault(args: argparse.Namespace) -> None:
    """Restore a `kaal export` archive into a missing or empty vault, then verify it."""
    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        die(f"Archive not found: {archive}", kind="not_found")
    if current_vault().root.exists() and any(current_vault().root.iterdir()):
        die(f"The vault at {current_vault().root} is not empty; import only restores into an empty vault.", kind="conflict")
    try:
        tar = tarfile.open(archive)
    except tarfile.TarError:
        die("The archive is not a tar file.")
    files = 0
    with tar:
        members = tar.getmembers()
        for member in members:
            parts = PurePosixPath(member.name).parts
            if PurePosixPath(member.name).is_absolute() or ".." in parts or not parts or not (member.isfile() or member.isdir()):
                die(f"Refusing unsafe archive member: {member.name}")
        if "index.json" not in {PurePosixPath(m.name).as_posix() for m in members if m.isfile()}:
            die("The archive has no index.json, so it is not a Kaal export.")
        _mkdirs_700(current_vault().root)
        os.chmod(current_vault().root, 0o700)
        with vault_capture_lock():
            for member in members:
                dest = current_vault().root.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    _mkdirs_700(dest)
                    continue
                _mkdirs_700(dest.parent)
                source = tar.extractfile(member)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out, source:
                    shutil.copyfileobj(source, out)
                files += 1
    verify, _ = call_captured(verify_vault, SimpleNamespace(deep=False))
    emit({"status": "imported", "vault": str(current_vault().root), "files": files, "notes": verify["notes"], "verify": verify}, indent=2)


BACKUP_NAME_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _mkdir_700(path: Path) -> None:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def backup_vault(args: argparse.Namespace) -> None:
    """Copy the vault, as stored, into a new dated folder under --dest.

    Runs under the vault lock so the copy is consistent. Files unchanged since
    the newest earlier backup (same size and modification time) are hard links
    to it, so each night costs only what changed. Keeps the newest --keep
    backups and deletes older ones; only folders named like a backup
    (YYYYMMDDTHHMMSSZ) are ever removed. Prints counts, never names.
    """
    init_if_needed()
    raw_dest = getattr(args, "dest", "") or os.environ.get("KAAL_BACKUP_DIR", "")
    if not raw_dest:
        die("Pass --dest or set KAAL_BACKUP_DIR.")
    dest = Path(raw_dest).expanduser().resolve()
    if dest.is_relative_to(current_vault().root.resolve()):
        die("Backups must live outside the vault.")
    keep = int(getattr(args, "keep", 14))
    if keep < 1:
        die("--keep must be at least 1.")
    if not dest.exists():
        dest.mkdir(mode=0o700, parents=True)
    os.chmod(dest, 0o700)
    for stale in dest.glob(".incomplete-*"):
        if stale.is_dir() and BACKUP_NAME_RE.match(stale.name.removeprefix(".incomplete-")):
            shutil.rmtree(stale)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = dest / stamp
    if final.exists():
        die("A backup with this timestamp already exists; run it again in a second.", kind="conflict")
    work = dest / f".incomplete-{stamp}"
    counts = {"files": 0, "copied": 0, "linked": 0, "bytes": 0}
    complete = sorted(p for p in dest.iterdir() if p.is_dir() and BACKUP_NAME_RE.match(p.name))
    previous = complete[-1] if complete else None
    _mkdir_700(work)
    with vault_capture_lock():
        _backup_tree(current_vault().root, work, previous, counts)
    others = _backup_installation(work, previous, counts, dest)
    os.rename(work, final)
    complete = sorted(p for p in dest.iterdir() if p.is_dir() and BACKUP_NAME_RE.match(p.name))
    expired = complete[:-keep]
    for old in expired:
        shutil.rmtree(old)
    emit({"status": "ok", "backup": str(final), **counts, "kept": len(complete) - len(expired), "pruned": len(expired), **others}, indent=2)


# Inside a backup, beside the first vault's own files: the accounts file, the
# token file and every other vault of the installation.
BACKUP_INSTALLATION_DIR = "_installation"


def _backup_tree(source_root: Path, work: Path, previous: Path | None, counts: dict[str, int]) -> None:
    """Copy one vault folder into ``work``, hard-linking files unchanged since ``previous``."""
    for root, dirs, names in os.walk(source_root):
        rel_root = Path(root).relative_to(source_root)
        top = rel_root == Path(".")
        dirs[:] = sorted(d for d in dirs if not (top and d in EXPORT_EXCLUDED) and not os.path.islink(os.path.join(root, d)))
        for name in dirs:
            _mkdir_700(work / rel_root / name)
        for name in sorted(names):
            source = Path(root) / name
            if (top and name in EXPORT_EXCLUDED) or source.is_symlink() or not source.is_file():
                continue
            rel = rel_root / name
            target = work / rel
            info = source.stat()
            earlier = previous / rel if previous else None
            counts["files"] += 1
            counts["bytes"] += info.st_size
            if earlier is not None and earlier.is_file() and not earlier.is_symlink():
                earlier_info = earlier.stat()
                if (earlier_info.st_size, earlier_info.st_mtime_ns) == (info.st_size, info.st_mtime_ns):
                    os.link(earlier, target)
                    counts["linked"] += 1
                    continue
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
            counts["copied"] += 1


def installation_home() -> Path | None:
    """The folder holding accounts.db for the current vault's installation, if it has one."""
    named = os.environ.get("KAAL_HOME", "").strip()
    candidates = [Path(named).expanduser()] if named else []
    candidates.append(current_vault().root.parent)
    return next((home for home in candidates if (home / "accounts.db").is_file()), None)


def _backup_installation(work: Path, previous: Path | None, counts: dict[str, int], dest: Path) -> dict[str, Any]:
    """Add the accounts file, the token file and the installation's other vaults to a backup."""
    home = installation_home()
    if home is None:
        return {}
    import sqlite3

    from kaal import accounts as account_store

    store = account_store.AccountStore(home / "accounts.db", home)
    extra = work / BACKUP_INSTALLATION_DIR
    _mkdir_700(extra)
    # SQLite's own backup gives a consistent copy even while the server is writing.
    source_db = sqlite3.connect(home / "accounts.db", timeout=15)
    try:
        target_db = sqlite3.connect(extra / "accounts.db")
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
    finally:
        source_db.close()
    os.chmod(extra / "accounts.db", 0o600)
    counts["files"] += 1
    counts["copied"] += 1
    counts["bytes"] += (extra / "accounts.db").stat().st_size
    tokens_file = Path(os.environ.get("KAAL_TOKENS_FILE") or home / "tokens.json").expanduser()
    if tokens_file.is_file() and not tokens_file.is_symlink():
        shutil.copy2(tokens_file, extra / "tokens.json")
        os.chmod(extra / "tokens.json", 0o600)
        counts["files"] += 1
        counts["copied"] += 1
        counts["bytes"] += tokens_file.stat().st_size
    here = current_vault().root.resolve()
    saved = 0
    for record in store.vaults():
        folder = store.folder_of(record)
        if folder.resolve() == here or not (folder / "index.json").is_file():
            continue
        if dest.is_relative_to(folder.resolve()):
            die("Backups must live outside every vault.")
        target = extra / "vaults" / record["id"]
        _mkdirs_700(target)
        earlier = previous / BACKUP_INSTALLATION_DIR / "vaults" / record["id"] if previous else None
        with use_vault(Vault(folder)), vault_capture_lock():
            _backup_tree(folder, target, earlier, counts)
        saved += 1
    return {"accounts": True, "other_vaults": saved}

def _copy_tree_private(source: Path, target: Path, skip_top: frozenset[str] = frozenset()) -> int:
    """Copy a folder, giving the copy owner-only permissions. Returns how many files were copied."""
    copied = 0
    _mkdirs_700(target)
    for root, dirs, names in os.walk(source):
        rel = Path(root).relative_to(source)
        if rel == Path("."):
            dirs[:] = [name for name in dirs if name not in skip_top]
        for name in dirs:
            if not os.path.islink(os.path.join(root, name)):
                _mkdirs_700(target / rel / name)
        for name in names:
            from_path = Path(root) / name
            if from_path.is_symlink() or not from_path.is_file():
                continue
            to_path = target / rel / name
            shutil.copy2(from_path, to_path)
            os.chmod(to_path, 0o600)
            copied += 1
    return copied


def _check_vault(folder: Path) -> dict[str, Any]:
    """Whether the key opens a vault and its files are all there. Counts and problem kinds only."""
    with use_vault(Vault(folder)):
        result, _ = call_captured(verify_vault, SimpleNamespace(deep=False))
    return {"notes": result.get("notes", 0), "problems": sorted({problem["kind"] for problem in result.get("problems", [])})}


def restore_backup(args: argparse.Namespace) -> None:
    """Put an installation back the way a backup (from `kaal backup`) holds it.

    Kaal must not be running. The first vault comes from the backup's top level;
    when the backup has them, the accounts, the tokens and the other vaults come
    from its _installation folder. Nothing is deleted: what is there now is moved
    into a dated before-restore folder first. The key must be the one the backup
    was made with; it is checked before anything is moved.
    """
    backup = Path(args.backup).expanduser().resolve()
    if not (backup / "index.json").is_file():
        die("That is not a Kaal backup: it has no index.json.", kind="not_found")
    first = current_vault().root.resolve()
    if first.is_relative_to(backup) or backup.is_relative_to(first):
        die("The backup and the vault must be separate folders.")
    extra = backup / BACKUP_INSTALLATION_DIR
    installation = (extra / "accounts.db").is_file()
    try:
        before = _check_vault(backup)
    except KaalError:
        die("The key cannot open this backup. Restore needs the key the backup was made with.", kind="forbidden")
    if "decrypt_failed" in before["problems"]:
        die("The key cannot open this backup. Restore needs the key the backup was made with.", kind="forbidden")
    home = Path(os.environ.get("KAAL_HOME") or first.parent).expanduser().resolve()
    tokens_file = Path(os.environ.get("KAAL_TOKENS_FILE") or home / "tokens.json").expanduser().resolve()
    from kaal import accounts as account_store

    # Everything that belongs to the installation now, so it can be moved aside as a whole.
    present: list[Path] = [first]
    if installation:
        present += [home / "accounts.db", home / "accounts.db-wal", home / "accounts.db-shm", tokens_file]
        if (home / "accounts.db").is_file():
            current = account_store.AccountStore(home / "accounts.db", home)
            present += [current.folder_of(record).resolve() for record in current.vaults()]
    present = [path for path in dict.fromkeys(present) if path.exists()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = home / f"before-restore-{stamp}"
    moved = 0
    if present:
        _mkdirs_700(aside)
        for number, path in enumerate(present):
            try:
                label = path.relative_to(home).as_posix().replace("/", "__")
            except ValueError:
                label = f"outside-{number}-{path.name}"
            os.rename(path, aside / label)
            moved += 1
    files = _copy_tree_private(backup, first, frozenset({BACKUP_INSTALLATION_DIR}))
    checked = {"first_vault": _check_vault(first)}
    other_vaults = 0
    if installation:
        shutil.copy2(extra / "accounts.db", home / "accounts.db")
        os.chmod(home / "accounts.db", 0o600)
        if (extra / "tokens.json").is_file():
            _mkdirs_700(tokens_file.parent)
            shutil.copy2(extra / "tokens.json", tokens_file)
            os.chmod(tokens_file, 0o600)
        restored = account_store.AccountStore(home / "accounts.db", home)
        for record in restored.vaults():
            folder = restored.folder_of(record).resolve()
            saved = extra / "vaults" / record["id"]
            if folder == first or not saved.is_dir():
                continue
            files += _copy_tree_private(saved, folder)
            checked[record["id"]] = _check_vault(folder)
            other_vaults += 1
    trouble = sorted({kind for result in checked.values() for kind in result["problems"]})
    emit({"status": "restored" if not trouble else "restored_with_problems", "backup": backup.name, "files": files, "accounts": installation,
          "other_vaults": other_vaults, "notes": sum(result["notes"] for result in checked.values()), "problems": trouble,
          "moved_aside": str(aside) if moved else ""}, indent=2)


@holds_vault_lock
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
    emit({"status": "deleted", "id": nid}, indent=2)


def receipt_ai_status() -> dict[str, Any]:
    """Whether a model is configured for receipts and its command exists. Does not call the model."""
    try:
        return receipt_ai.describe(receipt_ai.settings_from_env())
    except receipt_ai.ReceiptAIError as error:
        return {"backend": "misconfigured", "usable": False, "problem": str(error)}


def _note_count() -> int | None:
    """How many notes the vault holds; None when the index cannot be read, for example without the key."""
    if not current_vault().index_path.exists():
        return 0
    try:
        return len(load_index().get("notes", []))
    except KaalError:
        return None


def status(args: argparse.Namespace) -> None:
    try:
        provider = key_providers.provider_from_env()
        provider_name = provider.name
        has_key = provider.key_exists()
    except key_providers.KeyProviderError:
        provider_name, has_key = "unconfigured", False
    emit({
        "vault": str(current_vault().root),
        "vault_exists": current_vault().root.exists(),
        "index_exists": current_vault().index_path.exists(),
        "key_provider": provider_name,
        "key_exists": has_key,
        "notes": _note_count(),
        "index_encrypted": index_is_encrypted() if current_vault().index_path.exists() else None,
        "receipt_ai": receipt_ai_status(),
    }, indent=2)


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
    return {
        "notes": notes,
        "folders": folders,
        "resources": resources,
        "tags": tags,
        "note_tags": note_tags,
        "referenced": referenced,
        "unresolved_resources": unresolved,
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


@holds_vault_lock
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
        emit(summary, indent=2, ensure_ascii=False)
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
    emit(summary, indent=2, ensure_ascii=False)
