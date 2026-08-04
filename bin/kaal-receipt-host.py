#!/usr/bin/env python3
"""Chrome Native Messaging host for the Kaal Receipt Capture extension."""
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
RECEIPT_LIST_LIMIT = 100
STAGING_DIRECTORY_NAME = "Kaal Capture"
DOWNLOADS_DIR = Path.home() / "Downloads"
DEFAULT_STAGING_DIR = DOWNLOADS_DIR / STAGING_DIRECTORY_NAME
STAGING_DIR = Path(os.environ.get("KAAL_RECEIPT_STAGING_DIR", str(DEFAULT_STAGING_DIR))).expanduser().resolve()
STAGING_FILE_RE = re.compile(r"^receipt-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z-.+\.pdf$")
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


def is_staging_file(path: Path) -> bool:
    # The installer pins this absolute directory to the user's Chrome download
    # location. Do not authorize a similarly named directory elsewhere.
    return path.parent == STAGING_DIR.resolve() and STAGING_FILE_RE.fullmatch(path.name) is not None


def is_download_file(path: Path) -> bool:
    try:
        return path.is_relative_to(DOWNLOADS_DIR.resolve())
    except ValueError:
        return False


def capture(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action not in {"capture", "capture-local-file"}:
        raise ValueError("unsupported action")
    raw_path = optional_text(payload, "path")
    if not raw_path:
        raise ValueError("path is required")
    candidate = Path(raw_path).expanduser()
    if candidate.is_symlink():
        raise ValueError("symbolic-link receipt paths are not allowed")
    source = candidate.resolve()
    if not source.is_file():
        raise ValueError("receipt file no longer exists")
    if action == "capture" and not is_staging_file(source):
        raise ValueError("receipt path is outside the Kaal capture staging directory")
    if action == "capture-local-file" and not is_download_file(source):
        raise ValueError("local receipt path is outside the user's Downloads directory")
    if source.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError(f"receipt exceeds the {MAX_CAPTURE_BYTES // (1024 * 1024)} MiB capture limit")
    command = [sys.executable, str(KAAL_SCRIPT), "medical", "capture", str(source)]
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
    if action == "capture" and payload.get("cleanupStaging") is True and is_staging_file(source):
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
    if payload.get("action") == "list":
        return list_receipts(inbox=payload.get("inbox") is True, trash=payload.get("trash") is True)
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
