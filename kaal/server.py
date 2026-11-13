"""kaal-server: the Kaal vault behind a small authenticated HTTP API.

The server is the only process that opens the vault. Every endpoint runs the
same core handler the local CLI runs and returns its result as
``{"result": ..., "format": {...}}``, so a remote ``kaal`` prints exactly what
a local one would.

It listens on 127.0.0.1 and is meant to be published by a TLS-terminating
proxy such as ``tailscale serve``. It never logs request bodies, query
strings, file contents or tokens.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from kaal import __version__, core, tokens

log = logging.getLogger("kaal.server")

DEFAULT_PORT = 8765
MiB = 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
STATUS_FOR_KIND = {"invalid": 400, "not_found": 404, "conflict": 409, "unavailable": 503}
KIND_FOR_STATUS = {400: "invalid", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "invalid", 409: "conflict", 413: "too_large", 429: "rate_limited"}

Sensitivity = Literal["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"]
Title = Field(min_length=1, max_length=500)
Body = Field(min_length=0, max_length=1_000_000)
Short = Field(default="", max_length=4096)


class ServerConfigError(Exception):
    pass


@dataclass
class Settings:
    home: Path
    vault: Path
    tokens_file: Path
    tmp_dir: Path
    max_upload_bytes: int = 100 * MiB
    rate_per_minute: int = 240
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        home = Path(env.get("KAAL_HOME") or "~/.kaal").expanduser()
        vault = env.get("KAAL_VAULT") or env.get("SECURE_NOTES_VAULT") or str(home / "data")
        return cls(
            home=home,
            vault=Path(vault).expanduser(),
            tokens_file=Path(env.get("KAAL_TOKENS_FILE") or home / "tokens.json").expanduser(),
            tmp_dir=Path(env.get("KAAL_TMP_DIR") or home / "tmp").expanduser(),
            max_upload_bytes=int(env.get("KAAL_MAX_UPLOAD_BYTES") or 100 * MiB),
            rate_per_minute=int(env.get("KAAL_RATE_PER_MINUTE") or 240),
            host=env.get("KAAL_BIND") or "127.0.0.1",
            port=int(env.get("KAAL_PORT") or DEFAULT_PORT),
        )


class RateLimiter:
    """A token bucket per key: ``per_minute`` requests, refilled continuously."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens_left, last = self._buckets.get(key, (float(self.per_minute), now))
            tokens_left = min(float(self.per_minute), tokens_left + (now - last) * self.per_minute / 60.0)
            if tokens_left < 1:
                self._buckets[key] = (tokens_left, now)
                return False
            self._buckets[key] = (tokens_left - 1, now)
            return True


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """Reject request bodies over a byte limit, declared or streamed."""

    def __init__(self, app: Any, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.limit:
            await _send_json(send, 413, {"error": "Request is too large.", "kind": "too_large"})
            return
        received = 0
        started = False

        async def limited_receive() -> dict:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _TooLarge()
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not started:
                await _send_json(send, 413, {"error": "Request is too large.", "kind": "too_large"})


async def _send_json(send: Callable, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


@dataclass
class AppState:
    settings: Settings
    tokens: tokens.TokenStore
    limiter: RateLimiter
    vault_lock: threading.Lock


def _error(status: int, message: str, kind: str | None = None, details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": message, "kind": kind or KIND_FOR_STATUS.get(status, "error")}
    if details:
        payload["details"] = details
    return JSONResponse(payload, status_code=status, headers=headers)


def require(*scopes: str) -> Callable:
    def dependency(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.kaal
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        record = state.tokens.authenticate(presented.strip()) if scheme.lower() == "bearer" else None
        if record is None:
            if not state.limiter.allow("unauthenticated"):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            raise HTTPException(401, "A valid bearer token is required.", headers={"WWW-Authenticate": "Bearer"})
        request.state.token_name = record["name"]
        if not state.limiter.allow(record["id"]):
            raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
        missing = [scope for scope in scopes if scope not in record.get("scopes", [])]
        if missing:
            raise HTTPException(403, f"This token lacks the {', '.join(missing)} scope.")
        return record

    return Depends(dependency)


def _run(state: AppState, handler: Callable[[Any], None], *, exact_note: str | None = None, **params: Any) -> JSONResponse:
    """Run a core handler under the server lock; ``exact_note`` must be an existing note id."""
    with state.vault_lock:
        try:
            if exact_note is not None:
                core.note_by_id(exact_note)
            result, options = core.call_captured(handler, SimpleNamespace(**params))
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
    return JSONResponse({"result": result, "format": options})


def _upload_name(raw: str | None) -> str:
    name = Path((raw or "").replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f/]", "_", name).strip()
    if name in {"", ".", ".."}:
        name = "upload"
    if len(name.encode("utf-8")) > 255:
        name = core.safe_attachment_filename(name, "upload")
    return name


@contextlib.contextmanager
def _saved_upload(state: AppState, upload: UploadFile) -> Iterator[Path]:
    """Copy an upload into a private folder (mode 700), enforcing the size limit."""
    state.settings.tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="upload-", dir=state.settings.tmp_dir))
    try:
        dest = workdir / _upload_name(upload.filename)
        size = 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while chunk := upload.file.read(MiB):
                size += len(chunk)
                if size > state.settings.max_upload_bytes:
                    raise HTTPException(413, f"File is larger than the {state.settings.max_upload_bytes // MiB} MiB limit.")
                out.write(chunk)
        yield dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _content_response(state: AppState, note_id: str, attachment_id: str, *, receipt: bool = False) -> Response:
    with state.vault_lock:
        try:
            meta = core.medical_receipt_meta(core.load_index(), note_id) if receipt else core.note_by_id(note_id)
            matches = [att for att in meta.get("attachments", []) if att.get("id") == attachment_id]
            if not matches:
                core.die("Attachment not found.", kind="not_found")
            data = core.attachment_original_bytes(meta, matches[0])
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Kaal-Attachment-Id": attachment_id,
            "X-Kaal-Attachment-Name": quote(matches[0].get("name", ""), safe=""),
            "X-Kaal-Sha256": hashlib.sha256(data).hexdigest(),
        },
    )


def _lookup(args: Any) -> None:
    core.init_if_needed()
    matches = core.find_notes(args.query)
    if not matches:
        core.die(f"No note found matching: {args.query}", kind="not_found")
    if len(matches) > 1:
        core.die("Use the note id to disambiguate.", kind="conflict", details={"matches": [{"id": n["id"], "title": n.get("title", "")} for n in matches]})
    core.emit({"note": matches[0]}, indent=2, ensure_ascii=False)


def _field(args: Any) -> None:
    note_id, value = core.note_field_value(args.query, args.field)
    core.emit({"note": note_id, "field": args.field, "value": value})


def _tags(tags: list[str]) -> str:
    return ",".join(tags)


# --- request bodies


class NoteCreate(BaseModel):
    title: str = Title
    body: str = Body
    tags: list[str] = Field(default_factory=list, max_length=100)
    sensitivity: Sensitivity = "auto"


class TextBody(BaseModel):
    body: str = Body


class QueryBody(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class TextCapture(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    title: str = Short
    source_url: str = Short
    source_title: str = Short
    captured_at: str = Field(default="", max_length=64)


class LibraryCreate(BaseModel):
    title: str = Title
    body: str = Body
    tags: list[str] = Field(default_factory=list, max_length=100)
    source_url: str = Short
    source_title: str = Short


class LibraryUpdate(BaseModel):
    title: str = Title
    body: str = Body
    tags: list[str] = Field(default_factory=list, max_length=100)


class ExtractRun(BaseModel):
    query: str = Field(default="", max_length=500)
    force: bool = False
    dry_run: bool = False
    no_extract: bool = False
    no_ocr: bool = False
    limit: int = Field(default=0, ge=0)


class ReceiptFields(BaseModel):
    fields: dict[str, str]


class ReportFilter(BaseModel):
    year: str = Field(default="", max_length=10)
    patient_name: str = Field(default="", max_length=200)
    from_date: str = Field(default="", max_length=32)
    to_date: str = Field(default="", max_length=32)


class BulkPurge(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    confirm: bool = False


def create_app(settings: Settings) -> FastAPI:
    vault = core.configure_vault(settings.vault)
    if not (vault / "index.json").is_file():
        raise ServerConfigError(f"Vault not found at {vault}: refusing to start. Restore it, or run `kaal init` there first.")
    settings.tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.tmp_dir, 0o700)

    app = FastAPI(title="Kaal", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  description="An encrypted notes vault. Every request needs `Authorization: Bearer <token>` except /v1/health.")
    app.state.kaal = AppState(settings, tokens.TokenStore(settings.tokens_file), RateLimiter(settings.rate_per_minute), threading.Lock())
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_upload_bytes + MiB)

    @app.middleware("http")
    async def access_log(request: Request, call_next: Callable) -> Response:
        started = time.monotonic()
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        template = getattr(route, "path", "(unmatched)")
        token_name = getattr(request.state, "token_name", "-")
        log.info("%s %s %s %s %dms", request.method, template, response.status_code, token_name, (time.monotonic() - started) * 1000)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Name the fields only: FastAPI's default response echoes the submitted values.
        fields = sorted({".".join(str(part) for part in err.get("loc", ())) for err in exc.errors()})
        return _error(400, "The request is not valid.", "invalid", {"fields": fields})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s", type(exc).__name__)
        return _error(500, "Internal error.", "internal")

    S = lambda request: request.app.state.kaal  # noqa: E731

    @app.get("/v1/health", tags=["service"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/status", tags=["service"], dependencies=[require("read")])
    def status(request: Request):
        return _run(S(request), core.status)

    @app.get("/v1/verify", tags=["service"], dependencies=[require("read")])
    def verify(request: Request, deep: bool = False):
        return _run(S(request), core.verify_vault, deep=deep)

    @app.get("/v1/export", tags=["service"], dependencies=[require("read", "reveal", "admin")], response_class=Response)
    def export(request: Request):
        state = S(request)
        workdir = Path(tempfile.mkdtemp(prefix="export-", dir=state.settings.tmp_dir))
        out = workdir / "kaal-export.tar"
        with state.vault_lock:
            try:
                result, _ = core.call_captured(core.export_vault, SimpleNamespace(output=str(out)))
            except core.KaalError as error:
                shutil.rmtree(workdir, ignore_errors=True)
                return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        headers = {"X-Kaal-Sha256": result["sha256"], "X-Kaal-Notes": str(result["notes"]), "X-Kaal-Files": str(result["files"])}
        return FileResponse(out, media_type="application/x-tar", filename="kaal-export.tar", headers=headers, background=BackgroundTask(shutil.rmtree, workdir, True))

    # --- notes

    @app.get("/v1/notes", tags=["notes"], dependencies=[require("read")])
    def list_notes(request: Request, tag: str = Query("", max_length=200)):
        return _run(S(request), core.list_notes, tag=tag, json=True)

    @app.post("/v1/notes", tags=["notes"], dependencies=[require("write")])
    def add_note(request: Request, note: NoteCreate):
        return _run(S(request), core.add_note, title=note.title, tags=_tags(note.tags), body=note.body, body_file=None, sensitivity=note.sensitivity)

    @app.post("/v1/notes/search", tags=["notes"], dependencies=[require("read")])
    def search_notes(request: Request, body: QueryBody):
        return _run(S(request), core.search_notes, query=body.query)

    @app.post("/v1/notes/lookup", tags=["notes"], dependencies=[require("read")])
    def lookup_note(request: Request, body: QueryBody):
        return _run(S(request), _lookup, query=body.query)

    @app.post("/v1/notes/extract", tags=["notes"], dependencies=[require("write")])
    def extract_attachments(request: Request, run: ExtractRun):
        return _run(S(request), core.ocr_attachments, **run.model_dump())

    @app.get("/v1/notes/{note_id}", tags=["notes"])
    def show_note(request: Request, note_id: str, full: bool = False, token: dict = require("read")):
        if full and "reveal" not in token["scopes"]:
            raise HTTPException(403, "This token lacks the reveal scope.")
        return _run(S(request), core.show_note, exact_note=note_id, query=note_id, full=full, json=True)

    @app.post("/v1/notes/{note_id}/append", tags=["notes"], dependencies=[require("write")])
    def append_note(request: Request, note_id: str, body: TextBody):
        return _run(S(request), core.append_note, exact_note=note_id, query=note_id, body=body.body)

    @app.delete("/v1/notes/{note_id}", tags=["notes"], dependencies=[require("delete")])
    def delete_note(request: Request, note_id: str, confirm: bool = False):
        return _run(S(request), core.delete_note, exact_note=note_id, query=note_id, yes=confirm)

    @app.get("/v1/notes/{note_id}/fields/{field}", tags=["notes"], dependencies=[require("read", "reveal")])
    def field_value(request: Request, note_id: str, field: str):
        return _run(S(request), _field, exact_note=note_id, query=note_id, field=field)

    @app.post("/v1/notes/{note_id}/attachments", tags=["notes"], dependencies=[require("write")])
    def attach(request: Request, note_id: str, file: UploadFile = File(...), sensitivity: Sensitivity = Form("auto"), extract: bool = Form(False), ocr: bool = Form(False)):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.attach_file, exact_note=note_id, query=note_id, file=str(path), sensitivity=sensitivity, extract=extract, ocr=ocr)

    @app.get("/v1/notes/{note_id}/attachments/{attachment_id}/content", tags=["notes"], dependencies=[require("read", "reveal")], response_class=Response)
    def attachment_content(request: Request, note_id: str, attachment_id: str):
        return _content_response(S(request), note_id, attachment_id)

    # --- browser captures

    @app.post("/v1/captures", tags=["captures"], dependencies=[require("write")])
    def capture(request: Request, file: UploadFile = File(...), title: str = Form(""), source_url: str = Form(""), source_title: str = Form(""), captured_at: str = Form("")):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.browser_capture, file=str(path), title=title, source_url=source_url, source_title=source_title, captured_at=captured_at)

    @app.post("/v1/captures/text", tags=["captures"], dependencies=[require("write")])
    def capture_text(request: Request, body: TextCapture):
        return _run(S(request), core.browser_capture_text, **body.model_dump())

    # --- library (non-medical notes for the dashboard)

    @app.get("/v1/library/notes", tags=["library"], dependencies=[require("read")])
    def library_list(request: Request, trash: bool = False, limit: int = Query(100, ge=0, le=100)):
        return _run(S(request), core.library_list, trash=trash, limit=limit)

    @app.post("/v1/library/notes", tags=["library"], dependencies=[require("write")])
    def library_create(request: Request, note: LibraryCreate):
        return _run(S(request), core.library_create, title=note.title, tags=_tags(note.tags), body=note.body, source_url=note.source_url, source_title=note.source_title)

    @app.get("/v1/library/notes/{note_id}", tags=["library"], dependencies=[require("read", "reveal")])
    def library_show(request: Request, note_id: str):
        return _run(S(request), core.library_show, note_id=note_id)

    @app.put("/v1/library/notes/{note_id}", tags=["library"], dependencies=[require("write")])
    def library_update(request: Request, note_id: str, note: LibraryUpdate):
        return _run(S(request), core.library_update, note_id=note_id, title=note.title, tags=_tags(note.tags), body=note.body)

    @app.post("/v1/library/notes/{note_id}/trash", tags=["library"], dependencies=[require("write")])
    def library_trash(request: Request, note_id: str):
        return _run(S(request), core.library_trash, note_id=note_id)

    @app.post("/v1/library/notes/{note_id}/restore", tags=["library"], dependencies=[require("write")])
    def library_restore(request: Request, note_id: str):
        return _run(S(request), core.library_restore, note_id=note_id)

    @app.delete("/v1/library/notes/{note_id}", tags=["library"], dependencies=[require("delete")])
    def library_purge(request: Request, note_id: str, confirm: bool = False):
        return _run(S(request), core.library_purge, note_id=note_id, yes=confirm)

    @app.post("/v1/library/notes/{note_id}/attachments", tags=["library"], dependencies=[require("write")])
    def library_attach(request: Request, note_id: str, file: UploadFile = File(...)):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.library_attach, note_id=note_id, file=str(path))

    @app.post("/v1/library/notes/{note_id}/extract", tags=["library"], dependencies=[require("write")])
    def library_extract(request: Request, note_id: str):
        return _run(S(request), core.library_extract, note_id=note_id)

    @app.get("/v1/library/notes/{note_id}/attachments/{attachment_id}/extracted-text", tags=["library"], dependencies=[require("read", "reveal")])
    def library_extracted_text(request: Request, note_id: str, attachment_id: str):
        return _run(S(request), core.library_extracted_text, note_id=note_id, attachment_id=attachment_id)

    # --- medical receipts (fields are medical data, so reading them needs reveal)

    @app.post("/v1/medical/receipts", tags=["medical"], dependencies=[require("write")])
    def medical_capture(request: Request, file: UploadFile = File(...), title: str = Form(""), source_url: str = Form(""), source_title: str = Form(""), captured_at: str = Form("")):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.medical_capture, file=str(path), title=title, source_url=source_url, source_title=source_title, captured_at=captured_at)

    @app.get("/v1/medical/receipts", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_list(request: Request, inbox: bool = False, trash: bool = False, limit: int = Query(0, ge=0)):
        if inbox and trash:
            raise HTTPException(400, "Choose inbox or trash, not both.")
        return _run(S(request), core.medical_list, inbox=inbox, trash=trash, json=True, limit=limit)

    @app.post("/v1/medical/report", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_report(request: Request, body: ReportFilter):
        return _run(S(request), core.medical_report, **body.model_dump())

    @app.post("/v1/medical/receipts/purge", tags=["medical"], dependencies=[require("delete")])
    def medical_purge_bulk(request: Request, body: BulkPurge):
        return _run(S(request), core.medical_purge_bulk, note_ids=body.ids, yes=body.confirm)

    @app.post("/v1/medical/receipts/{note_id}/review", tags=["medical"], dependencies=[require("write")])
    def medical_review(request: Request, note_id: str):
        return _run(S(request), core.medical_review, note_id=note_id)

    @app.post("/v1/medical/receipts/{note_id}/reopen", tags=["medical"], dependencies=[require("write")])
    def medical_reopen(request: Request, note_id: str):
        return _run(S(request), core.medical_reopen, note_id=note_id)

    @app.patch("/v1/medical/receipts/{note_id}/fields", tags=["medical"], dependencies=[require("write")])
    def medical_update(request: Request, note_id: str, body: ReceiptFields):
        return _run(S(request), core.medical_update, note_id=note_id, fields_json=json.dumps(body.fields))

    @app.get("/v1/medical/receipts/{note_id}/extracted-text", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_extracted_text(request: Request, note_id: str, attachment_id: str = ""):
        return _run(S(request), core.medical_extracted_text, note_id=note_id, attachment_id=attachment_id)

    @app.get("/v1/medical/receipts/{note_id}/attachments/{attachment_id}/content", tags=["medical"], dependencies=[require("read", "reveal")], response_class=Response)
    def medical_content(request: Request, note_id: str, attachment_id: str):
        return _content_response(S(request), note_id, attachment_id, receipt=True)

    @app.post("/v1/medical/receipts/{note_id}/trash", tags=["medical"], dependencies=[require("write")])
    def medical_trash(request: Request, note_id: str):
        return _run(S(request), core.medical_trash, note_id=note_id)

    @app.post("/v1/medical/receipts/{note_id}/restore", tags=["medical"], dependencies=[require("write")])
    def medical_restore(request: Request, note_id: str):
        return _run(S(request), core.medical_restore, note_id=note_id)

    @app.delete("/v1/medical/receipts/{note_id}", tags=["medical"], dependencies=[require("delete")])
    def medical_purge(request: Request, note_id: str, confirm: bool = False):
        return _run(S(request), core.medical_purge, note_id=note_id, yes=confirm)

    return app


def openapi_spec(app: FastAPI) -> dict[str, Any]:
    spec = app.openapi()
    spec.setdefault("components", {})["securitySchemes"] = {"bearer": {"type": "http", "scheme": "bearer"}}
    spec["security"] = [{"bearer": []}]
    return spec


# --- kaal-server command line


def _serve(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port
    if host not in LOOPBACK and os.environ.get("KAAL_ALLOW_NON_LOOPBACK") != "1":
        sys.exit(f"Refusing to listen on {host}: kaal-server listens on loopback and is published by a TLS proxy. Set KAAL_ALLOW_NON_LOOPBACK=1 to override (for example inside a container).")
    try:
        app = create_app(settings)
    except ServerConfigError as error:
        sys.exit(str(error))
    import uvicorn

    # Multipart parsing spools large uploads to disk: keep that in our private folder.
    tempfile.tempdir = str(settings.tmp_dir)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=host, port=port, access_log=False, server_header=False, proxy_headers=False, log_level="warning")


def _token_create(args: argparse.Namespace) -> None:
    store = tokens.TokenStore(Settings.from_env().tokens_file)
    try:
        record, secret = store.create(args.name, args.scopes.split(","))
    except tokens.TokenError as error:
        sys.exit(f"ERROR: {error}")
    if args.output:
        try:
            path = tokens.write_secret_file(args.output, secret)
        except FileExistsError:
            store.revoke(record["id"])
            sys.exit(f"ERROR: {args.output} already exists; the new token was revoked.")
        print(json.dumps({"status": "created", "token": record, "written_to": str(path)}, indent=2))
    else:
        print(json.dumps({"status": "created", "token": record}, indent=2), file=sys.stderr)
        print("The token is shown once. Store it now:", file=sys.stderr)
        print(secret)


def _token_list(_args: argparse.Namespace) -> None:
    print(json.dumps(tokens.TokenStore(Settings.from_env().tokens_file).list(), indent=2))


def _token_revoke(args: argparse.Namespace) -> None:
    try:
        record = tokens.TokenStore(Settings.from_env().tokens_file).revoke(args.name)
    except tokens.TokenError as error:
        sys.exit(f"ERROR: {error}")
    print(json.dumps({"status": "revoked", "token": record}, indent=2))


def _openapi(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        (vault / "index.json").write_text('{"version": 1, "notes": []}')
        saved = core.VAULT
        try:
            spec = openapi_spec(create_app(Settings(home=Path(td), vault=vault, tokens_file=Path(td) / "t.json", tmp_dir=Path(td) / "tmp")))
        finally:
            core.configure_vault(saved)
    text = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kaal-server", description="Serve a Kaal vault over an authenticated HTTP API.")
    parser.add_argument("--version", action="version", version=f"kaal-server {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the API server (loopback only)")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=_serve)
    token = sub.add_parser("token", help="Issue, list and revoke API tokens")
    token_sub = token.add_subparsers(dest="token_cmd", required=True)
    create = token_sub.add_parser("create", help="Issue a token; it is shown once")
    create.add_argument("name")
    create.add_argument("--scopes", required=True, help=f"Comma-separated: {', '.join(tokens.SCOPES)}")
    create.add_argument("--output", default="", help="Write the token to this new file (mode 600) instead of printing it")
    create.set_defaults(func=_token_create)
    token_sub.add_parser("list", help="List tokens (never their secrets)").set_defaults(func=_token_list)
    revoke = token_sub.add_parser("revoke", help="Revoke a token by name or id")
    revoke.add_argument("name")
    revoke.set_defaults(func=_token_revoke)
    spec = sub.add_parser("openapi", help="Print the OpenAPI spec")
    spec.add_argument("--output", default="")
    spec.set_defaults(func=_openapi)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
