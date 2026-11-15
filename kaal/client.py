"""The kaal CLI's remote mode: the same commands, sent to kaal-server.

Remote mode is on when a server URL is configured, through ``KAAL_URL`` or a
config file (``KAAL_CONFIG``, default ``~/.config/kaal/config.toml``)::

    url = "https://storage.example.ts.net"
    token_file = "~/.config/kaal/mac-cli.token"

``KAAL_TOKEN_FILE`` or ``KAAL_TOKEN`` may supply the token instead.
``KAAL_LOCAL=1`` forces local mode. Token files, and a config file that holds
a token inline, must be readable only by their owner. Plain http is accepted
for loopback addresses only.

Each command prints exactly what the local CLI prints: the server returns the
handler's result together with the JSON options the CLI prints it with.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kaal import __version__, core

DEFAULT_CONFIG = "~/.config/kaal/config.toml"
TIMEOUT_SECONDS = 600
# Used when Python's own defaults load no CA certificates, as with python.org
# builds on macOS until their "Install Certificates" step has been run.
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",                    # macOS, Alpine
    "/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # Fedora, RHEL
)


class ClientConfigError(Exception):
    pass


@dataclass(frozen=True)
class Remote:
    url: str
    token: str
    ca_file: str | None = None


def _owner_only(path: Path, what: str) -> None:
    info = path.stat()
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ClientConfigError(f"{what} {path} must be readable only by you (chmod 600 it).")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def remote_config(environ: Mapping[str, str] | None = None) -> Remote | None:
    """Return the remote settings, or None when the CLI should work locally."""
    env = os.environ if environ is None else environ
    if env.get("KAAL_LOCAL") == "1":
        return None
    config: dict[str, Any] = {}
    config_path = Path(env.get("KAAL_CONFIG") or DEFAULT_CONFIG).expanduser()
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ClientConfigError(f"Could not read {config_path}: {error}") from None
        if config.get("token"):
            _owner_only(config_path, "Config file")
    url = (env.get("KAAL_URL") or config.get("url") or "").strip().rstrip("/")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ClientConfigError(f"Kaal server URL must be an https URL: {url}")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ClientConfigError("Kaal server URL must use https (plain http is allowed only for loopback addresses).")
    token = (env.get("KAAL_TOKEN") or "").strip()
    token_file = env.get("KAAL_TOKEN_FILE") or config.get("token_file")
    if not token and token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            raise ClientConfigError(f"Token file not found: {path}")
        _owner_only(path, "Token file")
        token = path.read_text(encoding="ascii").strip()
    if not token:
        token = str(config.get("token") or "").strip()
    if not token:
        raise ClientConfigError("A Kaal server URL is set but no API token: set token_file in the config file or KAAL_TOKEN_FILE.")
    ca_file = env.get("KAAL_CA_FILE") or config.get("ca_file")
    return Remote(url=url, token=token, ca_file=str(Path(ca_file).expanduser()) if ca_file else None)


def tls_context(ca_file: str | None, *, base: ssl.SSLContext | None = None, bundles: tuple[str, ...] = SYSTEM_CA_BUNDLES) -> ssl.SSLContext:
    """A verifying TLS context: an explicit CA file, else Python's defaults,
    else the first system CA bundle found when the defaults hold no CAs."""
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    context = base or ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) == 0:
        for bundle in bundles:
            if os.path.isfile(bundle):
                context.load_verify_locations(cafile=bundle)
                break
    return context


class Api:
    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self._ssl = tls_context(remote.ca_file) if remote.url.startswith("https") else None

    def call(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None,
             file: Path | None = None, form: dict[str, Any] | None = None, raw: bool = False) -> Any:
        url = self.remote.url + path
        if params:
            query = {k: ("true" if v is True else "false" if v is False else v) for k, v in params.items() if v not in (None, "")}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": f"Bearer {self.remote.token}", "Accept": "application/json", "User-Agent": f"kaal/{__version__}"}
        data: bytes | None = None
        if file is not None:
            data, content_type = _multipart(file, form or {})
            headers["Content-Type"] = content_type
        elif json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=self._ssl) as response:
                body = response.read()
                return (body, dict(response.headers)) if raw else json.loads(body)
        except urllib.error.HTTPError as error:
            _fail_from_response(error.code, error.read())
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            core.die(f"Cannot reach the Kaal server at {self.remote.url}: {reason}", kind="unavailable")


def _multipart(path: Path, form: dict[str, Any]) -> tuple[bytes, str]:
    boundary = "kaal-" + secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in form.items():
        if value is None:
            continue
        text = "true" if value is True else "false" if value is False else str(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + text.encode("utf-8") + b"\r\n")
    filename = path.name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"; '
              f"filename*=UTF-8''{urllib.parse.quote(path.name)}\r\nContent-Type: application/octet-stream\r\n\r\n")
    parts.append(header.encode("utf-8") + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _fail_from_response(status: int, body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = {}
    message = payload.get("error") or f"The Kaal server answered HTTP {status}."
    if status == 401:
        message = "The Kaal server rejected the token (missing, wrong or revoked)."
    matches = (payload.get("details") or {}).get("matches")
    if matches:
        core.print_ambiguous_matches(matches)
    core.die(message, kind=payload.get("kind") or "invalid")


def _print(response: dict[str, Any]) -> None:
    print(json.dumps(response["result"], **response.get("format", {})))


def _result(response: dict[str, Any]) -> Any:
    return response["result"]


def _local_file(path: str, label: str) -> Path:
    src = Path(path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        core.die(f"{label} not found: {src}")
    return src


def _write_new_file(out: Path, data: bytes, *, overwrite: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL)
    fd = os.open(out, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except Exception:
        out.unlink(missing_ok=True)
        raise
    os.chmod(out, 0o600)


def _download(api: Api, path: str) -> tuple[bytes, str]:
    data, headers = api.call("GET", path, raw=True)
    expected = {k.lower(): v for k, v in headers.items()}.get("x-kaal-sha256", "")
    digest = hashlib.sha256(data).hexdigest()
    if expected and expected != digest:
        core.die("The downloaded attachment does not match the server's checksum.")
    return data, digest


class Commands:
    """One method per CLI command, named after the core handler it replaces."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def resolve(self, query: str) -> dict[str, Any]:
        return _result(self.api.call("POST", "/v1/notes/lookup", json_body={"query": query}))["note"]

    def status(self, args: Any) -> None:
        response = self.api.call("GET", "/v1/status")
        response["result"]["server"] = self.api.remote.url
        _print(response)

    def verify_vault(self, args: Any) -> None:
        response = self.api.call("GET", "/v1/verify", params={"deep": bool(getattr(args, "deep", False))})
        _print(response)
        if response["result"].get("status") == "problems":
            raise SystemExit(2)

    def add_note(self, args: Any) -> None:
        body = core.read_stdin_or_arg(args.body, getattr(args, "body_file", None))
        _print(self.api.call("POST", "/v1/notes", json_body={"title": args.title, "tags": core.parse_tags(args.tags), "body": body, "sensitivity": getattr(args, "sensitivity", "auto")}))

    def list_notes(self, args: Any) -> None:
        response = self.api.call("GET", "/v1/notes", params={"tag": args.tag})
        _print(response) if args.json else core.print_note_lines(response["result"])

    def search_notes(self, args: Any) -> None:
        _print(self.api.call("POST", "/v1/notes/search", json_body={"query": args.query}))

    def show_note(self, args: Any) -> None:
        note = self.resolve(args.query)
        response = self.api.call("GET", f"/v1/notes/{note['id']}", params={"full": bool(args.full)})
        _print(response) if args.json else core.print_note_text(response["result"])

    def append_note(self, args: Any) -> None:
        note = self.resolve(args.query)
        body = core.read_stdin_or_arg(args.body)
        if not body.strip():
            core.die("No append text provided.")
        _print(self.api.call("POST", f"/v1/notes/{note['id']}/append", json_body={"body": body}))

    def copy_field(self, args: Any) -> None:
        note = self.resolve(args.query)
        value = _result(self.api.call("GET", f"/v1/notes/{note['id']}/fields/{urllib.parse.quote(args.field, safe='')}"))["value"]
        pbcopy = shutil.which("pbcopy")
        if not pbcopy:
            core.die("pbcopy not found; cannot copy to clipboard.")
        subprocess.run([pbcopy], input=value, text=True, check=True)
        print(json.dumps({"status": "copied", "note": note["id"], "field": args.field, "characters": len(value)}, indent=2))

    def attach_file(self, args: Any) -> None:
        src = _local_file(args.file, "Attachment file")
        note = self.resolve(args.query)
        form = {"sensitivity": getattr(args, "sensitivity", "auto"), "extract": bool(getattr(args, "extract", False)), "ocr": bool(getattr(args, "ocr", False))}
        _print(self.api.call("POST", f"/v1/notes/{note['id']}/attachments", file=src, form=form))

    def export_attachment(self, args: Any) -> None:
        note = self.resolve(args.query)
        att = core.match_attachment(note, args.attachment)
        out = Path(args.output).expanduser().resolve() if args.output else Path.cwd() / att.get("name", "attachment")
        if out.exists() and not args.force:
            core.die(f"Output exists: {out}; pass --force to overwrite.")
        data, digest = _download(self.api, f"/v1/notes/{note['id']}/attachments/{att['id']}/content")
        _write_new_file(out, data, overwrite=bool(args.force))
        print(json.dumps({"status": "exported", "path": str(out), "bytes": len(data), "sha256": digest}, indent=2))

    def export_vault(self, args: Any) -> None:
        out = Path(args.output).expanduser().resolve()
        if out.exists() or out.is_symlink():
            core.die(f"Output exists: {out}")
        data, headers = self.api.call("GET", "/v1/export", raw=True)
        lowered = {key.lower(): value for key, value in headers.items()}
        digest = hashlib.sha256(data).hexdigest()
        if lowered.get("x-kaal-sha256") and lowered["x-kaal-sha256"] != digest:
            core.die("The downloaded export does not match the server's checksum.")
        _write_new_file(out, data, overwrite=False)
        print(json.dumps({"status": "exported", "path": str(out), "bytes": len(data), "sha256": digest,
                          "notes": int(lowered.get("x-kaal-notes", 0)), "files": int(lowered.get("x-kaal-files", 0))}, indent=2))

    def ocr_attachments(self, args: Any) -> None:
        body = {k: getattr(args, k, d) for k, d in (("query", ""), ("force", False), ("dry_run", False), ("no_extract", False), ("no_ocr", False), ("limit", 0))}
        _print(self.api.call("POST", "/v1/notes/extract", json_body=body))

    def delete_note(self, args: Any) -> None:
        note = self.resolve(args.query)
        if not args.yes:
            core.die("Deletion requires --yes.")
        _print(self.api.call("DELETE", f"/v1/notes/{note['id']}", params={"confirm": True}))

    def browser_capture(self, args: Any) -> None:
        src = _local_file(args.file, "Capture file")
        _print(self.api.call("POST", "/v1/captures", file=src, form=_source_form(args)))

    def browser_capture_text(self, args: Any) -> None:
        max_chars = 100_000
        text = sys.stdin.read(max_chars + 1).strip()
        if not text:
            core.die("Selected browser text is required")
        if len(text) > max_chars:
            core.die(f"Selected browser text exceeds the {max_chars:,}-character limit")
        _print(self.api.call("POST", "/v1/captures/text", json_body={"text": text, **_source_form(args)}))

    # --- library

    def library_list(self, args: Any) -> None:
        _print(self.api.call("GET", "/v1/library/notes", params={"trash": bool(getattr(args, "trash", False)), "limit": getattr(args, "limit", 100)}))

    def library_show(self, args: Any) -> None:
        _print(self.api.call("GET", f"/v1/library/notes/{_id(args.note_id)}"))

    def library_create(self, args: Any) -> None:
        body = core.read_stdin_or_arg(getattr(args, "body", None))
        payload = {"title": args.title.strip(), "tags": core.parse_tags(args.tags), "body": body, "source_url": getattr(args, "source_url", ""), "source_title": getattr(args, "source_title", "")}
        _print(self.api.call("POST", "/v1/library/notes", json_body=payload))

    def library_update(self, args: Any) -> None:
        body = core.read_stdin_or_arg(getattr(args, "body", None))
        if not body.strip():
            core.die("Refusing to save an empty note.")
        _print(self.api.call("PUT", f"/v1/library/notes/{_id(args.note_id)}", json_body={"title": args.title.strip(), "tags": core.parse_tags(args.tags), "body": body}))

    def library_trash(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/library/notes/{_id(args.note_id)}/trash"))

    def library_restore(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/library/notes/{_id(args.note_id)}/restore"))

    def library_purge(self, args: Any) -> None:
        if not getattr(args, "yes", False):
            core.die("Permanent Library deletion requires --yes.")
        _print(self.api.call("DELETE", f"/v1/library/notes/{_id(args.note_id)}", params={"confirm": True}))

    def library_attach(self, args: Any) -> None:
        src = _local_file(args.file, "Attachment file")
        _print(self.api.call("POST", f"/v1/library/notes/{_id(args.note_id)}/attachments", file=src))

    def library_extract(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/library/notes/{_id(args.note_id)}/extract"))

    def library_extracted_text(self, args: Any) -> None:
        _print(self.api.call("GET", f"/v1/library/notes/{_id(args.note_id)}/attachments/{_id(args.attachment_id)}/extracted-text"))

    # --- medical receipts

    def medical_capture(self, args: Any) -> None:
        src = _local_file(args.file, "Receipt file")
        _print(self.api.call("POST", "/v1/medical/receipts", file=src, form=_source_form(args)))

    def medical_list(self, args: Any) -> None:
        params = {"inbox": bool(getattr(args, "inbox", False)), "trash": bool(getattr(args, "trash", False)), "limit": getattr(args, "limit", 0) or None}
        response = self.api.call("GET", "/v1/medical/receipts", params=params)
        _print(response) if getattr(args, "json", False) else core.print_note_lines(response["result"])

    def medical_review(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/medical/receipts/{_id(args.note_id)}/review"))

    def medical_reopen(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/medical/receipts/{_id(args.note_id)}/reopen"))

    def medical_extracted_text(self, args: Any) -> None:
        _print(self.api.call("GET", f"/v1/medical/receipts/{_id(args.note_id)}/extracted-text", params={"attachment_id": getattr(args, "attachment_id", "")}))

    def medical_export_original(self, args: Any) -> None:
        output = Path(args.output).expanduser().resolve()
        if not output.parent.is_dir():
            core.die("Export destination directory does not exist.")
        if output.exists() or output.is_symlink():
            core.die(f"Output exists: {output}")
        data, digest = _download(self.api, f"/v1/medical/receipts/{_id(args.note_id)}/attachments/{_id(args.attachment_id)}/content")
        _write_new_file(output, data, overwrite=False)
        print(json.dumps({"status": "exported", "id": args.note_id, "attachment_id": args.attachment_id, "path": str(output), "bytes": len(data), "sha256": digest}, ensure_ascii=False))

    def medical_update(self, args: Any) -> None:
        try:
            fields = json.loads(args.fields_json)
        except json.JSONDecodeError:
            core.die("Receipt fields must be valid JSON.")
        if not isinstance(fields, dict):
            core.die("Receipt fields are invalid.")
        _print(self.api.call("PATCH", f"/v1/medical/receipts/{_id(args.note_id)}/fields", json_body={"fields": fields}))

    def medical_report(self, args: Any) -> None:
        body = {"year": args.year or "", "patient_name": args.patient_name or "", "from_date": args.from_date or "", "to_date": args.to_date or "", "reimbursed": getattr(args, "reimbursed", "") or "all"}
        _print(self.api.call("POST", "/v1/medical/report", json_body=body))

    def medical_reimburse(self, args: Any) -> None:
        body = {"ids": list(args.note_ids), "date": getattr(args, "date", "") or "", "note": getattr(args, "note", "") or "", "undo": bool(getattr(args, "undo", False))}
        _print(self.api.call("POST", "/v1/medical/receipts/reimburse", json_body=body))

    def medical_people(self, args: Any) -> None:
        add, remove = list(getattr(args, "add", None) or []), list(getattr(args, "remove", None) or [])
        if add or remove:
            _print(self.api.call("POST", "/v1/medical/people", json_body={"add": add, "remove": remove}))
        else:
            _print(self.api.call("GET", "/v1/medical/people"))

    def medical_trash(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/medical/receipts/{_id(args.note_id)}/trash"))

    def medical_restore(self, args: Any) -> None:
        _print(self.api.call("POST", f"/v1/medical/receipts/{_id(args.note_id)}/restore"))

    def medical_purge(self, args: Any) -> None:
        if not getattr(args, "yes", False):
            core.die("Permanent receipt deletion requires --yes.")
        _print(self.api.call("DELETE", f"/v1/medical/receipts/{_id(args.note_id)}", params={"confirm": True}))

    def medical_purge_bulk(self, args: Any) -> None:
        ids = list(getattr(args, "note_ids", []))
        if not getattr(args, "yes", False):
            core.die("Permanent receipt deletion requires --yes.")
        _print(self.api.call("POST", "/v1/medical/receipts/purge", json_body={"ids": ids, "confirm": True}))


SERVER_ONLY = {"medical_evaluate": "kaal medical evaluate", "init_vault": "kaal init", "import_joplin_raw": "kaal import-joplin-raw", "import_vault": "kaal import", "backup_vault": "kaal backup"}
LOCAL_ALWAYS = {"classify_command"}


def _id(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _source_form(args: Any) -> dict[str, str]:
    return {key: getattr(args, key, "") or "" for key in ("title", "source_url", "source_title", "captured_at")}


def run(remote: Remote, args: Any) -> None:
    name = getattr(args.func, "__name__", "")
    if name in LOCAL_ALWAYS:
        args.func(args)
        return
    if name in SERVER_ONLY:
        core.die(f"`{SERVER_ONLY[name]}` runs on the server. Use it there, or set KAAL_LOCAL=1 to work on a local vault.")
    handler: Callable[[Any], None] | None = getattr(Commands(Api(remote)), name, None)
    if handler is None:
        core.die(f"This command is not available in remote mode: {name}")
    handler(args)
