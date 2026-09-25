#!/usr/bin/env python3
"""Chrome Native Messaging host for the Kaal browser-capture extension."""
from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_MESSAGE_BYTES = 1_000_000
MAX_CAPTURE_BYTES = 100 * 1024 * 1024
MAX_SELECTION_CHARS = 100_000
MAX_LIBRARY_BODY_CHARS = 100_000
MAX_LIBRARY_TEXT_CHARS = 100_000
RECEIPT_LIST_LIMIT = 100
LIBRARY_LIST_LIMIT = 100
STAGING_DIRECTORY_NAME = "Kaal Capture"
DOWNLOADS_DIR = Path.home() / "Downloads"
DEFAULT_STAGING_DIR = DOWNLOADS_DIR / STAGING_DIRECTORY_NAME
STAGING_DIR = Path(os.environ.get("KAAL_RECEIPT_STAGING_DIR", str(DEFAULT_STAGING_DIR))).expanduser().resolve()
STAGING_FILE_RE = re.compile(r"^receipt-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z-.+\.pdf$")
GENERAL_STAGING_FILE_RE = re.compile(r"^item-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z-.+\.[A-Za-z0-9]{1,16}$")
KAAL_SCRIPT = Path(__file__).with_name("secure-notes.py")


def read_message() -> dict[str, Any] | None:
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError("incomplete native-messaging header")
    length = struct.unpack("<I", header)[0]
    if length > MAX_MESSAGE_BYTES:
        raise ValueError("native-messaging request is too large")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        raise ValueError("incomplete native-messaging request")
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("native-messaging request must be an object")
    return parsed


def write_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def optional_text(payload: dict[str, Any], key: str, limit: int = 4096) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{key} is too long")
    return value


def is_staging_file(path: Path, *, general: bool = False) -> bool:
    # The installer pins this absolute directory to the user's Chrome download
    # location. Do not authorize a similarly named directory elsewhere.
    filename_pattern = GENERAL_STAGING_FILE_RE if general else STAGING_FILE_RE
    return path.parent == STAGING_DIR.resolve() and filename_pattern.fullmatch(path.name) is not None


def is_download_file(path: Path) -> bool:
    try:
        return path.is_relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        return False


def capture(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    medical_action = action in {"capture", "capture-local-file"}
    general_action = action in {"capture-general", "capture-general-local-file"}
    if not medical_action and not general_action:
        raise ValueError("unsupported action")
    raw_path = optional_text(payload, "path")
    if not raw_path:
        raise ValueError("path is required")
    candidate = Path(raw_path).expanduser()
    if candidate.is_symlink():
        raise ValueError("symbolic-link capture paths are not allowed")
    source = candidate.resolve()
    if not source.is_file():
        raise ValueError("capture file no longer exists")
    is_local_file = action in {"capture-local-file", "capture-general-local-file"}
    if not is_local_file and not is_staging_file(source, general=general_action):
        raise ValueError("capture path is outside the Kaal capture staging directory")
    if is_local_file and not is_download_file(source):
        raise ValueError("local capture path is outside the user's Downloads directory")
    if source.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError(f"capture exceeds the {MAX_CAPTURE_BYTES // (1024 * 1024)} MiB capture limit")
    command = [sys.executable, str(KAAL_SCRIPT)]
    if medical_action:
        command.extend(["medical", "capture"])
    else:
        command.append("capture")
    command.append(str(source))
    options = (("title", "--title"), ("sourceUrl", "--source-url"), ("sourceTitle", "--source-title"), ("capturedAt", "--captured-at"))
    for key, flag in options:
        value = optional_text(payload, key)
        if value:
            command.extend([flag, value])
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Kaal capture failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Kaal returned invalid capture output"}
    if not isinstance(result, dict) or result.get("status") != "captured" or not result.get("id") or not result.get("attachment_id"):
        return {"ok": False, "error": "Kaal capture did not confirm a saved receipt and attachment"}
    cleaned = False
    cleanup_error = ""
    # Chrome creates this staging file solely for the capture workflow. Remove it
    # only after Kaal reports that it copied the original successfully.
    if not is_local_file and payload.get("cleanupStaging") is True and is_staging_file(source, general=general_action):
        try:
            source.unlink()
            cleaned = True
        except OSError as exc:
            # The actual Kaal capture succeeded; report the leftover staging
            # file without turning success into a misleading browser failure.
            cleanup_error = str(exc)
    response: dict[str, Any] = {"ok": True, "result": result, "stagingFileCleaned": cleaned}
    if cleanup_error:
        response["stagingCleanupError"] = cleanup_error
    return response


def capture_selection(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("action") != "capture-selection":
        raise ValueError("unsupported selection action")
    selected_text = payload.get("text")
    if not isinstance(selected_text, str):
        raise ValueError("selection text must be a string")
    selected_text = selected_text.strip()
    if not selected_text or len(selected_text) > MAX_SELECTION_CHARS:
        raise ValueError(f"selection text must contain 1 to {MAX_SELECTION_CHARS:,} characters")
    command = [sys.executable, str(KAAL_SCRIPT), "capture-text"]
    options = (("title", "--title"), ("sourceUrl", "--source-url"), ("sourceTitle", "--source-title"), ("capturedAt", "--captured-at"))
    for key, flag in options:
        value = optional_text(payload, key)
        if value:
            command.extend([flag, value])
    # Keep potentially private selected text out of process arguments and any
    # process-list inspection; secure-notes reads it only from standard input.
    proc = subprocess.run(command, input=selected_text, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Kaal selection capture failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Kaal returned invalid selection-capture output"}
    if not isinstance(result, dict) or result.get("status") != "captured" or not result.get("id"):
        return {"ok": False, "error": "Kaal selection capture did not confirm a saved note"}
    return {"ok": True, "result": result}


def library_note_id(payload: dict[str, Any]) -> str:
    note_id = optional_text(payload, "id", limit=128)
    if not note_id:
        raise ValueError("id is required")
    return note_id


def library_body(payload: dict[str, Any]) -> str:
    body = payload.get("body")
    if not isinstance(body, str):
        raise ValueError("body must be a string")
    if not body.strip() or len(body) > MAX_LIBRARY_BODY_CHARS:
        raise ValueError(f"body must contain 1 to {MAX_LIBRARY_BODY_CHARS:,} characters")
    return body


def run_library(command: list[str], *, body: str | None = None) -> dict[str, Any]:
    proc = subprocess.run(command, input=body, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Kaal Library operation failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Kaal returned invalid Library output"}
    return {"ok": True, "result": result}


def manage_library(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    actions = {"library-list", "library-show", "library-create", "library-update", "library-trash", "library-restore", "library-purge", "library-attach", "library-extract", "library-extracted-text"}
    if action not in actions:
        raise ValueError("unsupported Library action")
    command = [sys.executable, str(KAAL_SCRIPT), "library"]
    if action == "library-list":
        command.extend(["list", "--limit", str(LIBRARY_LIST_LIMIT)])
        if payload.get("trash") is True:
            command.append("--trash")
        response = run_library(command)
        if response.get("ok") and not isinstance(response.get("result"), list):
            return {"ok": False, "error": "Kaal returned an invalid Library list"}
        return {"ok": response["ok"], "notes": response.get("result", []), **({"error": response["error"]} if not response["ok"] else {})}
    if action in {"library-create", "library-update"}:
        body = library_body(payload)
        title = optional_text(payload, "title", limit=500)
        if not title:
            raise ValueError("title is required")
        tags = optional_text(payload, "tags", limit=2000)
        command.append("create" if action == "library-create" else "update")
        if action == "library-update":
            command.append(library_note_id(payload))
        command.extend(["--title", title, "--tags", tags])
        if action == "library-create":
            for key, flag in (("sourceUrl", "--source-url"), ("sourceTitle", "--source-title")):
                value = optional_text(payload, key)
                if value:
                    command.extend([flag, value])
        return run_library(command, body=body)
    note_id = library_note_id(payload)
    if action == "library-attach":
        raw_path = optional_text(payload, "path")
        if not raw_path:
            raise ValueError("path is required")
        candidate = Path(raw_path).expanduser()
        if candidate.is_symlink():
            raise ValueError("symbolic-link attachment paths are not allowed")
        source = candidate.resolve()
        if not source.is_file() or not is_staging_file(source, general=True):
            raise ValueError("attachment path is outside the Kaal capture staging directory")
        if source.stat().st_size > MAX_CAPTURE_BYTES:
            raise ValueError(f"attachment exceeds the {MAX_CAPTURE_BYTES // (1024 * 1024)} MiB limit")
        response = run_library([*command, "attach", note_id, str(source)])
        if response.get("ok") and payload.get("cleanupStaging") is True:
            source.unlink(missing_ok=True)
        return response
    if action == "library-purge":
        if payload.get("confirmPermanent") is not True or optional_text(payload, "confirmationText", limit=128) != note_id:
            raise ValueError("permanent deletion requires typed note-ID confirmation")
        return run_library([*command, "purge", note_id, "--yes"])
    if action == "library-extracted-text":
        attachment_id = optional_text(payload, "attachmentId", limit=128)
        if not attachment_id:
            raise ValueError("attachmentId is required")
        response = run_library([*command, "extracted-text", note_id, "--attachment-id", attachment_id])
        if response.get("ok"):
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("text"), str):
                return {"ok": False, "error": "Kaal returned invalid extracted text"}
            result["truncated"] = len(result["text"]) > MAX_LIBRARY_TEXT_CHARS
            result["text"] = result["text"][:MAX_LIBRARY_TEXT_CHARS]
        return response
    command.extend([{"library-trash": "trash", "library-restore": "restore", "library-extract": "extract", "library-show": "show"}[action], note_id])
    return run_library(command)


def list_receipts(*, inbox: bool = False, trash: bool = False) -> dict[str, Any]:
    """Return bounded receipt metadata for the extension's local inbox page."""
    command = [sys.executable, str(KAAL_SCRIPT), "medical", "list", "--json", "--limit", str(RECEIPT_LIST_LIMIT)]
    if trash:
        command.append("--trash")
    elif inbox:
        command.append("--inbox")
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Kaal receipt list failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        receipts = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Kaal returned invalid receipt-list output"}
    if not isinstance(receipts, list):
        return {"ok": False, "error": "Kaal returned an invalid receipt list"}
    return {"ok": True, "receipts": receipts}


def selected_receipt_ids(payload: dict[str, Any]) -> list[str]:
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("ids must be a non-empty list")
    if len(raw_ids) > RECEIPT_LIST_LIMIT:
        raise ValueError("too many receipt IDs")
    ids: list[str] = []
    for value in raw_ids:
        if not isinstance(value, str):
            raise ValueError("each receipt ID must be a string")
        note_id = value.strip()
        if not note_id or len(note_id) > 128:
            raise ValueError("invalid receipt ID")
        ids.append(note_id)
    if len(ids) != len(set(ids)):
        raise ValueError("receipt IDs must be unique")
    return ids


def safe_download_filename(raw_name: str) -> str:
    filename = Path(raw_name.replace("\\", "/")).name
    suffix = Path(filename).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
        suffix = ""
    stem = filename[:-len(suffix)] if suffix else filename
    stem = re.sub(r"[^\w .()\-]+", "_", stem).strip(" .")[:120]
    return f"{stem or 'receipt'}{suffix}"


def unique_download_path(filename: str) -> Path:
    downloads = DOWNLOADS_DIR.expanduser().resolve()
    downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = safe_download_filename(filename)
    suffix = Path(safe_name).suffix
    stem = safe_name[:-len(suffix)] if suffix else safe_name
    candidate = downloads / safe_name
    sequence = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = downloads / f"{stem} ({sequence}){suffix}"
        sequence += 1
    return candidate


def download_receipt_original(payload: dict[str, Any]) -> dict[str, Any]:
    note_id = optional_text(payload, "id", limit=128)
    if not note_id:
        raise ValueError("id is required")
    attachment_id = optional_text(payload, "attachmentId", limit=128)
    if not attachment_id:
        raise ValueError("attachmentId is required")
    attachment_name = optional_text(payload, "attachmentName", limit=500)
    if not attachment_name:
        raise ValueError("attachmentName is required")
    output = unique_download_path(attachment_name)
    command = [
        sys.executable,
        str(KAAL_SCRIPT),
        "medical",
        "export-original",
        note_id,
        "--attachment-id",
        attachment_id,
        "--output",
        str(output),
    ]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Kaal original receipt export failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Kaal returned invalid original-receipt export output"}
    if not isinstance(result, dict) or result.get("status") != "exported":
        return {"ok": False, "error": "Kaal did not confirm the original receipt export"}
    reported_path = result.get("path", "")
    if not isinstance(reported_path, str) or output.is_symlink() or not output.is_file() or Path(reported_path).expanduser().resolve() != output.resolve():
        output.unlink(missing_ok=True)
        return {"ok": False, "error": "Kaal did not create the expected original receipt export"}
    result["path"] = str(output)
    result["filename"] = output.name
    return {"ok": True, "result": result}


def manage_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action not in {"trash", "restore", "purge", "purge-bulk", "review", "reopen", "extract", "extracted-text", "update", "report"}:
        raise ValueError("unsupported receipt-management action")
    ids = selected_receipt_ids(payload) if action == "purge-bulk" else []
    note_id = optional_text(payload, "id", limit=128)
    if action not in {"report", "purge-bulk"} and not note_id:
        raise ValueError("id is required")
    if action == "purge" and payload.get("confirmPermanent") is not True:
        raise ValueError("permanent deletion requires confirmPermanent")
    if action == "purge-bulk":
        if payload.get("confirmPermanent") is not True:
            raise ValueError("permanent deletion requires confirmPermanent")
        if optional_text(payload, "confirmationText", limit=RECEIPT_LIST_LIMIT * 129) != ",".join(ids):
            raise ValueError("bulk permanent deletion requires typed receipt-ID confirmation")
    if action == "report":
        command = [sys.executable, str(KAAL_SCRIPT), "medical", "report"]
        for key, flag in (("year", "--year"), ("patientName", "--patient-name"), ("fromDate", "--from-date"), ("toDate", "--to-date")):
            value = optional_text(payload, key, limit=200)
            if value: command.extend([flag, value])
    elif action == "extract":
        command = [sys.executable, str(KAAL_SCRIPT), "ocr-attachments", "--query", note_id, "--force"]
    elif action == "purge-bulk":
        command = [sys.executable, str(KAAL_SCRIPT), "medical", "purge-bulk", *ids]
    else:
        command = [sys.executable, str(KAAL_SCRIPT), "medical", action, note_id]
    if action in {"purge", "purge-bulk"}:
        command.append("--yes")
    if action == "extracted-text":
        attachment_id = optional_text(payload, "attachmentId", limit=128)
        if attachment_id:
            command.extend(["--attachment-id", attachment_id])
    if action == "update":
        fields = payload.get("fields")
        if not isinstance(fields, dict): raise ValueError("fields must be an object")
        command.extend(["--fields-json", json.dumps(fields, ensure_ascii=False)])
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"Kaal receipt {action} failed").strip()
        return {"ok": False, "error": detail[:2000]}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"Kaal returned invalid {action} output"}
    return {"ok": True, "result": result}


def handle_request(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("action"), str) and payload["action"].startswith("library-"):
        return manage_library(payload)
    if payload.get("action") == "capture-selection":
        return capture_selection(payload)
    if payload.get("action") == "list":
        return list_receipts(inbox=payload.get("inbox") is True, trash=payload.get("trash") is True)
    if payload.get("action") == "download-original":
        return download_receipt_original(payload)
    if payload.get("action") in {"trash", "restore", "purge", "purge-bulk", "review", "reopen", "extract", "extracted-text", "update", "report"}:
        return manage_receipt(payload)
    return capture(payload)


def main() -> None:
    try:
        request = read_message()
        if request is None:
            return
        write_message(handle_request(request))
    except Exception as exc:
        write_message({"ok": False, "error": str(exc)[:2000]})


if __name__ == "__main__":
    main()
