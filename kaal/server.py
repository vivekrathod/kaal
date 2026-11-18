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
import asyncio
from datetime import datetime, timezone
import contextlib
import functools
import gc
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
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

from kaal import __version__, accounts, core, oidc, passwords, tokens

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


# The id Chrome gives Kaal's extension, which follows from the key in its manifest.
OFFICIAL_EXTENSION_ID = "ebmkcbpcihgiaoimcpncmadadogclmld"
CORS_METHODS = b"GET, POST, PUT, PATCH, DELETE"
CORS_HEADERS = b"Authorization, Content-Type, Accept, X-Kaal-Request"


def extension_origins(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Origins of the browser extensions allowed to call the API from their pages.

    Kaal's own extension by default. KAAL_EXTENSION_IDS replaces the list (comma
    separated ids, for a build with a different key); "none" allows no extension.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("KAAL_EXTENSION_IDS") or "").strip()
    if not raw:
        return (f"chrome-extension://{OFFICIAL_EXTENSION_ID}",)
    if raw.lower() == "none":
        return ()
    ids = [item.strip() for item in raw.split(",") if item.strip()]
    bad = [item for item in ids if not re.fullmatch(r"[a-p]{32}", item)]
    if bad:
        raise ServerConfigError("KAAL_EXTENSION_IDS must be Chrome extension ids (32 letters a to p), separated by commas.")
    return tuple(f"chrome-extension://{item}" for item in ids)


@dataclass
class Settings:
    home: Path
    # The vault a single-vault installation had; adopted as the first account's on first start.
    vault: Path
    tokens_file: Path
    tmp_dir: Path
    # Who has an account and who belongs to which vault. Empty means home/accounts.db.
    accounts_file: Path | None = None
    # When sign-up is open, how many accounts may be created per hour.
    signups_per_hour: int = 10
    # How many vaults one person may create for themselves.
    vaults_per_person: int = 10
    max_upload_bytes: int = 100 * MiB
    rate_per_minute: int = 600
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    # Read captured receipts after the capture request has returned. `serve`
    # turns this on (KAAL_BACKGROUND_EXTRACTION=0 turns it off).
    background_extraction: bool = False
    # Browsers only send the session cookie over HTTPS. KAAL_COOKIE_SECURE=0
    # allows plain http, for development against a non-loopback address.
    cookie_secure: bool = True
    # Browser extensions whose pages may call the API (see ExtensionCorsMiddleware).
    extension_origins: tuple[str, ...] = (f"chrome-extension://{OFFICIAL_EXTENSION_ID}",)
    # The address people open Kaal at, such as https://kaal.example.org. Sign-in
    # services send the browser back to it. An administrator can also set it in the web app.
    public_url: str = ""
    # Proxies whose X-Forwarded-For is believed, so limits apply per visitor rather than per proxy.
    trusted_proxies: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    # Requests a minute one address may make to the routes that need no sign-in.
    strangers_per_minute: int = 30
    # Passwords checked at the same moment; more wait briefly, then are told to try again.
    password_checks: int = 4
    # The most one vault may hold, in bytes (0: no limit). The installation's first vault has no limit.
    vault_quota_bytes: int = 10 * 1024 * MiB
    # Files being downloaded are held in memory until the client has them; this is the most held at once.
    download_memory_bytes: int = 1024 * MiB
    # Nothing new is accepted, and no export is written, when the disk has less than this free (0: no floor).
    free_space_bytes: int = 1024 * MiB

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        home = Path(env.get("KAAL_HOME") or "~/.kaal").expanduser()
        vault = env.get("KAAL_VAULT") or str(home / "data")
        return cls(
            home=home,
            vault=Path(vault).expanduser(),
            tokens_file=Path(env.get("KAAL_TOKENS_FILE") or home / "tokens.json").expanduser(),
            tmp_dir=Path(env.get("KAAL_TMP_DIR") or home / "tmp").expanduser(),
            accounts_file=Path(env.get("KAAL_ACCOUNTS_FILE") or home / "accounts.db").expanduser(),
            signups_per_hour=int(env.get("KAAL_SIGNUPS_PER_HOUR") or 10),
            vaults_per_person=int(env.get("KAAL_VAULTS_PER_PERSON") or 10),
            max_upload_bytes=int(env.get("KAAL_MAX_UPLOAD_BYTES") or 100 * MiB),
            rate_per_minute=int(env.get("KAAL_RATE_PER_MINUTE") or 600),
            host=env.get("KAAL_BIND") or "127.0.0.1",
            port=int(env.get("KAAL_PORT") or DEFAULT_PORT),
            background_extraction=(env.get("KAAL_BACKGROUND_EXTRACTION") or "1").strip() != "0",
            cookie_secure=(env.get("KAAL_COOKIE_SECURE") or "1").strip() != "0",
            extension_origins=extension_origins(env),
            public_url=(env.get("KAAL_PUBLIC_URL") or "").strip().rstrip("/"),
            trusted_proxies=trusted_proxy_list(env.get("KAAL_TRUSTED_PROXIES") or ""),
            strangers_per_minute=int(env.get("KAAL_SIGN_IN_PER_MINUTE") or 30),
            password_checks=int(env.get("KAAL_PASSWORD_CHECKS") or 4),
            vault_quota_bytes=max(0, int(env.get("KAAL_VAULT_QUOTA_MB") or 10 * 1024)) * MiB,
            download_memory_bytes=max(1, int(env.get("KAAL_DOWNLOAD_MEMORY_MB") or 1024)) * MiB,
            free_space_bytes=max(0, int(env.get("KAAL_FREE_SPACE_MB") or 1024)) * MiB,
        )


PRIVATE_NETWORKS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")
STRANGER_BODY_BYTES = 1 * 1024 * 1024
MAX_TOKENS_PER_PERSON = 50
TOKENS_PER_HOUR = 20
# Approving an app or the extension needs a sign-in this recent, or the password again.
APPROVE_WITHIN_SECONDS = 600
JSON_BODY_BYTES = 8 * 1024 * 1024


def trusted_proxy_list(text: str) -> tuple[str, ...]:
    """KAAL_TRUSTED_PROXIES: addresses or networks, comma-separated; "private" for every private
    network (right when only a proxy on the same machine or network can reach Kaal); "none"."""
    if not text.strip():
        return ("127.0.0.1/32", "::1/128")
    chosen: list[str] = []
    for part in (item.strip().lower() for item in text.split(",")):
        if not part:
            continue
        if part == "none":
            return ()
        if part == "private":
            chosen += PRIVATE_NETWORKS
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"KAAL_TRUSTED_PROXIES: {part!r} is not an address or network") from None
        chosen.append(part)
    return tuple(chosen)


@functools.lru_cache(maxsize=16)
def _networks(listed: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in listed)


def _is_trusted(address: str, listed: tuple[str, ...]) -> bool:
    try:
        parsed = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return any(parsed in network for network in _networks(listed))


def client_address(request: Request) -> str:
    """Who is asking: the connecting address, or, behind a trusted proxy, the first address in
    X-Forwarded-For that is not a trusted proxy. An IPv6 address counts as its /64."""
    listed = request.app.state.kaal.settings.trusted_proxies
    address = request.client.host if request.client else ""
    if _is_trusted(address, listed):
        hops = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",") if hop.strip()]
        for hop in reversed(hops):
            address = hop
            if not _is_trusted(hop, listed):
                break
    try:
        parsed = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return "unknown"
    if parsed.version == 6:
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def log_safe(text: str, limit: int = 80) -> str:
    """Text from outside (a token's name, a method, a provider's message) made safe for one log line."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]", "?", str(text))[:limit]


class ExtractionWorker:
    """Reads receipts that were captured or queued but not yet read.

    Capture only stores the original and marks the receipt pending; this
    worker does the slow part afterwards, so a capture returns at once and the
    request lock is held only while a result is written. Pending receipts are
    found in the vault itself, so work left over from a restart is picked up.
    """

    def __init__(self, vaults: Callable[[], list[core.Vault]], idle_seconds: float = 300.0) -> None:
        self.vaults = vaults
        self.idle_seconds = idle_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        self._wake.set()

    def drain(self) -> int:
        """Read every pending receipt once; return how many were attempted."""
        attempted = 0
        for vault in self.vaults():
            with core.use_vault(vault):
                for note_id in core.pending_receipt_ids():
                    if self._stop.is_set():
                        return attempted
                    attempted += 1
                    try:
                        outcome = core.reextract_receipt(note_id, outer_lock=vault.request_lock)
                        log.info("receipt read: %s", outcome)
                    except (Exception, core.KaalError) as error:
                        # Leave it pending for the next pass; never log a receipt's contents.
                        log.error("receipt read failed: %s", type(error).__name__)
        return attempted

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.drain()
            self._wake.wait(self.idle_seconds)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="kaal-receipt-extraction", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)


class RateLimiter:
    """A token bucket per key: ``per_minute`` requests, refilled continuously."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    REMEMBERED = 20_000

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) >= self.REMEMBERED and key not in self._buckets:
                # A bucket untouched for a minute is full again; forgetting it changes nothing.
                self._buckets = {name: entry for name, entry in self._buckets.items() if now - entry[1] < 60}
                if len(self._buckets) >= self.REMEMBERED:
                    self._buckets.pop(next(iter(self._buckets)))
            tokens_left, last = self._buckets.get(key, (float(self.per_minute), now))
            tokens_left = min(float(self.per_minute), tokens_left + (now - last) * self.per_minute / 60.0)
            if tokens_left < 1:
                self._buckets[key] = (tokens_left, now)
                return False
            self._buckets[key] = (tokens_left - 1, now)
            return True


class Usage:
    """How much each vault holds on disk, counted at most every half minute."""

    FRESH_SECONDS = 30.0

    def __init__(self) -> None:
        self._known: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _count(folder: Path) -> int:
        total = 0
        for directory, _names, files in os.walk(folder):
            for name in files:
                try:
                    total += os.lstat(os.path.join(directory, name)).st_size
                except OSError:
                    pass
        return total

    def used(self, folder: Path) -> int:
        key, now = str(folder), time.monotonic()
        with self._lock:
            known = self._known.get(key)
        if known is not None and now - known[0] < self.FRESH_SECONDS:
            return known[1]
        total = self._count(folder)
        with self._lock:
            self._known[key] = (now, total)
        return total

    def add(self, folder: Path, size: int) -> None:
        """Count what is about to arrive, so many uploads at once cannot all slip under the limit."""
        with self._lock:
            known = self._known.get(str(folder))
            if known is not None:
                self._known[str(folder)] = (known[0], known[1] + max(0, size))

    def forget(self, folder: Path) -> None:
        with self._lock:
            self._known.pop(str(folder), None)


class Downloads:
    """Files on their way out. A file is decrypted whole and stays in memory until the client has
    taken it, so a client that asks and never reads could otherwise pin as much memory as it liked."""

    PER_CALLER = 6

    def __init__(self, limit_bytes: int, per_caller: int = PER_CALLER) -> None:
        self.limit = limit_bytes
        self.per_caller = per_caller
        self._held = 0
        self._by_caller: dict[str, int] = {}
        self._lock = threading.Lock()

    def take(self, caller: str, size: int) -> tuple[str, int] | None:
        with self._lock:
            if self._by_caller.get(caller, 0) >= self.per_caller:
                return None
            # One file is always allowed, however large; more only while they fit.
            if self._held and self._held + size > self.limit:
                return None
            self._held += size
            self._by_caller[caller] = self._by_caller.get(caller, 0) + 1
            return caller, size

    def give(self, ticket: tuple[str, int]) -> None:
        caller, size = ticket
        with self._lock:
            self._held = max(0, self._held - size)
            left = self._by_caller.get(caller, 0) - 1
            if left > 0:
                self._by_caller[caller] = left
            else:
                self._by_caller.pop(caller, None)


class HeldDownloadsMiddleware:
    """Gives back what Downloads counted once a response has left, or its client has gone.

    It sits outside everything else so that "left" means written to the client, not handed to
    another layer of the server."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        held: list[Callable[[], None]] = []
        scope["kaal_held"] = held
        try:
            await self.app(scope, receive, send)
        finally:
            for give_back in held:
                give_back()


def held_download(request: Request, size: int, pool: Downloads | None = None) -> JSONResponse | None:
    """Count a file about to be sent; an error answer when this caller, or the server, holds too many."""
    pool = pool or request.app.state.kaal.downloads
    ticket = pool.take(getattr(request.state, "caller", "-"), size)
    if ticket is None:
        return _error(503, "Kaal is sending as many files as it can hold at once. Try again in a moment.", "busy", headers={"Retry-After": "5"})
    request.scope.setdefault("kaal_held", []).append(lambda: pool.give(ticket))
    return None


def disk_is_full(settings: "Settings", needed: int = 0) -> bool:
    """Whether storing ``needed`` more bytes would leave the disk under its floor of free space."""
    if not settings.free_space_bytes:
        return False
    try:
        free = shutil.disk_usage(settings.home).free
    except OSError:
        return False
    return free - max(0, needed) < settings.free_space_bytes


FULL_DISK = "This Kaal's disk is nearly full, so nothing new can be stored. Whoever runs it needs to make room."


class _TooLarge(Exception):
    pass


class ExtensionCorsMiddleware:
    """Let Kaal's browser extension read the API's answers from its own pages.

    A browser hides another site's answers from a page unless that site names
    the page's origin. Only the listed extension origins are named, and only
    for the API. Cookies are never allowed across, so a browser's signed-in
    session cannot be used this way: every such request needs its own token.
    """

    def __init__(self, app: Any, origins: tuple[str, ...]) -> None:
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin", b"").decode("latin-1")
        if origin not in self.origins:
            await self.app(scope, receive, send)
            return
        named = [(b"access-control-allow-origin", origin.encode("latin-1")), (b"vary", b"Origin"), (b"access-control-expose-headers", b"X-Kaal-Version")]
        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            # The browser asking, before the real request, whether it may send it.
            await send({"type": "http.response.start", "status": 204, "headers": named + [
                (b"access-control-allow-methods", CORS_METHODS), (b"access-control-allow-headers", CORS_HEADERS),
                (b"access-control-max-age", b"600"), (b"content-length", b"0"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def named_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message.get("headers") or []) + named}
            await send(message)

        await self.app(scope, receive, named_send)


UPLOAD_ROUTE = re.compile(r"/v1/(?:captures|medical/receipts|notes/[^/]+/attachments|library/notes/[^/]+/attachments)")
ONCE_ONLY = (b"content-type", b"content-length", b"authorization", b"transfer-encoding", b"x-kaal-session")
BODY_IDLE_SECONDS = 60.0


class _TooSlow(Exception):
    pass


class BodyLimitMiddleware:
    """Reject request bodies over a byte limit, declared or streamed.

    Only a file sent to one of the upload routes by someone signed in may be large. Everything
    else (any other route, or any body from a request that carries no valid credential) is held
    to a small limit, checked before the body is read, so that nobody can make the server read,
    parse or store a large body it has no use for. The limit is chosen by where the request is
    going, not by what it says it carries. A request that is not clear about its size or about
    who sends it (a header that matters given twice, or a length together with chunked encoding)
    is refused, since this layer and the one that reads the body could each believe a different one.
    """

    UPLOADS_AT_ONCE = 4

    def __init__(self, app: Any, limit: int, credentials: Callable[[dict[bytes, bytes]], str] | None = None,
                 full: Callable[[int], bool] | None = None) -> None:
        self.app = app
        self.limit = limit
        # Who a request's credential belongs to ("" when it carries none that works).
        self.credentials = credentials
        self.full = full
        self._uploading: dict[str, int] = {}
        self._lock = threading.Lock()

    def _limit_for(self, scope: dict, headers: dict[bytes, bytes], declared: int | None) -> tuple[int, str]:
        """The most this request may send, and whose it is when it may be more than a stranger's."""
        upload = scope.get("method") == "POST" and UPLOAD_ROUTE.fullmatch(scope.get("path", "")) is not None
        limit = self.limit if upload else min(self.limit, JSON_BODY_BYTES)
        who = ""
        # A request with neither a length nor chunked encoding has no body at all: nothing to ask about.
        sized_or_streamed = declared is not None or b"transfer-encoding" in headers
        if sized_or_streamed and (declared is None or declared > STRANGER_BODY_BYTES) and self.credentials is not None:
            who = self.credentials(headers)
            if not who:
                limit = min(limit, STRANGER_BODY_BYTES)
        return limit, who

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        listed = scope.get("headers") or []
        headers = dict(listed)
        names = [name for name, _value in listed]
        # Cookies may arrive as several headers; they are read as one.
        headers[b"cookie"] = b"; ".join(value for name, value in listed if name == b"cookie")
        raw = headers.get(b"content-length")
        if any(names.count(name) > 1 for name in ONCE_ONLY) or (raw is not None and b"transfer-encoding" in headers) or (raw is not None and not raw.isdigit()):
            await _send_json(send, 400, {"error": "The request is not clear about its size or who sends it.", "kind": "invalid"})
            return
        declared = int(raw) if raw is not None else None
        limit, who = self._limit_for(scope, headers, declared)
        if declared is not None and declared > limit:
            await _send_json(send, 413, {"error": "Request is too large.", "kind": "too_large"})
            return
        # A large body is written to disk or held in memory as it arrives, before anything else looks
        # at the request: so the disk's floor, and how many one person sends at once, are checked here.
        large = bool(who) and limit > STRANGER_BODY_BYTES
        if large and limit > JSON_BODY_BYTES and self.full is not None and self.full(declared or 0):
            await _send_json(send, 507, {"error": FULL_DISK, "kind": "full"})
            return
        if large:
            with self._lock:
                busy = self._uploading.get(who, 0) >= self.UPLOADS_AT_ONCE
                if not busy:
                    self._uploading[who] = self._uploading.get(who, 0) + 1
            if busy:
                await _send_json(send, 429, {"error": "You are sending several large requests at once. Wait for one to finish.", "kind": "rate_limited"})
                return
        received = 0
        started = False
        finished = False
        body_done = False

        async def limited_receive() -> dict:
            nonlocal received, body_done
            if body_done:
                return await receive()      # nothing more to read: this only waits to hear that the client has gone
            try:
                # A body that stops arriving is given up on, so a connection cannot be parked half-sent.
                message = await asyncio.wait_for(receive(), BODY_IDLE_SECONDS)
            except asyncio.TimeoutError:
                raise _TooSlow() from None
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
                body_done = not message.get("more_body", False)
            else:
                body_done = True
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
            finished = True
        except _TooLarge:
            if not started:
                await _send_json(send, 413, {"error": "Request is too large.", "kind": "too_large"})
        except _TooSlow:
            if not started:
                await _send_json(send, 408, {"error": "The request stopped arriving.", "kind": "timeout"})
        finally:
            if large:
                with self._lock:
                    left = self._uploading.get(who, 0) - 1
                    if left > 0:
                        self._uploading[who] = left
                    else:
                        self._uploading.pop(who, None)
                if not (finished and body_done):
                    # An upload cut off part-way leaves its temporary file open until the objects
                    # holding it are collected; do that now rather than whenever it next happens.
                    gc.collect()


async def _send_json(send: Callable, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


class Vaults:
    """The installation's vaults as objects, one per vault for the life of the process.

    Keeping one object per vault matters: it holds the vault's locks and its
    search memory.
    """

    def __init__(self, store: accounts.AccountStore) -> None:
        self.store = store
        self._open: dict[str, core.Vault] = {}
        self._lock = threading.Lock()

    def get(self, vault_id: str) -> core.Vault | None:
        with self._lock:
            found = self._open.get(vault_id)
            if found is None:
                record = self.store.vault(vault_id)
                if record is None:
                    return None
                found = self._open[vault_id] = core.Vault(self.store.folder_of(record))
            return found

    def all(self) -> list[core.Vault]:
        return [vault for vault in (self.get(record["id"]) for record in self.store.vaults()) if vault is not None]

    def forget(self, vault_id: str) -> None:
        with self._lock:
            self._open.pop(vault_id, None)


@dataclass
class AppState:
    settings: Settings
    tokens: tokens.TokenStore
    limiter: RateLimiter
    accounts: accounts.AccountStore
    vaults: Vaults
    worker: Any = None
    guard: Any = None
    signups: Any = None
    services: Any = None
    strangers: Any = None
    usage: Any = None
    downloads: Any = None
    invites: Any = None
    token_making: Any = None
    exports: Any = None


class Scoped:
    """The server's state as one request sees it: everything shared, plus the
    one vault this request was allowed into (None before it has been checked)."""

    def __init__(self, state: AppState, vault: core.Vault | None) -> None:
        self._state = state
        self.vault = vault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state, name)

    def _chosen(self) -> core.Vault:
        if self.vault is None:
            raise RuntimeError("This request reached the vault without being checked for access to one.")
        return self.vault

    @contextlib.contextmanager
    def in_vault(self) -> Iterator[core.Vault]:
        """Work on the request's vault, holding its request lock so requests take turns."""
        vault = self._chosen()
        with core.use_vault(vault), vault.request_lock:
            yield vault

    def beside_vault(self) -> Any:
        """Work on the request's vault without its request lock: for slow reading
        of something already looked up under the lock, which changes nothing."""
        return core.use_vault(self._chosen())


def open_store(settings: Settings) -> accounts.AccountStore:
    """The accounts store, after adopting a single-vault installation's vault if that has not happened yet."""
    store = accounts.AccountStore(settings.accounts_file or settings.home / "accounts.db", settings.home)
    vault = core.Vault(settings.vault)
    if vault.index_path.is_file() and store.adopt(vault.root):
        log.info("accounts: adopted the existing vault as the first account's")
    return store


def delete_vault(settings: Settings, store: accounts.AccountStore, token_store: tokens.TokenStore, vault_id: str,
                 registry: "Vaults | None" = None) -> dict[str, Any]:
    """Delete a vault for good: its folder, its record, who belonged to it and the tokens made for it.

    The installation's first vault is kept: backups and tokens from before
    accounts existed depend on it. Copies already in backups remain until they
    are pruned.
    """
    record = store.vault(vault_id)
    if record is None:
        raise accounts.AccountError("No such vault.")
    folder = store.folder_of(record).resolve()
    if folder == Path(settings.vault).expanduser().resolve():
        raise accounts.AccountError(f"“{record['name']}” is this installation's first vault, which backups and older tokens depend on, "
                                    "so it cannot be deleted. Move to the Trash what you do not want to keep.")
    opened = registry.get(vault_id) if registry is not None else core.Vault(folder)
    if opened is not None and folder.exists():
        with core.use_vault(opened), core.vault_capture_lock():
            shutil.rmtree(folder)
    if registry is not None:
        registry.forget(vault_id)
    store.remove_vault_record(vault_id)
    return {"id": record["id"], "name": record["name"], "tokens_revoked": token_store.revoke_where(vault=vault_id)}


def removal_plan(settings: Settings, store: accounts.AccountStore, account_id: str) -> dict[str, Any]:
    """What removing an account would delete, and anything that stops it."""
    plan = store.plan_account_removal(account_id)
    first = Path(settings.vault).expanduser().resolve()
    for vault in plan["vaults_to_delete"]:
        record = store.vault(vault["id"])
        if record is not None and store.folder_of(record).resolve() == first:
            plan["blocked"].append(f"“{vault['name']}” is this installation's first vault and cannot be deleted. Share it with someone and make them its owner first.")
    return plan


def remove_account(settings: Settings, store: accounts.AccountStore, token_store: tokens.TokenStore, account_id: str,
                   registry: "Vaults | None" = None) -> dict[str, Any]:
    """Remove an account for good, with the vaults only it belonged to and every token it made."""
    plan = removal_plan(settings, store, account_id)
    if plan["blocked"]:
        raise accounts.AccountError(" ".join(plan["blocked"]))
    deleted = [delete_vault(settings, store, token_store, vault["id"], registry)["name"] for vault in plan["vaults_to_delete"]]
    revoked = token_store.revoke_where(account=plan["account"]["id"])
    store.remove_account(plan["account"]["id"])
    return {"status": "removed", "account": plan["account"]["login"], "vaults_deleted": deleted,
            "vaults_left": [vault["name"] for vault in plan["vaults_to_leave"]], "tokens_revoked": revoked}


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {"login": plan["account"]["login"], "vaults_to_delete": [vault["name"] for vault in plan["vaults_to_delete"]],
            "vaults_to_leave": [vault["name"] for vault in plan["vaults_to_leave"]], "blocked": plan["blocked"]}


def create_vault(store: accounts.AccountStore, name: str, owner: str) -> dict[str, Any]:
    """Record a new vault for ``owner`` and make its folder, with a key of its own."""
    record = store.add_vault(name, owner=owner)
    try:
        core.create_vault_folder(store.folder_of(record))
    except BaseException:
        store.remove_vault_record(record["id"])
        raise
    return record


class SignInGuard:
    """Slows password guessing without letting anyone lock a person out.

    Three counts of wrong tries, kept in memory (a restart clears them):
    a sign-in name tried from one address (five free, then a minute, doubling to fifteen);
    one address across every name (twenty free, then the same); and a sign-in name from
    everywhere together (fifty free, then at most a minute, so many addresses together
    cannot guess fast, but neither can they keep the person out for long). A correct
    sign-in clears its name's counts. Tries for one name are checked one at a time, so
    parallel guesses cannot slip past a count.
    """

    RULES = {"pair": (5, 60.0, 900.0), "from": (20, 60.0, 900.0), "name": (50, 5.0, 60.0)}
    REMEMBERED = 20_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], tuple[int, float, float]] = {}   # failures, next try allowed at, last failure
        self._names = [threading.Lock() for _ in range(64)]

    def one_at_a_time(self, login: str) -> threading.Lock:
        """Hold this while checking a password for ``login``."""
        return self._names[int(hashlib.sha256(login.lower().encode()).hexdigest()[:8], 16) % len(self._names)]

    @staticmethod
    def _keys(login: str, address: str) -> dict[str, tuple[str, str]]:
        name = login.lower()
        return {"pair": ("pair", f"{name}\n{address}"), "from": ("from", address), "name": ("name", name)}

    def wait(self, login: str, address: str = "") -> int:
        """Seconds until this name may be tried again from this address; 0 means now."""
        now = time.monotonic()
        with self._lock:
            latest = max((self._seen.get(key, (0, 0.0, 0.0))[1] for key in self._keys(login, address).values()), default=0.0)
        return max(0, int(latest - now + 0.999))

    def failed(self, login: str, address: str = "") -> None:
        now = time.monotonic()
        with self._lock:
            for kind, key in self._keys(login, address).items():
                free, first, longest = self.RULES[kind]
                count = self._seen.get(key, (0, 0.0, 0.0))[0] + 1
                over = count - free
                pause = min(longest, first * 2 ** min(over - 1, 20)) if over > 0 else 0.0
                if key not in self._seen and len(self._seen) >= self.REMEMBERED:
                    self._forget_some(now)
                self._seen[key] = (count, now + pause, now)

    def _forget_some(self, now: float) -> None:
        # First what no longer holds anyone back and has been quiet for fifteen minutes; then, oldest
        # first, what holds nobody back now; a count that is holding someone back goes only as a last resort.
        self._seen = {key: entry for key, entry in self._seen.items() if entry[1] > now or now - entry[2] < 900}
        while len(self._seen) >= self.REMEMBERED:
            idle = [key for key, entry in self._seen.items() if entry[1] <= now]
            pool = idle or list(self._seen)
            self._seen.pop(min(pool, key=lambda key: self._seen[key][2]))

    def succeeded(self, login: str, address: str = "") -> None:
        keys = self._keys(login, address)
        with self._lock:
            self._seen.pop(keys["pair"], None)
            self._seen.pop(keys["name"], None)


def device_label(user_agent: str) -> str:
    """A short name for the browser a session was started from, such as "Chrome on macOS"."""
    agent = user_agent or ""
    browser = next((name for mark, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if mark in agent), "")
    system = next((name for mark, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "macOS"), ("Windows", "Windows"), ("Linux", "Linux"))
                   if mark in agent), "")
    return " on ".join(part for part in (browser, system) if part) or "A browser"


def person_record(state: AppState, session: dict[str, Any]) -> dict[str, Any] | None:
    """What a signed-in person may do right now: their account, the vault their
    session is on (their first if that one is gone) and what their role allows there."""
    account = state.accounts.account(session["account"])
    if account is None or account["disabled_at"]:
        return None
    theirs = state.accounts.vaults_of(account["id"])
    current = next((vault for vault in theirs if vault["id"] == session["vault"]), None) or (theirs[0] if theirs else None)
    return {"id": f"session:{session['id']}", "name": account["login"], "session": session["id"], "person": account, "vaults": theirs,
            "account": account["id"], "vault": current["id"] if current else None, "role": current["role"] if current else None,
            "scopes": list(accounts.ROLE_SCOPES[current["role"]]) if current else []}


def who_may(state: AppState, record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """The account a token acts for, the vault it acts in, and that account's role there.

    The account is None when it is missing or switched off; the role is None
    when the account does not belong to the vault.
    """
    default_account, default_vault = state.accounts.defaults()
    account_id, vault_id = record.get("account") or default_account, record.get("vault") or default_vault
    if not account_id or not vault_id:
        return None, None, None
    account, role = state.accounts.standing(account_id, vault_id)
    if account is None or account["disabled_at"]:
        return None, vault_id, None
    return account, vault_id, role


def _error(status: int, message: str, kind: str | None = None, details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    payload: dict[str, Any] = {"error": message, "kind": kind or KIND_FOR_STATUS.get(status, "error")}
    if details:
        payload["details"] = details
    return JSONResponse(payload, status_code=status, headers=headers)


WEB_DIR = Path(__file__).with_name("web")
# The web app is static files with no inline script or style, so the browser
# can be told to run nothing else and to refuse being framed by another site.
WEB_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
WEB_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json", ".png": "image/png"}

# Files a browser may show in place, each with the bytes it must start with.
# Anything else is download-only: a saved web page, an SVG or a script opened
# from Kaal's own address could act with the signed-in user's session.
VIEWABLE = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".gif": ("image/gif", b"GIF8"),
    ".webp": ("image/webp", b"RIFF"),
}
VIEWABLE_TEXT = {".txt", ".md", ".markdown", ".csv", ".log"}


def viewable_type(name: str, data: bytes) -> str | None:
    """The media type to show a file as in the browser, or None when it must be downloaded."""
    suffix = Path(name or "").suffix.lower()
    if suffix in VIEWABLE_TEXT:
        return "text/plain; charset=utf-8"
    media_type, magic = VIEWABLE.get(suffix, (None, b""))
    if media_type is None or not data.startswith(magic) or (suffix == ".webp" and data[8:12] != b"WEBP"):
        return None
    return media_type


SESSION_COOKIE = "kaal_session"
# Over https the browser cookie is "__Host-" prefixed: only this exact host can set it, so another
# site under the same domain (a sibling on a tailnet, say) cannot plant a session in your browser.
HOST_SESSION_COOKIE = "__Host-kaal_session"
# Apps (the iPhone app) send their session in this header instead of a cookie.
SESSION_HEADER = "x-kaal-session"
SESSION_SECONDS = 30 * 24 * 3600
# Held by the browser for one trip to a sign-in service and back, so the trip
# can only be finished by the browser that started it.
FLOW_COOKIE = "kaal_signin"
HOST_FLOW_COOKIE = "__Host-kaal_signin"
FLOW_PATH = "/v1/sign-in/with"


def session_cookie_name(settings: "Settings") -> str:
    return HOST_SESSION_COOKIE if settings.cookie_secure else SESSION_COOKIE


def flow_cookie_name(settings: "Settings") -> str:
    return HOST_FLOW_COOKIE if settings.cookie_secure else FLOW_COOKIE


def cookie_named(header: str, name: str) -> str:
    """The value of the cookie with exactly this name, read from the Cookie header as it was sent.

    Not through the framework's parser, which trims more than spaces from a name: a cookie called
    "\xa0__Host-kaal_session", which any neighbouring host may set, would be read as ours. Here a
    name matches byte for byte or not at all, and the first such cookie is the one used."""
    for part in header.split(";"):
        key, found, value = part.partition("=")
        if found and key.strip(" \t") == name:
            return value.strip(" \t").strip('"')
    return ""


def exact_cookie(request: Request, name: str) -> str:
    return cookie_named("; ".join(request.headers.getlist("cookie")), name)


HOST_COOKIE_SINCE = "host_cookie_since"


def _older_than_the_host_cookie(state: Any, presented: str) -> bool:
    """Whether a session or token was made before this server began tying its cookie to the host.

    Only those may still arrive in the old cookie from a browser: someone on a neighbouring host
    who plants a cookie can plant only a session of their own, and after the change a new one is
    never accepted under the old name."""
    since = state.accounts.get(HOST_COOKIE_SINCE)
    if not since:
        return False
    found = state.accounts.session(presented) or state.tokens.authenticate(presented)
    return bool(found) and str(found.get("created") or "9") < since


def presented_session(request: Request) -> str:
    """The session (or, from older sign-ins, the token) a request carries: the app's header, or the
    browser's cookie. Over https the un-prefixed cookie is believed only for a sign-in older than
    the change to a host-bound cookie: a browser's is then moved to the new cookie, and an app
    installed before the change keeps working until it is updated to send the header."""
    state = request.app.state.kaal
    settings = state.settings
    header = request.headers.get(SESSION_HEADER, "").strip()
    if header:
        return header
    cookie = exact_cookie(request, session_cookie_name(settings))
    if cookie or not settings.cookie_secure:
        return cookie
    legacy = exact_cookie(request, SESSION_COOKIE)
    if legacy and _older_than_the_host_cookie(state, legacy):
        if request.headers.get("sec-fetch-site"):
            request.state.move_cookie = legacy
        return legacy
    return ""


def set_session_cookie(response: Response, settings: "Settings", value: str) -> None:
    response.set_cookie(session_cookie_name(settings), value, max_age=SESSION_SECONDS, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")


def clear_session_cookie(response: Response, settings: "Settings") -> None:
    response.delete_cookie(session_cookie_name(settings), path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")
    if settings.cookie_secure:
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="strict")


_ADDRESS = re.compile(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?|http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?")


def public_address(state: "AppState") -> str:
    """The address people open this Kaal at, or "" when nobody has said."""
    return state.accounts.get("public_address") or state.settings.public_url


def clean_address(address: str) -> str:
    cleaned = (address or "").strip().rstrip("/")
    if not _ADDRESS.fullmatch(cleaned):
        raise accounts.AccountError("Kaal's own address must look like https://kaal.example.org (http is allowed only for localhost).")
    return cleaned


def callback_address(address: str, service_id: str) -> str:
    """Where a sign-in service sends the browser back to. This exact text is registered with the service."""
    return f"{address}{FLOW_PATH}/{service_id}/callback"


def add_signin_service(store: accounts.AccountStore, services: Any, *, service_id: str, name: str, issuer: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """Check that a sign-in service answers, then store it with its secret encrypted."""
    issuer = (issuer or "").strip().rstrip("/")
    if not issuer.startswith("https://"):
        raise accounts.AccountError("A sign-in service's address must start with https://")
    if not (client_secret or "").strip():
        raise accounts.AccountError("A sign-in service needs a name, a client id and a client secret.")
    try:
        services.settings(issuer)
    except oidc.SignInServiceError as error:
        raise accounts.AccountError(f"Kaal could not read that service's settings at {issuer}/.well-known/openid-configuration ({error}).") from None
    return store.put_service(service_id, name, issuer, client_id, oidc.seal_secret(core.installation_key(), client_secret.strip()))
# A browser cannot add this header to a request made from another site without
# the server's permission, which Kaal never gives.
SAME_SITE_HEADER = "x-kaal-request"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _from_cookie_checked(request: Request) -> None:
    if request.method not in SAFE_METHODS and request.headers.get(SAME_SITE_HEADER) != "1":
        raise HTTPException(403, "This request did not come from the Kaal web app.")


def refused_too_often(request: Request, state: Any) -> bool:
    """Count one refused credential from this address; True when it has had too many this minute."""
    return not state.limiter.allow(f"refused:{client_address(request)}")


def _refuse_unknown(request: Request, state: AppState) -> None:
    if refused_too_often(request, state):
        raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
    raise HTTPException(401, "A valid bearer token is required.", headers={"WWW-Authenticate": "Bearer"})


def stranger_gate(request: Request) -> None:
    """Routes that need no sign-in do real work (checking a password, starting a trip to Google);
    each address may call them only so often, so one client cannot crowd everyone else out."""
    state = request.app.state.kaal
    if not state.strangers.allow(f"addr:{client_address(request)}"):
        raise HTTPException(429, "Too many tries from here. Wait a minute and try again.", headers={"Retry-After": "60"})


STRANGERS = Depends(stranger_gate)


def _signed_in_person(request: Request, state: AppState) -> dict[str, Any] | None:
    """The person a session belongs to; None when the request carries no live session."""
    presented = presented_session(request)
    if not presented.startswith(accounts.SESSION_PREFIX + "_"):
        return None
    session = state.accounts.session(presented)
    record = person_record(state, session) if session else None
    if record is None:
        return None
    _from_cookie_checked(request)
    request.state.token_name = f"account:{record['account']}"
    if not state.limiter.allow(f"account:{record['account']}"):
        raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
    return record


def _room_checked(request: Request, state: AppState, vault: core.Vault | None) -> None:
    """Refuse something new for a vault that is full. Counted from the request's declared size, before it is read."""
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        # Without a size there is nothing to count before the body has been stored.
        raise HTTPException(411, "Say how large the request is (Content-Length).")
    incoming = int(declared)
    if disk_is_full(state.settings, incoming):
        raise HTTPException(507, FULL_DISK)
    quota = state.settings.vault_quota_bytes
    if not quota or vault is None or vault.root.resolve() == Path(state.settings.vault).expanduser().resolve():
        return
    if state.usage.used(vault.root) + incoming > quota:
        raise HTTPException(413, f"This vault is full: it may hold {quota // MiB} MiB. Delete what you no longer need from the Trash, "
                                 "or ask whoever runs this Kaal for more room.")
    state.usage.add(vault.root, incoming)


def require(*scopes: str, adds: bool = False) -> Callable:
    """The caller must hold ``scopes`` in the vault they are in. ``adds`` marks a route that stores
    something new, which a full vault refuses."""

    def dependency(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.kaal
        request.state.needed = scopes
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        from_cookie = False
        if scheme.lower() != "bearer":
            # A signed-in browser: a session, or (older sign-ins) a token, in a cookie page scripts cannot read.
            person = _signed_in_person(request, state)
            if person is not None:
                missing = [scope for scope in scopes if scope not in person["scopes"]]
                if scopes and person["vault"] is None:
                    raise HTTPException(403, "You do not belong to a vault yet.")
                if missing:
                    raise HTTPException(403, f"Your role in this vault does not allow that (it needs {', '.join(missing)}).")
                request.state.vault = state.vaults.get(person["vault"]) if person["vault"] else None
                request.state.caller, request.state.scopes = f"account:{person['account']}", person["scopes"]
                if adds:
                    _room_checked(request, state, request.state.vault)
                return person
            presented, from_cookie = presented_session(request), True
        record = state.tokens.authenticate(presented.strip()) if presented.strip() else None
        if record is not None and from_cookie:
            _from_cookie_checked(request)
        if record is None:
            _refuse_unknown(request, state)
        request.state.token_name = record["name"]
        if not state.limiter.allow(record["id"]):
            raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
        account, vault_id, role = who_may(state, record)
        if account is None:
            raise HTTPException(401, "A valid bearer token is required.", headers={"WWW-Authenticate": "Bearer"})
        vault = state.vaults.get(vault_id) if role else None
        if vault is None:
            raise HTTPException(403, "This token's account does not belong to its vault.")
        # A role caps what any token of that person may do in that vault.
        allowed = [scope for scope in record.get("scopes", []) if scope in accounts.ROLE_SCOPES[role]]
        missing = [scope for scope in scopes if scope not in allowed]
        if missing:
            raise HTTPException(403, f"This token lacks the {', '.join(missing)} scope.")
        request.state.vault = vault
        request.state.caller, request.state.scopes = record["id"], allowed
        if adds:
            _room_checked(request, state, vault)
        return {**record, "scopes": allowed, "account": account["id"], "vault": vault_id, "role": role}

    return Depends(dependency)


def require_person() -> Callable:
    """For managing one's own account: only a person signed in with their own
    password will do. A token never may, whatever it is allowed in a vault."""
    def dependency(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.kaal
        person = _signed_in_person(request, state)
        if person is None:
            if state.tokens.authenticate(presented_session(request)) or request.headers.get("authorization"):
                raise HTTPException(403, "Sign in with your own password to manage your account; a token cannot.")
            _refuse_unknown(request, state)
        return person

    return Depends(dependency)


def require_admin() -> Callable:
    """Managing who may use the installation: an administrator, signed in as themselves."""
    def dependency(person: dict[str, Any] = require_person()) -> dict[str, Any]:
        if not person["person"]["admin"]:
            raise HTTPException(403, "Only an administrator may do that.")
        return person

    return Depends(dependency)


def require_owner() -> Callable:
    """Managing who shares a vault: one of its owners, signed in as themselves, with that vault open."""
    def dependency(person: dict[str, Any] = require_person()) -> dict[str, Any]:
        if person["role"] != "owner":
            raise HTTPException(403, "Only an owner of this vault may do that.")
        return person

    return Depends(dependency)


class Hourly:
    """So many an hour for each key: for things nobody needs to do often, such as making tokens."""

    REMEMBERED = 10_000

    def __init__(self, per_hour: int) -> None:
        self.per_hour = max(1, per_hour)
        self._lock = threading.Lock()
        self._times: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._times) >= self.REMEMBERED and key not in self._times:
                self._times = {name: times for name, times in self._times.items() if times and now - times[-1] < 3600}
            recent = [moment for moment in self._times.get(key, []) if now - moment < 3600]
            if len(recent) >= self.per_hour:
                self._times[key] = recent
                return False
            recent.append(now)
            self._times[key] = recent
            return True


class SignUpGuard:
    """Caps how many accounts strangers can create in an hour when sign-up is open."""

    def __init__(self, per_hour: int) -> None:
        self.per_hour = max(0, per_hour)
        self._lock = threading.Lock()
        self._times: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._times = [moment for moment in self._times if now - moment < 3600]
            if len(self._times) >= self.per_hour:
                return False
            self._times.append(now)
            return True


def _run(state: AppState, handler: Callable[[Any], None], *, exact_note: str | None = None, **params: Any) -> JSONResponse:
    """Run a core handler under the server lock; ``exact_note`` must be an existing note id."""
    with state.in_vault():
        try:
            if exact_note is not None:
                core.note_by_id(exact_note)
            result, options = core.call_captured(handler, SimpleNamespace(**params))
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
    meta = options.pop("meta", None)
    return JSONResponse({"result": result, "format": options, **({"meta": meta} if meta else {})})


META_ATTACHMENT_KEYS = ("id", "name", "size", "storage", "sensitivity", "extracted_markdown", "extracted_at")


SOURCE_LINE = re.compile(r"(?m)^(Source URL: )(\S+).*$")


def _host_of(address: str) -> str:
    try:
        return core.urlsplit(address).hostname or ""
    except ValueError:
        return ""


def metadata_only(note: dict[str, Any]) -> dict[str, Any]:
    """What the read scope may see of a note: its title, tags, times and files' names. Not a
    receipt's values (names, amounts, dates) and not the full address it was saved from."""
    shown = {key: value for key, value in note.items() if key not in {"receipt", "source"}}
    source = note.get("source")
    if isinstance(source, dict):
        host = _host_of(str(source.get("url") or ""))
        shown["source"] = {key: value for key, value in {"type": source.get("type"), "host": host}.items() if value}
    if "attachments" in note:
        shown["attachments"] = [{key: entry[key] for key in META_ATTACHMENT_KEYS if key in entry} for entry in note.get("attachments", []) if isinstance(entry, dict)]
    return shown


def _metadata_answer(response: JSONResponse, caller: dict[str, Any]) -> JSONResponse:
    """Trim a note list (or {"note": ...}) to metadata for a caller without the reveal scope."""
    if "reveal" in caller.get("scopes", []) or response.status_code != 200:
        return response
    payload = json.loads(response.body)
    found = payload.get("result")
    if isinstance(found, list):
        payload["result"] = [metadata_only(entry) if isinstance(entry, dict) else entry for entry in found]
    elif isinstance(found, dict) and isinstance(found.get("note"), dict):
        payload["result"] = {**found, "note": metadata_only(found["note"])}
    return JSONResponse(payload, status_code=response.status_code)


# What an answer to a change may tell a caller that cannot read the vault's contents.
WRITTEN_KEYS = frozenset({"id", "ids", "note_id", "attachment_id", "attachmentId", "status", "state", "kind", "type", "created", "created_at", "updated", "updated_at",
                          "duplicate", "queued", "count", "changed", "trashed", "restored", "deleted", "purged", "reviewed", "size", "storage"})


def ids_only(value: Any) -> Any:
    """Ids, states and counts, and nothing a person wrote: no titles, names, amounts, dates of service or text."""
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, inner in value.items():
            if key == "ids" and isinstance(inner, list):
                kept[key] = [entry for entry in inner if isinstance(entry, str)]
            elif isinstance(inner, (dict, list)):
                trimmed = ids_only(inner)
                if trimmed:
                    kept[key] = trimmed
            elif key in WRITTEN_KEYS or (key in ("note", "receipt") and isinstance(inner, str)):
                # "note" and "receipt" name what was changed when they hold an id rather than the thing itself.
                kept[key] = inner
        return kept
    if isinstance(value, list):
        return [trimmed for trimmed in (ids_only(entry) for entry in value if isinstance(entry, (dict, list))) if trimmed]
    return None


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


def _content_response(request: Request, state: AppState, note_id: str, attachment_id: str, *, receipt: bool = False) -> Response:
    with state.in_vault():
        try:
            meta = core.medical_receipt_meta(core.load_index(), note_id) if receipt else core.note_by_id(note_id)
            matches = [att for att in meta.get("attachments", []) if att.get("id") == attachment_id]
            if not matches:
                core.die("Attachment not found.", kind="not_found")
            data = core.attachment_original_bytes(meta, matches[0])
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
    busy = held_download(request, len(data))
    if busy is not None:
        return busy
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


class LibrarySearch(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    trash: bool = False
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


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
    fields: dict[str, str] = Field(max_length=50)


class ReportFilter(BaseModel):
    year: str = Field(default="", max_length=10)
    patient_name: str = Field(default="", max_length=200)
    from_date: str = Field(default="", max_length=32)
    to_date: str = Field(default="", max_length=32)
    reimbursed: str = Field(default="all", max_length=3)


class SignIn(BaseModel):
    token: str = Field(min_length=1, max_length=400)


class PasswordSignIn(BaseModel):
    login: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class Recover(BaseModel):
    login: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupCode(BaseModel):
    code: str = Field(min_length=1, max_length=200)


class SetupFinish(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    name: str = Field(default="", max_length=100)


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=1024)
    new: str = Field(min_length=1, max_length=1024)


class PasswordProof(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class PersonNew(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=100)
    vault_name: str = Field(default="", max_length=80)
    admin: bool = False
    own_vault: bool = True


class Flag(BaseModel):
    on: bool


class SignUpSetting(BaseModel):
    signup: Literal["open", "invitation"]


class SignUp(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=100)
    password: str = Field(min_length=1, max_length=1024)
    vault_name: str = Field(default="", max_length=80)


class MemberChange(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    role: Literal["owner", "editor", "viewer"]


class VaultNew(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class TokenNew(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(min_length=1, max_length=5)
    vault: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=1024)
    confirm: str = Field(default="", max_length=200)


class VaultChoice(BaseModel):
    vault: str = Field(min_length=1, max_length=64)


class ServiceStart(BaseModel):
    setup_code: str = Field(default="", max_length=200)


class ConnectStart(BaseModel):
    password: str = Field(default="", max_length=1024)


class AppSignInStart(BaseModel):
    # The SHA-256 of a secret only the app holds, base64url without padding.
    challenge: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    kind: Literal["app", "extension"] = "app"
    # For the browser extension: its id, which must be one this Kaal lets call it.
    extension: str = Field(default="", pattern=r"^[a-p]{0,32}$")
    # Needed when the browser was signed in more than a few minutes ago: the password, or for an
    # account without one, the sign-in name typed.
    password: str = Field(default="", max_length=1024)
    confirm: str = Field(default="", max_length=200)


class ExtensionSignInFinish(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    name: str = Field(default="Chrome extension", max_length=60)


class Proof(BaseModel):
    """What a person gives to confirm something that cannot be undone: their password, or,
    for an account that signs in only through another service, the name of what is deleted."""
    password: str = Field(default="", max_length=1024)
    confirm: str = Field(default="", max_length=200)


class AdminRemoval(BaseModel):
    confirm: str = Field(min_length=1, max_length=200, description="The account's sign-in name, typed again")


class AppSignInFinish(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    device: str = Field(default="", max_length=80)


class ServiceNew(BaseModel):
    kind: Literal["google", "other"] = "google"
    name: str = Field(default="", max_length=40)
    issuer: str = Field(default="", max_length=300)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str = Field(min_length=1, max_length=1000)
    address: str = Field(min_length=1, max_length=300)


class Reextract(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=100)
    unreviewed: bool = False


class ProviderForget(BaseModel):
    forget: list[str] = Field(min_length=1, max_length=50)


class Reimburse(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    date: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=500)
    undo: bool = False


class PeopleChange(BaseModel):
    add: list[str] = Field(default_factory=list, max_length=50)
    remove: list[str] = Field(default_factory=list, max_length=50)


class BulkPurge(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    confirm: bool = False


def create_app(settings: Settings) -> FastAPI:
    vault = core.Vault(settings.vault)
    if not vault.index_path.is_file():
        raise ServerConfigError(f"Vault not found at {vault.root}: refusing to start. Restore it, or run `kaal init` there first.")
    # An installation from before accounts: its one vault becomes the first account's.
    store = open_store(settings)
    registry = Vaults(store)
    missing = [record["name"] for record in store.vaults() if not (store.folder_of(record) / "index.json").is_file()]
    if missing:
        raise ServerConfigError(f"Vault folder not found for: {', '.join(missing)}. Refusing to start; restore it first.")
    settings.tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(settings.tmp_dir, 0o700)

    app = FastAPI(title="Kaal", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  description="An encrypted notes vault. Every request needs `Authorization: Bearer <token>` except /v1/health.")
    app.state.kaal = AppState(settings, tokens.TokenStore(settings.tokens_file), RateLimiter(settings.rate_per_minute), store, registry, guard=SignInGuard(),
                               signups=SignUpGuard(settings.signups_per_hour))
    app.state.kaal.worker = ExtractionWorker(registry.all)
    app.state.kaal.services = oidc.Services()
    app.state.kaal.strangers = RateLimiter(settings.strangers_per_minute)
    app.state.kaal.usage = Usage()
    if store.get(HOST_COOKIE_SINCE) is None:
        # From now on a browser's sign-in is accepted only in the cookie tied to this host.
        store.put(HOST_COOKIE_SINCE, accounts._now())
    app.state.kaal.downloads = Downloads(settings.download_memory_bytes)
    # A whole vault written out for download sits on disk until its client has it: one at a time each.
    app.state.kaal.exports = Downloads(1 << 62, per_caller=1)
    # Adding someone to a vault says whether that sign-in name exists; nobody needs to try many a minute.
    app.state.kaal.invites = RateLimiter(10)
    # Tokens, and apps signed in, per person: each leaves a record behind, and nobody makes many.
    app.state.kaal.token_making = Hourly(TOKENS_PER_HOUR)
    passwords.set_concurrency(settings.password_checks)

    def holds_credential(headers: dict[bytes, bytes]) -> str:
        """Whose credential a request carries, "" when none that works; checked only before reading a large body."""
        state = app.state.kaal
        scheme, _, presented = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        if scheme.lower() == "bearer":
            record = state.tokens.authenticate(presented.strip())
            return f"token:{record.get('account') or record['id']}" if record else ""
        offered = headers.get(SESSION_HEADER.encode(), b"").decode("latin-1").strip()
        if not offered:
            sent = headers.get(b"cookie", b"").decode("latin-1")
            offered = cookie_named(sent, session_cookie_name(settings)) or cookie_named(sent, SESSION_COOKIE)
        if not offered:
            return ""
        session = state.accounts.session(offered)
        if session is not None:
            return f"token:{session['account']}"
        record = state.tokens.authenticate(offered)
        return f"token:{record.get('account') or record['id']}" if record else ""

    app.add_middleware(BodyLimitMiddleware, limit=settings.max_upload_bytes + MiB, credentials=holds_credential,
                       full=lambda needed: disk_is_full(settings, needed))

    @app.middleware("http")
    async def access_log(request: Request, call_next: Callable) -> Response:
        started = time.monotonic()
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        # Lets an open web app notice that the server has been updated under it.
        response.headers["X-Kaal-Version"] = __version__
        route = request.scope.get("route")
        template = getattr(route, "path", "(unmatched)")
        token_name = getattr(request.state, "token_name", "-")
        moved = getattr(request.state, "move_cookie", None)
        if moved and response.status_code < 400 and "set-cookie" not in response.headers:
            # A browser signed in before the cookie was tied to the host: hand it the same sign-in under the new name.
            set_session_cookie(response, settings, moved)
            response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="strict")
        needed, held = getattr(request.state, "needed", ()), getattr(request.state, "scopes", None)
        if (held is not None and "reveal" not in held and ("write" in needed or "delete" in needed) and response.status_code == 200
                and response.headers.get("content-type", "").startswith("application/json")):
            # A caller that may change the vault but not read it is told what it changed by id, not shown it.
            body = b"".join([chunk async for chunk in response.body_iterator])
            try:
                payload = json.loads(body)
                told = {"result": ids_only(payload.get("result")), **({"format": payload["format"]} if "format" in payload else {})}
            except (ValueError, AttributeError):
                told = {"result": None}
            trimmed = JSONResponse(told)
            for name in ("Cache-Control", "X-Kaal-Version"):
                trimmed.headers[name] = response.headers[name]
            response = trimmed
        if settings.cookie_secure:
            # Browsers that have been here once never try plain http again (ignored over http).
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        # A container's health check asks every 30 seconds; only a failing answer is worth a line.
        if not (template == "/v1/health" and response.status_code == 200):
            log.info("%s %s %s %s %dms", log_safe(request.method, 12), template, response.status_code, log_safe(token_name), (time.monotonic() - started) * 1000)
        return response

    @app.exception_handler(passwords.PasswordBusy)
    async def password_busy(_request: Request, _exc: passwords.PasswordBusy) -> JSONResponse:
        return _error(503, "Kaal is busy checking other sign-ins. Try again in a moment.", "busy", headers={"Retry-After": "5"})

    # Added last so that it is the outermost layer: the browser's permission
    # check is answered without touching the vault, the log or the rate limit.
    if settings.extension_origins:
        app.add_middleware(ExtensionCorsMiddleware, origins=settings.extension_origins)
    app.add_middleware(HeldDownloadsMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Name the fields only: FastAPI's default response echoes the submitted values.
        fields = sorted({".".join(str(part) for part in err.get("loc", ()))[:200] for err in exc.errors()})
        return _error(400, "The request is not valid.", "invalid", {"fields": fields[:20], **({"more": len(fields) - 20} if len(fields) > 20 else {})})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled %s", type(exc).__name__)
        return _error(500, "Internal error.", "internal")

    S = lambda request: Scoped(request.app.state.kaal, getattr(request.state, "vault", None))  # noqa: E731

    # --- the web app: static files only; everything it shows comes from the API above

    def web_file(relative: str) -> Response:
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.[a-z]+", relative):
            raise HTTPException(404, "Not found.")
        path = WEB_DIR / relative
        media_type = WEB_TYPES.get(path.suffix)
        if media_type is None or not path.is_file():
            raise HTTPException(404, "Not found.")
        return Response(content=path.read_bytes(), media_type=media_type, headers=WEB_HEADERS)

    @app.get("/", include_in_schema=False)
    def web_index():
        return web_file("index.html")

    @app.get("/web/{relative:path}", include_in_schema=False)
    def web_asset(relative: str):
        return web_file(relative)

    # Browsers and phones ask for these addresses whatever the page declares.
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return web_file("icon-32.png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    def touch_icon():
        return web_file("icon-180.png")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return Response(content="User-agent: *\nDisallow: /\n", media_type="text/plain; charset=utf-8", headers=WEB_HEADERS)

    @app.get("/v1/health", tags=["service"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- browser sign-in: a device token, pasted once, kept in a cookie scripts cannot read

    @app.post("/v1/session", tags=["session"], dependencies=[STRANGERS])
    def sign_in(request: Request, body: SignIn):
        state = S(request)
        record = state.tokens.authenticate(body.token.strip())
        if record is None:
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            raise HTTPException(401, "That token is not valid.")
        account, _vault_id, role = who_may(state._state, record)
        if account is None or role is None:
            raise HTTPException(401, "That token is not valid.")
        request.state.token_name = record["name"]
        allowed = [scope for scope in record.get("scopes", []) if scope in accounts.ROLE_SCOPES[role]]
        response = JSONResponse({"result": {"name": record["name"], "scopes": allowed}, "format": {}})
        set_session_cookie(response, state.settings, body.token.strip())
        return response

    def answer(result: Any) -> JSONResponse:
        return JSONResponse({"result": result, "format": {}})

    def with_cookie(response: JSONResponse, state: Any, value: str) -> JSONResponse:
        set_session_cookie(response, state.settings, value)
        return response

    def who(record: dict[str, Any]) -> dict[str, Any]:
        """What a browser is told about its own sign-in."""
        told: dict[str, Any] = {"name": record["name"], "scopes": record.get("scopes", [])}
        if record.get("person"):
            person = record["person"]
            told["account"] = {"login": person["login"], "name": person["name"], "admin": person["admin"],
                               "has_password": app.state.kaal.accounts.password_hash(person["id"]) is not None}
            told["vaults"] = [{"id": vault["id"], "name": vault["name"], "role": vault["role"]} for vault in record["vaults"]]
            told["vault"] = next((vault for vault in told["vaults"] if vault["id"] == record["vault"]), None)
        return told

    def open_session(request: Request, state: Any, account: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """A new session for a person on this browser, on their first vault, and its cookie value."""
        theirs = state.accounts.vaults_of(account["id"])
        session_record, cookie = state.accounts.start_session(account["id"], theirs[0]["id"] if theirs else None, device_label(request.headers.get("user-agent", "")))
        request.state.token_name = f"account:{account['id']}"
        return session_record, cookie

    def start_session(request: Request, state: Any, account: dict[str, Any], **also: Any) -> JSONResponse:
        """Sign a person in on this browser. ``also`` is added to the answer."""
        session_record, cookie = open_session(request, state, account)
        return with_cookie(answer({**who(person_record(state, session_record)), **also}), state, cookie)

    def checked_password(request: Request, state: Any, account: dict[str, Any] | None, password: str, login: str) -> bool:
        """One password attempt for a sign-in name, counted by the guard either way. Attempts for
        one name are checked one at a time, so parallel guesses all see the count."""
        address = client_address(request)
        with state.guard.one_at_a_time(login):
            waiting = state.guard.wait(login, address)
            if waiting:
                raise HTTPException(429, "Too many wrong tries. Wait a little and try again.", headers={"Retry-After": str(waiting)})
            usable = account is not None and not account["disabled_at"]
            correct = passwords.verify_password(state.accounts.password_hash(account["id"]) if usable else None, password)
            if correct:
                state.guard.succeeded(login, address)
            else:
                state.guard.failed(login, address)
        return correct

    @app.get("/v1/session", tags=["session"])
    def session(record: dict[str, Any] = require()):
        return answer(who(record))

    @app.delete("/v1/session", tags=["session"])
    def sign_out(request: Request):
        state = S(request)
        live = state.accounts.session(presented_session(request))
        if live:
            state.accounts.end_session(live["id"])
        response = JSONResponse({"result": {"status": "signed_out"}, "format": {}})
        clear_session_cookie(response, state.settings)
        return response

    @app.post("/v1/session/vault", tags=["session"])
    def choose_vault(request: Request, body: VaultChoice, person: dict[str, Any] = require_person()):
        """Move this browser to another of the person's vaults."""
        state = S(request)
        if not any(vault["id"] == body.vault for vault in person["vaults"]):
            return _error(404, "You do not belong to that vault.", "not_found")
        state.accounts.set_session_vault(person["session"], body.vault)
        return answer(who(person_record(state, {"id": person["session"], "account": person["account"], "vault": body.vault})))

    # --- signing in as a person

    @app.post("/v1/sign-in", tags=["session"], dependencies=[STRANGERS])
    def sign_in_with_password(request: Request, body: PasswordSignIn):
        state = S(request)
        try:
            login = accounts.clean_login(body.login)
        except accounts.AccountError:
            login = body.login.strip().lower()[:64]
        # By sign-in name only: an account's id is not a second name to guess under.
        account = state.accounts.account_named(login)
        if not checked_password(request, state, account, body.password, login):
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            raise HTTPException(401, "That sign-in name and password do not match.")
        stored = state.accounts.password_hash(account["id"])
        if stored and passwords.needs_rehash(stored):
            state.accounts.set_password(account["id"], passwords.hash_password(body.password))
        return start_session(request, state, account)

    @app.post("/v1/sign-in/recover", tags=["session"], dependencies=[STRANGERS])
    def recover(request: Request, body: Recover):
        """Use a recovery code to choose a new password. Signs every other browser out and
        ends the account's tokens, since whoever forgot the password may not be the only one using it."""
        state = S(request)
        login = body.login.strip().lower()[:64]
        address = client_address(request)
        try:
            passwords.check_new(body.password, login=login)
        except passwords.PasswordError as error:
            return _error(400, str(error), "invalid")
        with state.guard.one_at_a_time(login):
            waiting = state.guard.wait(login, address)
            if waiting:
                raise HTTPException(429, "Too many wrong tries. Wait a little and try again.", headers={"Retry-After": str(waiting)})
            account = state.accounts.account_named(login)
            usable = account is not None and not account["disabled_at"]
            # The code is checked the same way whether or not the name exists, so timing says nothing.
            matched = state.accounts.use_recovery_code(account["id"] if usable else "no-such-account", body.code)
            if not usable or not matched:
                state.guard.failed(login, address)
                raise HTTPException(401, "That sign-in name and recovery code do not match.")
            state.guard.succeeded(login, address)
        state.accounts.set_password(account["id"], passwords.hash_password(body.password))
        state.accounts.end_sessions(account["id"])
        state.tokens.revoke_where(account=account["id"])
        return start_session(request, state, account)

    @app.post("/v1/setup/check", tags=["session"], dependencies=[STRANGERS])
    def setup_check(request: Request, body: SetupCode):
        """Whose setup link this is, so the page can greet them. Uses nothing up."""
        state = S(request)
        account = state.accounts.setup_account(body.code)
        if account is None:
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            return _error(404, "This setup link has been used or has run out. Ask for a new one.", "not_found")
        return answer({"login": account["login"], "name": account["name"]})

    @app.post("/v1/setup", tags=["session"], dependencies=[STRANGERS])
    def setup_finish(request: Request, body: SetupFinish):
        """Choose a password with a setup link, then sign in. The link works once."""
        state = S(request)
        account = state.accounts.setup_account(body.code)
        if account is None:
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            return _error(404, "This setup link has been used or has run out. Ask for a new one.", "not_found")
        try:
            passwords.check_new(body.password, login=account["login"])
        except passwords.PasswordError as error:
            return _error(400, str(error), "invalid")
        if state.accounts.setup_account(body.code, spend=True) is None:
            return _error(404, "This setup link has been used or has run out. Ask for a new one.", "not_found")
        state.accounts.set_password(account["id"], passwords.hash_password(body.password))
        state.accounts.rename(account["id"], body.name)
        state.accounts.end_sessions(account["id"])
        codes = state.accounts.new_recovery_codes(account["id"])
        return start_session(request, state, state.accounts.account(account["id"]), recovery_codes=codes)

    # --- an app on a person's own device (a phone) signs in as them. The app
    # opens Kaal's sign-in page in a browser, the person signs in there however
    # they usually do and approves, and the browser hands the app a code. The
    # code is worth nothing without a secret that never left the app.

    @app.post("/v1/account/app-sign-ins", tags=["account"])
    def app_sign_in_start(request: Request, body: AppSignInStart, person: dict[str, Any] = require_person()):
        """Approve signing an app (or this browser's Kaal extension) in as oneself. Answers a code that
        works once, for five minutes, and where to send it: the app's own address, or the extension's.
        Kaal decides where it goes, so a page cannot be talked into handing a code to anyone else."""
        state = S(request)
        if body.kind == "extension" and (not body.extension or f"chrome-extension://{body.extension}" not in state.settings.extension_origins):
            return _error(400, "This Kaal does not know that extension.", "invalid")
        # What is approved here outlasts the browser's own sign-in, so a browser left signed in is
        # not enough: the person signed in just now (as they do in the window an app opens), or
        # says who they are again.
        session = state.accounts.session(presented_session(request)) or {}
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(session.get("created", ""))).total_seconds()
        except ValueError:
            age = float("inf")
        if not 0 <= age <= APPROVE_WITHIN_SECONDS:
            if state.accounts.password_hash(person["account"]) is not None:
                if not body.password:
                    return _error(403, "Enter your password to allow this.", "proof_needed", {"proof": "password"})
                if not checked_password(request, state, person["person"], body.password, person["name"]):
                    return _error(403, "Your password is not right.", "proof_needed", {"proof": "password"})
            elif body.confirm.strip() != person["person"]["login"]:
                return _error(403, "Type your sign-in name to allow this.", "proof_needed", {"proof": "name"})
        if not state.token_making.allow(person["account"]):
            return _error(429, "You have connected a lot of apps in the last hour. Try again later.", "rate_limited", headers={"Retry-After": "600"})
        if body.kind == "extension":
            code = state.accounts.start_app_signin(person["account"], person["vault"], body.challenge, kind="extension")
            return answer({"code": code, "redirect": f"https://{body.extension}.chromiumapp.org/kaal?code={quote(code)}"})
        code = state.accounts.start_app_signin(person["account"], person["vault"], body.challenge)
        return answer({"code": code, "redirect": f"kaal://signed-in?code={quote(code)}"})

    @app.post("/v1/extension-sign-ins", tags=["session"], dependencies=[STRANGERS])
    def extension_sign_in_finish(request: Request, body: ExtensionSignInFinish):
        """The browser extension collects the sign-in a browser approved for it, as a token of its own
        for the vault that was open: read, reveal and write, as far as the person's role there allows."""
        state = S(request)
        found = state.accounts.finish_app_signin(body.code, body.verifier, kind="extension")
        account = state.accounts.account(found["account"]) if found else None
        if account is None or account["disabled_at"]:
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            return _error(401, "That sign-in has been used or has run out. Start again from the extension.")
        theirs = state.accounts.vaults_of(account["id"])
        vault = next((entry for entry in theirs if entry["id"] == found["vault"]), theirs[0] if theirs else None)
        if vault is None:
            return _error(403, "You do not belong to a vault yet.", "forbidden")
        if sum(1 for token in state.tokens.list() if token.get("account") == account["id"] and not token.get("revoked_at")) >= MAX_TOKENS_PER_PERSON:
            return _error(409, f"You have {MAX_TOKENS_PER_PERSON} tokens already. Revoke one you no longer use first.", "conflict")
        scopes = [scope for scope in ("read", "reveal", "write") if scope in accounts.ROLE_SCOPES[vault["role"]]]
        base = body.name.strip() or "Chrome extension"
        for number in range(1, 100):
            try:
                record, secret = state.tokens.create(base if number == 1 else f"{base} {number}", scopes, account=account["id"], vault=vault["id"])
                break
            except tokens.TokenError:
                continue
        else:
            return _error(409, "Could not choose a name for the token. Revoke an older one first.", "conflict")
        request.state.token_name = record["name"]
        return answer({"token": secret, "name": record["name"], "scopes": scopes, "account": account["login"], "vault": vault["name"]})

    @app.post("/v1/app-sign-ins", tags=["session"], dependencies=[STRANGERS])
    def app_sign_in_finish(request: Request, body: AppSignInFinish):
        """An app collects the sign-in a browser approved for it. The session it is
        given is sent back as the ``kaal_session`` cookie, with ``X-Kaal-Request: 1``."""
        state = S(request)
        found = state.accounts.finish_app_signin(body.code, body.verifier)
        account = state.accounts.account(found["account"]) if found else None
        if account is None or account["disabled_at"]:
            if refused_too_often(request, state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            return _error(401, "That sign-in has been used or has run out. Start again from the app.")
        theirs = state.accounts.vaults_of(account["id"])
        vault = next((entry["id"] for entry in theirs if entry["id"] == found["vault"]), theirs[0]["id"] if theirs else None)
        session_record, secret = state.accounts.start_session(account["id"], vault, body.device.strip() or "An app", days=accounts.APP_SESSION_DAYS)
        request.state.token_name = f"account:{account['id']}"
        return answer({**who(person_record(state, session_record)), "session": secret})

    # --- one's own account

    @app.get("/v1/account/removal", tags=["account"])
    def account_removal_plan(request: Request, person: dict[str, Any] = require_person()):
        """What deleting one's own account would delete, and anything that stops it."""
        state = S(request)
        return answer(_public_plan(removal_plan(state.settings, state.accounts, person["account"])))

    @app.delete("/v1/account", tags=["account"])
    def account_delete(request: Request, body: Proof, person: dict[str, Any] = require_person()):
        """Delete one's own account for good, with the vaults only one belongs to.
        Needs the password, or, for an account without one, the sign-in name typed."""
        state = S(request)
        if not proved(request, state, person, body, person["person"]["login"]):
            return _error(403, "That is not right, so nothing was deleted.", "forbidden")
        try:
            done = remove_account(state.settings, state.accounts, state.tokens, person["account"], state.vaults)
        except accounts.AccountError as error:
            return _error(409, str(error), "conflict")
        log.info("account deleted by its owner")
        response = answer({"status": "removed", "vaults_deleted": done["vaults_deleted"]})
        clear_session_cookie(response, state.settings)
        return response

    @app.get("/v1/account", tags=["account"])
    def account_show(request: Request, person: dict[str, Any] = require_person()):
        state = S(request)
        mine = {entry["service"]: entry for entry in state.accounts.identities_of(person["account"])}
        return answer({**who(person),
                       "has_password": state.accounts.password_hash(person["account"]) is not None,
                       "connections": [{"service": service["id"], "name": service["name"], "connected": service["id"] in mine,
                                        "email": mine[service["id"]]["email"] if service["id"] in mine else ""} for service in offered(state)],
                       "recovery_codes_left": state.accounts.recovery_codes_left(person["account"]),
                       "sessions": [{"id": entry["id"], "label": entry["label"], "created": entry["created"], "last_seen": entry["last_seen"],
                                     "current": entry["id"] == person["session"]} for entry in state.accounts.sessions_of(person["account"])]})

    @app.post("/v1/account/password", tags=["account"])
    def account_password(request: Request, body: PasswordChange, person: dict[str, Any] = require_person()):
        """Change one's password. Every other browser is signed out."""
        state = S(request)
        if not checked_password(request, state, person["person"], body.current, person["name"]):
            return _error(403, "Your current password is not right.", "forbidden")
        try:
            passwords.check_new(body.new, login=person["name"])
        except passwords.PasswordError as error:
            return _error(400, str(error), "invalid")
        state.accounts.set_password(person["account"], passwords.hash_password(body.new))
        return answer({"status": "changed", "signed_out_elsewhere": state.accounts.end_sessions(person["account"], keep=person["session"])})

    @app.post("/v1/account/recovery-codes", tags=["account"])
    def account_recovery_codes(request: Request, body: PasswordProof, person: dict[str, Any] = require_person()):
        """Replace one's recovery codes. The new ones are shown this once."""
        state = S(request)
        if not checked_password(request, state, person["person"], body.password, person["name"]):
            return _error(403, "Your password is not right.", "forbidden")
        return answer({"recovery_codes": state.accounts.new_recovery_codes(person["account"])})

    @app.delete("/v1/account/sessions/{session_id}", tags=["account"])
    def account_end_session(request: Request, session_id: str, person: dict[str, Any] = require_person()):
        """Sign one of one's browsers out."""
        if not S(request).accounts.end_session(session_id, person["account"]):
            return _error(404, "That browser is not signed in.", "not_found")
        return answer({"status": "signed_out", "id": session_id})

    @app.post("/v1/account/sessions/sign-out-others", tags=["account"])
    def account_end_other_sessions(request: Request, person: dict[str, Any] = require_person()):
        return answer({"status": "signed_out", "count": S(request).accounts.end_sessions(person["account"], keep=person["session"])})

    # --- tokens for one's scripts and the browser extension

    def tokens_of(state: Any, person: dict[str, Any]) -> list[dict[str, Any]]:
        """A person's active tokens. Tokens from before accounts name nobody and belong to the first account."""
        mine = {person["account"], None} if state.accounts.defaults()[0] == person["account"] else {person["account"]}
        default_vault = state.accounts.defaults()[1]
        names = {vault["id"]: vault["name"] for vault in person["vaults"]}
        return [{"id": record["id"], "name": record["name"], "scopes": record["scopes"], "created": record["created"],
                 "vault": record.get("vault") or default_vault, "vault_name": names.get(record.get("vault") or default_vault, "")}
                for record in state.tokens.list() if not record.get("revoked_at") and record.get("account") in mine]

    @app.get("/v1/account/tokens", tags=["account"])
    def account_tokens(request: Request, person: dict[str, Any] = require_person()):
        return answer({"tokens": tokens_of(S(request), person)})

    @app.post("/v1/account/tokens", tags=["account"])
    def account_token_create(request: Request, body: TokenNew, person: dict[str, Any] = require_person()):
        """Make a token for a script or the extension. Its secret is in this answer and nowhere else, ever.
        Needs the password (or, for an account without one, the sign-in name typed), so a borrowed
        browser cannot leave a lasting way in behind."""
        state = S(request)
        if state.accounts.password_hash(person["account"]) is not None:
            if not checked_password(request, state, person["person"], body.password, person["name"]):
                return _error(403, "Your password is not right, so no token was made.", "forbidden")
        elif body.confirm.strip() != person["person"]["login"]:
            return _error(403, "Type your sign-in name to make a token.", "forbidden")
        vault = next((entry for entry in person["vaults"] if entry["id"] == body.vault), None)
        if vault is None:
            return _error(404, "You do not belong to that vault.", "not_found")
        if not state.token_making.allow(person["account"]):
            return _error(429, "You have made a lot of tokens in the last hour. Try again later.", "rate_limited", headers={"Retry-After": "600"})
        if sum(1 for token in state.tokens.list() if token.get("account") == person["account"] and not token.get("revoked_at")) >= MAX_TOKENS_PER_PERSON:
            return _error(409, f"You have {MAX_TOKENS_PER_PERSON} tokens already. Revoke one you no longer use first.", "conflict")
        beyond = [scope for scope in body.scopes if scope not in accounts.ROLE_SCOPES[vault["role"]]]
        if beyond:
            return _error(400, f"Your role in that vault does not allow: {', '.join(beyond)}.", "invalid")
        try:
            record, secret = state.tokens.create(body.name, body.scopes, account=person["account"], vault=body.vault)
        except tokens.TokenError as error:
            return _error(400, str(error), "invalid")
        return answer({"token": {"id": record["id"], "name": record["name"], "scopes": record["scopes"], "created": record["created"],
                                 "vault": body.vault, "vault_name": vault["name"]}, "secret": secret})

    @app.delete("/v1/account/tokens/{token_id}", tags=["account"])
    def account_token_revoke(request: Request, token_id: str, person: dict[str, Any] = require_person()):
        state = S(request)
        mine = {person["account"], None} if state.accounts.defaults()[0] == person["account"] else {person["account"]}
        try:
            record = state.tokens.revoke_id(token_id, accounts=mine)
        except tokens.TokenError:
            return _error(404, "You have no such token.", "not_found")
        return answer({"status": "revoked", "id": record["id"]})

    # --- one's vaults and who shares them

    @app.post("/v1/account/vaults", tags=["account"])
    def account_vault_create(request: Request, body: VaultNew, person: dict[str, Any] = require_person()):
        """Create another vault of one's own, for example one to share with a household."""
        state = S(request)
        if sum(1 for vault in person["vaults"] if vault["role"] == "owner") >= state.settings.vaults_per_person:
            return _error(409, f"You already own {state.settings.vaults_per_person} vaults, which is the limit here.", "conflict")
        try:
            vault = create_vault(state.accounts, body.name.strip(), person["account"])
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        return answer({"vault": {"id": vault["id"], "name": vault["name"], "role": "owner"}})

    @app.post("/v1/vault/name", tags=["account"])
    def vault_rename(request: Request, body: VaultNew, person: dict[str, Any] = require_owner()):
        """Give the open vault another name. Only the name changes."""
        state = S(request)
        try:
            vault = state.accounts.rename_vault(person["vault"], body.name)
        except accounts.AccountError as error:
            return _error(400, str(error), "invalid")
        return answer({"vault": {"id": vault["id"], "name": vault["name"], "role": "owner"}})

    def proved(request: Request, state: Any, person: dict[str, Any], body: Proof, name: str) -> bool:
        """Whether a person confirmed with their password, or, having none, by typing ``name``."""
        if state.accounts.password_hash(person["account"]) is not None:
            return checked_password(request, state, person["person"], body.password, person["name"])
        return body.confirm.strip() == name

    def export_refused(request: Request) -> JSONResponse | None:
        """A copy of a whole vault is written to disk and stays until its client has it: one at a
        time for each caller, and only while the disk has room for it."""
        state = S(request)
        size = state.usage.used(state.vault.root) if state.vault is not None else 0
        if disk_is_full(state.settings, size):
            return _error(507, "This Kaal's disk has no room to write the copy. Whoever runs it needs to make room.", "full")
        return held_download(request, size, state.exports)

    @app.get("/v1/vault/export", tags=["account"], dependencies=[require("read", "reveal", "admin")], response_class=Response)
    def vault_export_readable(request: Request):
        """Everything in the open vault, decrypted, as a zip: notes as Markdown, the files, receipts.csv. Owners only."""
        state = S(request)
        refused = export_refused(request)
        if refused is not None:
            return refused
        workdir = Path(tempfile.mkdtemp(prefix="export-", dir=state.settings.tmp_dir))
        out = workdir / "kaal.zip"
        with state.in_vault():
            try:
                core.call_captured(core.export_readable, SimpleNamespace(output=str(out)))
            except core.KaalError as error:
                shutil.rmtree(workdir, ignore_errors=True)
                return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return FileResponse(out, media_type="application/zip", filename=f"kaal-{stamp}.zip", background=BackgroundTask(shutil.rmtree, workdir, True))

    @app.delete("/v1/vault", tags=["account"])
    def vault_delete(request: Request, body: Proof, person: dict[str, Any] = require_owner()):
        """Delete the open vault for good, with everything in it. Needs the password (or the vault's name, typed)."""
        state = S(request)
        vault = next((entry for entry in person["vaults"] if entry["id"] == person["vault"]), None)
        if vault is None:
            return _error(404, "You do not belong to that vault.", "not_found")
        if not proved(request, state, person, body, vault["name"]):
            return _error(403, "That is not right, so nothing was deleted.", "forbidden")
        try:
            done = delete_vault(state.settings, state.accounts, state.tokens, vault["id"], state.vaults)
        except accounts.AccountError as error:
            return _error(409, str(error), "conflict")
        log.info("vault deleted by its owner")
        return answer({"deleted": done["name"], **who(person_record(state, {"id": person["session"], "account": person["account"], "vault": None}))})

    @app.get("/v1/vault/members", tags=["account"])
    def vault_members(request: Request, person: dict[str, Any] = require_owner()):
        state = S(request)
        return answer({"vault": next(vault for vault in who(person)["vaults"] if vault["id"] == person["vault"]),
                       "members": state.accounts.members(person["vault"])})

    @app.post("/v1/vault/members", tags=["account"])
    def vault_member_set(request: Request, body: MemberChange, person: dict[str, Any] = require_owner()):
        """Let someone with an account into this vault, or change their role in it."""
        state = S(request)
        if not state.invites.allow(person["account"]):
            return _error(429, "You are adding people too quickly. Try again in a minute.", "rate_limited", headers={"Retry-After": "60"})
        try:
            state.accounts.grant(body.login.strip().lower(), person["vault"], body.role, by_name=True)
        except accounts.AccountError as error:
            missing = str(error).startswith("No account")
            return _error(404 if missing else 409, "Nobody here has that sign-in name." if missing else str(error), "not_found" if missing else "conflict")
        return answer({"members": state.accounts.members(person["vault"])})

    @app.delete("/v1/vault/members/{account_id}", tags=["account"])
    def vault_member_remove(request: Request, account_id: str, person: dict[str, Any] = require_owner()):
        state = S(request)
        try:
            state.accounts.ungrant(account_id, person["vault"])
        except accounts.AccountError as error:
            return _error(409, str(error), "conflict")
        return answer({"members": state.accounts.members(person["vault"])})

    # --- who may use this installation (administrators)

    def setup_path(state: Any, account_id: str) -> str:
        """The part of a setup link after the site's address; the page that asked knows the address."""
        return f"/#/setup/{state.accounts.new_setup_code(account_id)}"

    def signup_setting(state: Any) -> str:
        return "open" if state.accounts.get("signup") == "open" else "invitation"

    @app.get("/v1/admin/people", tags=["admin"])
    def admin_people(request: Request, person: dict[str, Any] = require_admin()):
        state = S(request)
        return answer({"people": state.accounts.overview(), "signup": signup_setting(state), "you": person["account"]})

    @app.post("/v1/admin/people", tags=["admin"])
    def admin_person_add(request: Request, body: PersonNew, person: dict[str, Any] = require_admin()):
        """Create an account and a single-use link with which that person chooses their password."""
        state = S(request)
        try:
            made = state.accounts.add_account(body.login, body.name, admin=body.admin)
            vault = create_vault(state.accounts, body.vault_name.strip() or "My notes", made["id"]) if body.own_vault else None
        except accounts.AccountError as error:
            return _error(409 if "already" in str(error) else 400, str(error), "conflict" if "already" in str(error) else "invalid")
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        return answer({"account": made, "vault": vault and {"id": vault["id"], "name": vault["name"]}, "setup_path": setup_path(state, made["id"])})

    @app.post("/v1/admin/people/{account_id}/setup-link", tags=["admin"])
    def admin_person_link(request: Request, account_id: str, person: dict[str, Any] = require_admin()):
        """A new single-use link for someone who has never signed in. An administrator cannot make one
        for an account already in use: whoever uses a setup link signs in as that person, so that would
        let an administrator into other people's vaults. For someone who has lost every way in, whoever
        runs the server makes the link (kaal-server account setup-link LOGIN)."""
        state = S(request)
        target = state.accounts.account(account_id)
        if target is None:
            return _error(404, "No such account.", "not_found")
        if state.accounts.password_hash(target["id"]) is not None or state.accounts.identities_of(target["id"]):
            return _error(409, "This person has already set up their account, so only whoever runs the server can make a new link for them "
                               "(kaal-server account setup-link). They can also use one of their recovery codes.", "conflict")
        return answer({"setup_path": setup_path(state, account_id)})

    @app.post("/v1/admin/people/{account_id}/disabled", tags=["admin"])
    def admin_person_disable(request: Request, account_id: str, body: Flag, person: dict[str, Any] = require_admin()):
        state = S(request)
        if body.on and account_id == person["account"]:
            return _error(400, "You cannot switch off your own account.", "invalid")
        try:
            return answer({"account": state.accounts.set_disabled(account_id, body.on)})
        except accounts.AccountError as error:
            missing = str(error).startswith("No account")
            return _error(404 if missing else 409, str(error), "not_found" if missing else "conflict")

    @app.post("/v1/admin/people/{account_id}/admin", tags=["admin"])
    def admin_person_admin(request: Request, account_id: str, body: Flag, person: dict[str, Any] = require_admin()):
        state = S(request)
        try:
            return answer({"account": state.accounts.set_admin(account_id, body.on)})
        except accounts.AccountError as error:
            missing = str(error).startswith("No account")
            return _error(404 if missing else 409, str(error), "not_found" if missing else "conflict")

    @app.get("/v1/admin/people/{account_id}/removal", tags=["admin"])
    def admin_removal_plan(request: Request, account_id: str, person: dict[str, Any] = require_admin()):
        """What removing someone would delete, and anything that stops it."""
        state = S(request)
        try:
            return answer(_public_plan(removal_plan(state.settings, state.accounts, account_id)))
        except accounts.AccountError as error:
            return _error(404, str(error), "not_found")

    @app.delete("/v1/admin/people/{account_id}", tags=["admin"])
    def admin_remove_person(request: Request, account_id: str, body: AdminRemoval, person: dict[str, Any] = require_admin()):
        """Remove someone's account for good, with the vaults only they belonged to. Their sign-in name must be typed again."""
        state = S(request)
        target = state.accounts.account(account_id)
        if target is None:
            return _error(404, "No such account.", "not_found")
        if target["id"] == person["account"]:
            return _error(409, "To remove your own account, use Settings, Delete my account.", "conflict")
        if body.confirm.strip().lower() != target["login"]:
            return _error(400, "Type their sign-in name exactly to confirm.", "invalid")
        try:
            done = remove_account(state.settings, state.accounts, state.tokens, target["id"], state.vaults)
        except accounts.AccountError as error:
            return _error(409, str(error), "conflict")
        log.info("account removed by an administrator")
        return answer(done)

    @app.post("/v1/admin/sign-up", tags=["admin"])
    def admin_signup(request: Request, body: SignUpSetting, person: dict[str, Any] = require_admin()):
        """Whether anyone who can reach this Kaal may create an account, or only people an administrator adds."""
        state = S(request)
        state.accounts.put("signup", body.signup)
        return answer({"signup": signup_setting(state)})

    @app.get("/v1/sign-in/options", tags=["session"])
    def sign_in_options(request: Request):
        """What the sign-in page should offer. Needs no sign-in, and says nothing about who has an account."""
        state = S(request)
        return answer({"signup": signup_setting(state), "services": [{"id": service["id"], "name": service["name"]} for service in offered(state)]})

    # --- signing in through another service (Google, or any OpenID Connect provider)

    def offered(state: Any) -> list[dict[str, Any]]:
        """The sign-in services people can use: none until Kaal knows its own address."""
        return state.accounts.services() if public_address(state) else []

    def begin(request: Request, state: Any, service_id: str, purpose: str, *, account_id: str | None = None, setup_code: str | None = None) -> JSONResponse:
        """Start a trip to a sign-in service: the answer says where to send the browser."""
        service = next((entry for entry in offered(state) if entry["id"] == service_id), None)
        if service is None:
            return _error(404, "That way of signing in is not set up here.", "not_found")
        redirect = callback_address(public_address(state), service_id)
        nonce, verifier = secrets.token_urlsafe(24), oidc.new_verifier()
        try:
            flow = state.accounts.start_flow(service_id, purpose, nonce=nonce, verifier=verifier, redirect=redirect, account_id=account_id, setup_code=setup_code)
            url = state.services.address_to_visit(service, redirect=redirect, state=flow, nonce=nonce, verifier=verifier)
        except oidc.SignInServiceError as error:
            log.warning("sign-in with %s could not start: %s", log_safe(service_id, 32), log_safe(str(error), 120))
            return _error(503, f"{service['name']} could not be reached just now. Try again, or sign in another way.", "unavailable")
        response = answer({"url": url})
        # Host-only over https (which needs the path to be /), so no other site under the domain can plant one.
        response.set_cookie(flow_cookie_name(state.settings), flow, max_age=accounts.FLOW_MINUTES * 60, path="/" if state.settings.cookie_secure else FLOW_PATH,
                            httponly=True, secure=state.settings.cookie_secure, samesite="lax")
        return response

    @app.post("/v1/sign-in/with/{service_id}", tags=["session"], dependencies=[STRANGERS])
    def sign_in_with_service(request: Request, service_id: str, body: ServiceStart):
        """Start signing in through a service. With a setup link's code, the service becomes how that account signs in."""
        state = S(request)
        _from_cookie_checked(request)
        if body.setup_code and state.accounts.setup_account(body.setup_code) is None:
            return _error(404, "This setup link has been used or has run out. Ask for a new one.", "not_found")
        return begin(request, state, service_id, "setup" if body.setup_code else "sign-in", setup_code=body.setup_code or None)

    @app.post("/v1/account/connections/{service_id}", tags=["account"])
    def account_connect(request: Request, service_id: str, body: ConnectStart, person: dict[str, Any] = require_person()):
        """Start connecting a sign-in service to one's account. Someone with a password proves it first."""
        state = S(request)
        if state.accounts.password_hash(person["account"]) is not None and not checked_password(request, state, person["person"], body.password, person["name"]):
            return _error(403, "Your password is not right.", "forbidden")
        return begin(request, state, service_id, "connect", account_id=person["account"])

    @app.delete("/v1/account/connections/{service_id}", tags=["account"])
    def account_disconnect(request: Request, service_id: str, person: dict[str, Any] = require_person()):
        try:
            if not S(request).accounts.disconnect_identity(person["account"], service_id):
                return _error(404, "Your account is not connected to that.", "not_found")
        except accounts.AccountError as error:
            return _error(409, str(error), "conflict")
        return answer({"status": "disconnected", "service": service_id})

    @app.get("/v1/sign-in/with/{service_id}/callback", tags=["session"], response_class=Response, dependencies=[STRANGERS])
    def sign_in_callback(request: Request, service_id: str, code: str = Query("", max_length=4096), state: str = Query("", max_length=200),
                         error: str = Query("", max_length=200)):
        """Where a sign-in service sends the browser back to. Answers by sending the browser on into the web app."""
        kaal_state = S(request)

        def onward(path: str, cookie: str | None = None) -> Response:
            response = Response(status_code=303, headers={"Location": f"/#/{path}"})
            response.delete_cookie(flow_cookie_name(kaal_state.settings), path="/" if kaal_state.settings.cookie_secure else FLOW_PATH,
                                   httponly=True, secure=kaal_state.settings.cookie_secure, samesite="lax")
            return with_cookie(response, kaal_state, cookie) if cookie else response

        presented = exact_cookie(request, flow_cookie_name(kaal_state.settings))
        flow = kaal_state.accounts.finish_flow(state) if state and presented and secrets.compare_digest(presented.encode(), state.encode()) else None
        if flow is None or flow["service"] != service_id:
            if refused_too_often(request, kaal_state):
                raise HTTPException(429, "Too many requests.", headers={"Retry-After": "60"})
            return onward("sign-in-problem/expired")
        problem = "settings?problem=" if flow["purpose"] == "connect" else "sign-in-problem/"
        if error or not code:
            return onward(problem + "cancelled")
        service = kaal_state.accounts.service(service_id)
        if service is None:
            return onward(problem + "failed")
        try:
            claims = kaal_state.services.who_signed_in(service, oidc.open_secret(core.installation_key(), service["secret"]),
                                                       code=code, redirect=flow["redirect"], nonce=flow["nonce"], verifier=flow["verifier"])
        except (oidc.SignInServiceError, core.KaalError) as failure:
            log.warning("sign-in with %s failed: %s", log_safe(service_id, 32), log_safe(str(failure), 120))
            return onward(problem + "failed")
        subject = claims["sub"]
        email = str(claims.get("email") or "")[:254] if claims.get("email_verified") in (True, "true") else ""
        shown_name = str(claims.get("name") or "").strip()[:100]
        known = kaal_state.accounts.identity_account(service_id, subject)

        if flow["purpose"] == "connect":
            account = kaal_state.accounts.account(flow["account"]) if flow["account"] else None
            if account is None or account["disabled_at"]:
                return onward(problem + "off")
            try:
                kaal_state.accounts.connect_identity(account["id"], service_id, subject, email)
            except accounts.AccountError:
                return onward(problem + "in-use")
            request.state.token_name = f"account:{account['id']}"
            return onward(f"settings?connected={service_id}")

        if flow["purpose"] == "setup":
            account = kaal_state.accounts.setup_account("", hashed=flow["setup"])
            if account is None:
                return onward(problem + "expired")
            if known is not None and known["id"] != account["id"]:
                return onward(problem + "in-use")
            if kaal_state.accounts.setup_account("", hashed=flow["setup"], spend=True) is None:
                return onward(problem + "expired")
            kaal_state.accounts.connect_identity(account["id"], service_id, subject, email)
            if account["name"] == account["login"] and shown_name:
                kaal_state.accounts.rename(account["id"], shown_name)
            kaal_state.accounts.end_sessions(account["id"])
            return onward("receipts", open_session(request, kaal_state, account)[1])

        if known is None:
            if signup_setting(kaal_state) != "open":
                return onward(problem + "no-account")
            if not kaal_state.signups.allow():
                return onward(problem + "busy")
            try:
                known = kaal_state.accounts.add_account(kaal_state.accounts.free_login(email.partition("@")[0] or shown_name.replace(" ", ".")), shown_name)
                create_vault(kaal_state.accounts, "My notes", known["id"])
                kaal_state.accounts.connect_identity(known["id"], service_id, subject, email)
            except (accounts.AccountError, core.KaalError) as failure:
                log.warning("sign-up with %s failed: %s", service_id, type(failure).__name__)
                return onward(problem + "failed")
        if known["disabled_at"]:
            return onward(problem + "off")
        return onward("receipts", open_session(request, kaal_state, known)[1])

    # --- which sign-in services this installation offers (administrators)

    def services_told(state: Any) -> dict[str, Any]:
        address = public_address(state)
        return {"address": address, "services": [{"id": service["id"], "name": service["name"], "issuer": service["issuer"], "client_id": service["client_id"],
                                                  "callback": callback_address(address, service["id"]) if address else ""} for service in state.accounts.services()]}

    @app.get("/v1/admin/sign-in-services", tags=["admin"])
    def admin_services(request: Request, person: dict[str, Any] = require_admin()):
        return answer(services_told(S(request)))

    @app.post("/v1/admin/sign-in-services", tags=["admin"])
    def admin_service_add(request: Request, body: ServiceNew, person: dict[str, Any] = require_admin()):
        """Offer signing in through Google or another OpenID Connect service. The client secret is stored encrypted and never shown again."""
        state = S(request)
        chosen = oidc.GOOGLE if body.kind == "google" else {
            "id": re.sub(r"[^a-z0-9]+", "-", body.name.strip().lower()).strip("-")[:32], "name": body.name.strip(), "issuer": body.issuer}
        try:
            if body.kind == "other" and (chosen["id"] == "google" or not chosen["id"][:1].isalpha()):
                raise accounts.AccountError("Give the service a name that starts with a letter and is not Google.")
            address = clean_address(body.address)
            add_signin_service(state.accounts, state.services, service_id=chosen["id"], name=chosen["name"], issuer=chosen["issuer"],
                               client_id=body.client_id, client_secret=body.client_secret)
        except accounts.AccountError as error:
            return _error(400, str(error), "invalid")
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        state.accounts.put("public_address", address)
        return answer(services_told(state))

    @app.delete("/v1/admin/sign-in-services/{service_id}", tags=["admin"])
    def admin_service_remove(request: Request, service_id: str, person: dict[str, Any] = require_admin()):
        """Stop offering a service. People connected to it keep their connection in case it is added again."""
        state = S(request)
        if not state.accounts.remove_service(service_id):
            return _error(404, "No such sign-in service.", "not_found")
        return answer(services_told(state))

    @app.post("/v1/sign-up", tags=["session"], dependencies=[STRANGERS])
    def sign_up(request: Request, body: SignUp):
        """Create one's own account, where the administrator has left sign-up open."""
        state = S(request)
        if signup_setting(state) != "open":
            return _error(403, "Accounts here are created by an administrator. Ask them for a setup link.", "forbidden")
        try:
            login = accounts.clean_login(body.login)
            passwords.check_new(body.password, login=login)
            if state.accounts.account(login) is not None:
                raise accounts.AccountError("already taken")
            # Only a request that would really make an account uses up the hourly allowance.
            if not state.signups.allow():
                raise HTTPException(429, "Too many new accounts just now. Try again later.", headers={"Retry-After": "600"})
            made = state.accounts.add_account(login, body.name)
        except passwords.PasswordError as error:
            return _error(400, str(error), "invalid")
        except accounts.AccountError as error:
            return _error(409 if "already" in str(error) else 400, "That sign-in name is taken." if "already" in str(error) else str(error),
                          "conflict" if "already" in str(error) else "invalid")
        create_vault(state.accounts, body.vault_name.strip() or "My notes", made["id"])
        state.accounts.set_password(made["id"], passwords.hash_password(body.password))
        return start_session(request, state, made, recovery_codes=state.accounts.new_recovery_codes(made["id"]))

    @app.get("/v1/status", tags=["service"], dependencies=[require("read")])
    def status(request: Request):
        return _run(S(request), core.status)

    @app.get("/v1/verify", tags=["service"])
    def verify(request: Request, deep: bool = False, caller: dict[str, Any] = require("read")):
        # A deep check decrypts the whole vault while holding it; that is for its owners.
        if deep and "admin" not in caller.get("scopes", []):
            return _error(403, "A deep check needs the admin scope (a vault owner).", "forbidden")
        return _run(S(request), core.verify_vault, deep=deep)

    @app.get("/v1/export", tags=["service"], dependencies=[require("read", "reveal", "admin")], response_class=Response)
    def export(request: Request):
        state = S(request)
        refused = export_refused(request)
        if refused is not None:
            return refused
        workdir = Path(tempfile.mkdtemp(prefix="export-", dir=state.settings.tmp_dir))
        out = workdir / "kaal-export.tar"
        with state.in_vault():
            try:
                result, _ = core.call_captured(core.export_vault, SimpleNamespace(output=str(out)))
            except core.KaalError as error:
                shutil.rmtree(workdir, ignore_errors=True)
                return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind)
        headers = {"X-Kaal-Sha256": result["sha256"], "X-Kaal-Notes": str(result["notes"]), "X-Kaal-Files": str(result["files"])}
        return FileResponse(out, media_type="application/x-tar", filename="kaal-export.tar", headers=headers, background=BackgroundTask(shutil.rmtree, workdir, True))

    # --- notes

    @app.get("/v1/notes", tags=["notes"])
    def list_notes(request: Request, tag: str = Query("", max_length=200), caller: dict[str, Any] = require("read")):
        return _metadata_answer(_run(S(request), core.list_notes, tag=tag, json=True), caller)

    @app.post("/v1/notes", tags=["notes"], dependencies=[require("write", adds=True)])
    def add_note(request: Request, note: NoteCreate):
        return _run(S(request), core.add_note, title=note.title, tags=_tags(note.tags), body=note.body, body_file=None, sensitivity=note.sensitivity)

    @app.post("/v1/notes/search", tags=["notes"])
    def search_notes(request: Request, body: QueryBody, caller: dict[str, Any] = require("read")):
        return _metadata_answer(_run(S(request), core.search_notes, query=body.query), caller)

    @app.post("/v1/notes/lookup", tags=["notes"])
    def lookup_note(request: Request, body: QueryBody, caller: dict[str, Any] = require("read")):
        return _metadata_answer(_run(S(request), _lookup, query=body.query), caller)

    @app.post("/v1/notes/extract", tags=["notes"], dependencies=[require("write")])
    def extract_attachments(request: Request, run: ExtractRun):
        return _run(S(request), core.ocr_attachments, **run.model_dump())

    @app.get("/v1/notes/{note_id}", tags=["notes"])
    def show_note(request: Request, note_id: str, full: bool = False, token: dict = require("read")):
        if full and "reveal" not in token["scopes"]:
            raise HTTPException(403, "This token lacks the reveal scope.")
        shown = _run(S(request), core.show_note, exact_note=note_id, query=note_id, full=full, json=True)
        if "reveal" in token["scopes"] or shown.status_code != 200:
            return shown
        # Without reveal: the files by name and size only, and where a page was saved from by its host only.
        payload = json.loads(shown.body)
        note = payload.get("result")
        if isinstance(note, dict):
            note["attachments"] = [{key: entry[key] for key in META_ATTACHMENT_KEYS if key in entry} for entry in note.get("attachments", []) if isinstance(entry, dict)]
            note["body"] = SOURCE_LINE.sub(lambda line: f"{line.group(1)}{_host_of(line.group(2))}", str(note.get("body") or ""))
        return JSONResponse(payload)

    @app.post("/v1/notes/{note_id}/append", tags=["notes"], dependencies=[require("write", adds=True)])
    def append_note(request: Request, note_id: str, body: TextBody):
        return _run(S(request), core.append_note, exact_note=note_id, query=note_id, body=body.body)

    @app.delete("/v1/notes/{note_id}", tags=["notes"], dependencies=[require("delete")])
    def delete_note(request: Request, note_id: str, confirm: bool = False):
        return _run(S(request), core.delete_note, exact_note=note_id, query=note_id, yes=confirm)

    @app.get("/v1/notes/{note_id}/fields/{field}", tags=["notes"], dependencies=[require("read", "reveal")])
    def field_value(request: Request, note_id: str, field: str):
        return _run(S(request), _field, exact_note=note_id, query=note_id, field=field)

    @app.post("/v1/notes/{note_id}/attachments", tags=["notes"], dependencies=[require("write", adds=True)])
    def attach(request: Request, note_id: str, file: UploadFile = File(...), sensitivity: Sensitivity = Form("auto"), extract: bool = Form(False), ocr: bool = Form(False)):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.attach_file, exact_note=note_id, query=note_id, file=str(path), sensitivity=sensitivity, extract=extract, ocr=ocr)

    @app.get("/v1/notes/{note_id}/attachments/{attachment_id}/content", tags=["notes"], dependencies=[require("read", "reveal")], response_class=Response)
    def attachment_content(request: Request, note_id: str, attachment_id: str):
        return _content_response(request, S(request), note_id, attachment_id)

    # --- browser captures

    def note_attachment(state: AppState, note_id: str, attachment_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with state.in_vault():
            meta = core.note_by_id(note_id)
            matches = [att for att in meta.get("attachments", []) if att.get("id") == attachment_id]
            if not matches:
                core.die("Attachment not found.", kind="not_found")
            return meta, matches[0]

    @app.get("/v1/notes/{note_id}/attachments/{attachment_id}/view", tags=["notes"], dependencies=[require("read", "reveal")], response_class=Response)
    def view_attachment(request: Request, note_id: str, attachment_id: str):
        """A picture, PDF or plain-text file, served so the browser shows it; other kinds are refused."""
        try:
            meta, attachment = note_attachment(S(request), note_id, attachment_id)
            with S(request).beside_vault():
                data = core.attachment_original_bytes(meta, attachment)
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
        media_type = viewable_type(attachment.get("name", ""), data)
        if media_type is None:
            return _error(415, "This kind of file can only be downloaded.", "invalid")
        busy = held_download(request, len(data))
        if busy is not None:
            return busy
        headers = {
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(attachment.get('name', 'file'), safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        }
        if media_type != "application/pdf":
            # A PDF needs the browser's own viewer, which a sandbox would block.
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return Response(content=data, media_type=media_type, headers=headers)

    @app.get("/v1/notes/{note_id}/attachments/{attachment_id}/preview", tags=["notes"], dependencies=[require("read", "reveal")], response_class=Response)
    def preview_attachment(request: Request, note_id: str, attachment_id: str, page: int = Query(1, ge=1, le=20)):
        """A picture of a note's file, shrunk for the page: an image itself, or a PDF's page."""
        try:
            meta, attachment = note_attachment(S(request), note_id, attachment_id)
            with S(request).beside_vault():
                media_type, data = core.attachment_preview(meta, attachment, page)
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
        return Response(content=data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/v1/captures", tags=["captures"], dependencies=[require("write", adds=True)])
    def capture(request: Request, file: UploadFile = File(...), title: str = Form(""), source_url: str = Form(""), source_title: str = Form(""), captured_at: str = Form("")):
        with _saved_upload(S(request), file) as path:
            return _run(S(request), core.browser_capture, file=str(path), title=title, source_url=source_url, source_title=source_title, captured_at=captured_at)

    @app.post("/v1/captures/text", tags=["captures"], dependencies=[require("write", adds=True)])
    def capture_text(request: Request, body: TextCapture):
        return _run(S(request), core.browser_capture_text, **body.model_dump())

    # --- library (non-medical notes for the dashboard)

    @app.get("/v1/library/notes", tags=["library"])
    def library_list(request: Request, trash: bool = False, limit: int = Query(100, ge=0, le=100), offset: int = Query(0, ge=0), q: str = Query("", max_length=200),
                     caller: dict[str, Any] = require("read")):
        return _metadata_answer(_run(S(request), core.library_list, trash=trash, limit=limit, offset=offset, q=q), caller)

    # The words searched for travel in the request body, not the address, and
    # what comes back quotes note text, so this needs reveal.
    @app.post("/v1/library/search", tags=["library"], dependencies=[require("read", "reveal")])
    def library_search(request: Request, search: LibrarySearch):
        return _run(S(request), core.library_search, **search.model_dump())

    @app.post("/v1/library/notes", tags=["library"], dependencies=[require("write", adds=True)])
    def library_create(request: Request, note: LibraryCreate):
        return _run(S(request), core.library_create, title=note.title, tags=_tags(note.tags), body=note.body, source_url=note.source_url, source_title=note.source_title)

    @app.get("/v1/library/notes/{note_id}", tags=["library"], dependencies=[require("read", "reveal")])
    def library_show(request: Request, note_id: str):
        return _run(S(request), core.library_show, note_id=note_id)

    @app.put("/v1/library/notes/{note_id}", tags=["library"], dependencies=[require("write", adds=True)])
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

    @app.post("/v1/library/notes/{note_id}/attachments", tags=["library"], dependencies=[require("write", adds=True)])
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

    @app.post("/v1/medical/receipts", tags=["medical"], dependencies=[require("write", adds=True)])
    def medical_capture(request: Request, file: UploadFile = File(...), title: str = Form(""), source_url: str = Form(""), source_title: str = Form(""), captured_at: str = Form("")):
        state = S(request)
        defer = state.settings.background_extraction
        with _saved_upload(state, file) as path:
            response = _run(state, core.medical_capture, file=str(path), title=title, source_url=source_url, source_title=source_title, captured_at=captured_at, defer=defer)
        if defer:
            state.worker.wake()
        return response

    @app.get("/v1/medical/receipts", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_list(request: Request, inbox: bool = False, trash: bool = False, limit: int = Query(0, ge=0), offset: int = Query(0, ge=0),
                     status: str = Query("", max_length=20), q: str = Query("", max_length=200), sort: str = Query("updated", max_length=20)):
        if inbox and trash:
            raise HTTPException(400, "Choose inbox or trash, not both.")
        return _run(S(request), core.medical_list, inbox=inbox, trash=trash, json=True, limit=limit, offset=offset, status=status, q=q, sort=sort)

    @app.post("/v1/medical/report", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_report(request: Request, body: ReportFilter):
        return _run(S(request), core.medical_report, **body.model_dump())

    @app.post("/v1/medical/receipts/reimburse", tags=["medical"], dependencies=[require("write")])
    def medical_reimburse(request: Request, body: Reimburse):
        return _run(S(request), core.medical_reimburse, note_ids=body.ids, date=body.date, note=body.note, undo=body.undo)

    @app.post("/v1/medical/receipts/reextract", tags=["medical"], dependencies=[require("write")])
    def medical_reextract(request: Request, body: Reextract):
        state = S(request)
        defer = state.settings.background_extraction
        response = _run(state, core.medical_reextract, note_ids=body.ids, unreviewed=body.unreviewed, defer=defer)
        if defer:
            state.worker.wake()
        return response

    @app.get("/v1/medical/providers", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_providers(request: Request):
        return _run(S(request), core.medical_providers, forget=[])

    # Forgetting says whether a name was known, so it needs the right to read the names too.
    @app.post("/v1/medical/providers/forget", tags=["medical"], dependencies=[require("write", "reveal")])
    def medical_providers_forget(request: Request, body: ProviderForget):
        return _run(S(request), core.medical_providers, forget=body.forget)

    @app.get("/v1/medical/sources", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_sources(request: Request):
        return _run(S(request), core.medical_sources)

    @app.get("/v1/medical/people", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_people(request: Request):
        return _run(S(request), core.medical_people, add=[], remove=[])

    @app.post("/v1/medical/people", tags=["medical"], dependencies=[require("write")])
    def medical_people_change(request: Request, body: PeopleChange):
        if not body.add and not body.remove:
            raise HTTPException(400, "Give at least one name to add or remove.")
        return _run(S(request), core.medical_people, add=body.add, remove=body.remove)

    @app.post("/v1/medical/receipts/purge", tags=["medical"], dependencies=[require("delete")])
    def medical_purge_bulk(request: Request, body: BulkPurge):
        return _run(S(request), core.medical_purge_bulk, note_ids=body.ids, yes=body.confirm)

    @app.get("/v1/medical/receipts/{note_id}", tags=["medical"], dependencies=[require("read", "reveal")])
    def medical_show(request: Request, note_id: str):
        return _run(S(request), core.medical_show, note_id=note_id)

    @app.get("/v1/medical/receipts/{note_id}/attachments/{attachment_id}/preview", tags=["medical"], dependencies=[require("read", "reveal")], response_class=Response)
    def medical_preview(request: Request, note_id: str, attachment_id: str, page: int = Query(1, ge=1, le=20)):
        state = S(request)
        try:
            with state.in_vault():
                meta = core.medical_receipt_meta(core.load_index(), note_id)
                matches = [att for att in meta.get("attachments", []) if att.get("id") == attachment_id]
                if not matches:
                    core.die("Attachment not found.", kind="not_found")
            # Rendering is slow and needs no lock: it works on a private copy.
            with state.beside_vault():
                media_type, data = core.attachment_preview(meta, matches[0], page)
        except core.KaalError as error:
            return _error(STATUS_FOR_KIND.get(error.kind, 400), error.message, error.kind, error.details)
        return Response(content=data, media_type=media_type)

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
        return _content_response(request, S(request), note_id, attachment_id, receipt=True)

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
    if settings.background_extraction:
        app.state.kaal.worker.start()
    uvicorn.run(app, host=host, port=port, access_log=False, server_header=False, proxy_headers=False, log_level="warning")


def _token_owner(settings: Settings, account: str, vault: str) -> tuple[str | None, str | None]:
    """Check the account and vault a new token is for; (None, None) means the installation's first ones."""
    if not account and not vault:
        return None, None
    people = open_store(settings)
    default_account, _default_vault = people.defaults()
    person = people.account(account or default_account or "")
    if person is None:
        sys.exit(f"ERROR: No account {account!r}.")
    theirs = people.vaults_of(person["id"])
    chosen = next((v for v in theirs if v["id"] == vault), None) if vault else (theirs[0] if theirs else None)
    if chosen is None:
        sys.exit(f"ERROR: {person['login']} does not belong to {'vault ' + repr(vault) if vault else 'any vault'}.")
    return person["id"], chosen["id"]


def _token_create(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = tokens.TokenStore(settings.tokens_file)
    account, vault = _token_owner(settings, getattr(args, "account", ""), getattr(args, "vault", ""))
    try:
        record, secret = store.create(args.name, args.scopes.split(","), account=account, vault=vault)
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


def _token_scopes(args: argparse.Namespace) -> None:
    try:
        record = tokens.TokenStore(Settings.from_env().tokens_file).set_scopes(args.name, args.scopes.split(","))
    except tokens.TokenError as error:
        sys.exit(f"ERROR: {error}")
    print(json.dumps({"status": "updated", "token": record}, indent=2))


def _people(args: argparse.Namespace) -> None:
    """kaal-server account … and kaal-server vault …"""
    settings = Settings.from_env()
    try:
        store = open_store(settings)
        action = (args.cmd, args.action)
        if action == ("account", "list"):
            out: Any = [{**person, "vaults": [{"id": v["id"], "name": v["name"], "role": v["role"]} for v in store.vaults_of(person["id"])]}
                        for person in store.accounts()]
        elif action == ("account", "add"):
            person = store.add_account(args.login, args.name, admin=args.admin)
            vault = None if args.no_vault else create_vault(store, args.vault_name or "My notes", person["id"])
            out = {"status": "created", "account": person, "vault": vault}
        elif action == ("account", "setup-link"):
            found = store.account(args.login)
            if args.new_only and found is not None and (store.password_hash(found["id"]) is not None or store.identities_of(found["id"])):
                # Whoever can run this can reset any password on purpose; an installer run a second time should not by accident.
                print(f"{found['login']} has already set up their account, so no link was made.", file=sys.stderr)
                raise SystemExit(3)
            code = store.new_setup_code(args.login, hours=args.hours)
            base = (os.environ.get("KAAL_PUBLIC_URL") or "").rstrip("/") or "https://YOUR-KAAL-ADDRESS"
            print(f"Open this within {args.hours:g} hours to choose a password. It works once:", file=sys.stderr)
            print(f"{base}/#/setup/{code}")
            return
        elif action == ("account", "password"):
            person = store.account(args.login)
            if person is None:
                raise accounts.AccountError(f"No account {args.login!r}.")
            import getpass

            first, second = getpass.getpass("New password: "), getpass.getpass("Again: ")
            if first != second:
                sys.exit("ERROR: The two did not match.")
            try:
                passwords.check_new(first, login=person["login"])
            except passwords.PasswordError as error:
                sys.exit(f"ERROR: {error}")
            store.set_password(person["id"], passwords.hash_password(first))
            out = {"status": "password set", "account": person["login"], "signed_out": store.end_sessions(person["id"])}
        elif action == ("account", "rename"):
            out = {"status": "updated", "account": store.set_login(args.login, args.new_login)}
        elif action in {("account", "disable"), ("account", "enable")}:
            out = {"status": "updated", "account": store.set_disabled(args.login, args.action == "disable")}
        elif action == ("vault", "list"):
            out = [{**vault, "members": store.members(vault["id"])} for vault in store.vaults()]
        elif action == ("vault", "add"):
            out = {"status": "created", "vault": create_vault(store, args.name, args.owner)}
        elif action == ("account", "remove"):
            if not args.yes:
                out = {"status": "would_remove", **_public_plan(removal_plan(settings, store, args.login))}
            else:
                out = remove_account(settings, store, tokens.TokenStore(settings.tokens_file), args.login)
        elif action == ("vault", "remove"):
            record = store.vault(args.vault)
            if record is None:
                raise accounts.AccountError("No such vault.")
            if not args.yes:
                out = {"status": "would_delete", "vault": record["name"], "members": [member["login"] for member in store.members(record["id"])]}
            else:
                out = {"status": "deleted", **delete_vault(settings, store, tokens.TokenStore(settings.tokens_file), args.vault)}
        elif action == ("vault", "grant"):
            out = {"status": "updated", "membership": store.grant(args.login, args.vault, args.role)}
        else:
            store.ungrant(args.login, args.vault)
            out = {"status": "removed", "account": args.login, "vault": args.vault}
    except accounts.AccountError as error:
        sys.exit(f"ERROR: {error}")
    except core.KaalError as error:
        sys.exit(f"ERROR: {error.message}")
    print(json.dumps(out, indent=2))


def _read_secret(source: str) -> str:
    """A client secret from a file, from standard input, or typed at a prompt that
    does not show it. Never from the command line, where other users could see it."""
    if source != "-":
        return Path(source).expanduser().read_text().strip()
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("Paste the client secret and press Enter (it is not shown): ").strip()
    return sys.stdin.read().strip()


def _signin_services(args: argparse.Namespace) -> None:
    """kaal-server sign-in …: which services people may sign in through."""
    settings = Settings.from_env()
    try:
        store = open_store(settings)
        if args.action == "add":
            preset = oidc.GOOGLE if args.service == "google" else {}
            issuer, name = args.issuer or preset.get("issuer", ""), args.name or preset.get("name", "") or args.service
            if not issuer:
                sys.exit("ERROR: --issuer is needed for a service other than google.")
            secret = _read_secret(args.secret_file)
            address = clean_address(args.address or settings.public_url or store.get("public_address") or "")
            add_signin_service(store, oidc.Services(), service_id=args.service, name=name, issuer=issuer, client_id=args.client_id, client_secret=secret)
            store.put("public_address", address)
        elif args.action == "remove":
            if not store.remove_service(args.service):
                raise accounts.AccountError(f"No sign-in service {args.service!r}.")
        address = store.get("public_address") or settings.public_url
        out = {"address": address, "services": [{"id": service["id"], "name": service["name"], "issuer": service["issuer"], "client_id": service["client_id"],
                                                 "register_this_redirect_address": callback_address(address, service["id"]) if address else ""}
                                                for service in store.services()]}
    except accounts.AccountError as error:
        sys.exit(f"ERROR: {error}")
    except OSError as error:
        sys.exit(f"ERROR: The secret file could not be read ({type(error).__name__}).")
    except core.KaalError as error:
        sys.exit(f"ERROR: {error.message}")
    print(json.dumps(out, indent=2))


def _openapi(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        (vault / "index.json").write_text('{"version": 1, "notes": []}')
        spec = openapi_spec(create_app(Settings(home=Path(td), vault=vault, tokens_file=Path(td) / "t.json", tmp_dir=Path(td) / "tmp")))
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
    token = sub.add_parser("token", help="Issue, list, change and revoke API tokens")
    token_sub = token.add_subparsers(dest="token_cmd", required=True)
    create = token_sub.add_parser("create", help="Issue a token; it is shown once")
    create.add_argument("name")
    create.add_argument("--scopes", required=True, help=f"Comma-separated: {', '.join(tokens.SCOPES)}")
    create.add_argument("--output", default="", help="Write the token to this new file (mode 600) instead of printing it")
    create.add_argument("--account", default="", help="The sign-in name the token acts for (default: the installation's first account)")
    create.add_argument("--vault", default="", help="The id of the vault it acts in (default: that account's first vault)")
    create.set_defaults(func=_token_create)
    token_sub.add_parser("list", help="List tokens (never their secrets)").set_defaults(func=_token_list)
    revoke = token_sub.add_parser("revoke", help="Revoke a token by name or id")
    revoke.add_argument("name")
    revoke.set_defaults(func=_token_revoke)
    scopes = token_sub.add_parser("scopes", help="Change what an existing token may do; it stays signed in")
    scopes.add_argument("name")
    scopes.add_argument("--scopes", required=True, help=f"The full new list, comma-separated: {', '.join(tokens.SCOPES)}")
    scopes.set_defaults(func=_token_scopes)
    account = sub.add_parser("account", help="People who may use this installation")
    account_sub = account.add_subparsers(dest="action", required=True)
    account_sub.add_parser("list", help="Accounts and the vaults each belongs to")
    add = account_sub.add_parser("add", help="Create an account, with a vault of its own")
    add.add_argument("login", help="Sign-in name, such as a first name or an email address")
    add.add_argument("--name", default="", help="Name to show (default: the sign-in name)")
    add.add_argument("--admin", action="store_true", help="May manage accounts and vaults")
    add.add_argument("--vault-name", default="", help="What to call their vault (default: My notes)")
    add.add_argument("--no-vault", action="store_true", help="Do not create a vault; grant them one with `vault grant`")
    remove = account_sub.add_parser("remove", help="Delete an account for good, with the vaults only it belongs to")
    remove.add_argument("login")
    remove.add_argument("--yes", action="store_true", help="Really delete; without it, only say what would be deleted")
    for word, text in (("disable", "Stop an account from being used, keeping its data"), ("enable", "Let a disabled account be used again"),
                       ("password", "Set an account's password here, typed twice without being shown")):
        account_sub.add_parser(word, help=text).add_argument("login")
    rename = account_sub.add_parser("rename", help="Change the name an account signs in with")
    rename.add_argument("login")
    rename.add_argument("new_login")
    link = account_sub.add_parser("setup-link", help="Print a single-use link for choosing an account's password in the web app")
    link.add_argument("login")
    link.add_argument("--hours", type=float, default=accounts.SETUP_HOURS, help="How long the link works (default: 24)")
    link.add_argument("--new-only", action="store_true", help="Make no link (and exit 3) for someone who has already chosen a password or connected a sign-in service")
    vault = sub.add_parser("vault", help="Vaults and who belongs to each")
    vault_sub = vault.add_subparsers(dest="action", required=True)
    vault_sub.add_parser("list", help="Vaults and their members")
    vault_add = vault_sub.add_parser("add", help="Create a vault with a key of its own")
    vault_add.add_argument("name")
    vault_add.add_argument("--owner", required=True, help="Sign-in name of its first owner")
    grant = vault_sub.add_parser("grant", help="Let an account into a vault, or change its role there")
    grant.add_argument("vault", help="Vault id")
    grant.add_argument("login")
    grant.add_argument("role", choices=accounts.ROLES)
    vault_remove = vault_sub.add_parser("remove", help="Delete a vault for good, with everything in it")
    vault_remove.add_argument("vault", help="Vault id")
    vault_remove.add_argument("--yes", action="store_true", help="Really delete; without it, only say what would be deleted")
    ungrant = vault_sub.add_parser("ungrant", help="Take an account out of a vault")
    ungrant.add_argument("vault", help="Vault id")
    ungrant.add_argument("login")
    for parser_ in (account, vault):
        parser_.set_defaults(func=_people)
    signin = sub.add_parser("sign-in", help="Services people may sign in through, such as Google")
    signin_sub = signin.add_subparsers(dest="action", required=True)
    signin_sub.add_parser("list", help="The services offered and the redirect address to register with each")
    signin_add = signin_sub.add_parser("add", help="Offer a service, or replace its client id and secret")
    signin_add.add_argument("service", help="google, or a short id of your own for another OpenID Connect service")
    signin_add.add_argument("--client-id", required=True)
    signin_add.add_argument("--secret-file", default="-", help="A file holding the client secret. Without it, the secret is asked for (or read from standard input)")
    signin_add.add_argument("--address", default="", help="The address people open Kaal at, such as https://kaal.example.org (default: KAAL_PUBLIC_URL)")
    signin_add.add_argument("--issuer", default="", help="The service's address, for one other than google")
    signin_add.add_argument("--name", default="", help="What the button calls it (default: the id)")
    signin_sub.add_parser("remove", help="Stop offering a service").add_argument("service")
    signin.set_defaults(func=_signin_services)
    spec = sub.add_parser("openapi", help="Print the OpenAPI spec")
    spec.add_argument("--output", default="")
    spec.set_defaults(func=_openapi)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
