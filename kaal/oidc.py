"""Signing in through another service with OpenID Connect: Google, or any
provider that publishes a discovery document.

Kaal sends the browser to the service, the service sends it back with a
one-time code, and Kaal exchanges that code for a signed statement of who the
person is. Kaal keeps only the service's own id for the person (``sub``) and
their email address for display; it never sees their password there and asks
for no access to anything of theirs.

Only the authorization-code flow with PKCE is spoken, and only to https
addresses. Every statement's signature, issuer, audience, expiry and nonce is
checked.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import http.client
import json
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlencode, urlsplit

GOOGLE = {"id": "google", "name": "Google", "issuer": "https://accounts.google.com"}
TIMEOUT_SECONDS = 10         # for each step of a request: connecting, and each wait for more of the answer
TOTAL_SECONDS = 20           # for reading a whole answer, however slowly it trickles in
FAIL_SECONDS = 30            # how long a service that could not be reached is not asked again
CALLS_AT_ONCE = 4            # requests to sign-in services in progress at one time, for everyone
WAIT_SECONDS = 3             # how long a request waits its turn before the visitor is told to try again
MAX_ANSWER_BYTES = 1024 * 1024
KEEP_SECONDS = 3600          # how long a service's published settings and keys are remembered
CLOCK_SLACK_SECONDS = 120
_SECRET_AAD = b"kaal:signin-secret-v1"

# (url, form fields to POST or None for GET) -> (status, body)
Fetch = Callable[[str, dict[str, str] | None], tuple[int, bytes]]


class SignInServiceError(Exception):
    """The trip to the service failed. ``kind`` is safe to show; the message is for the log."""

    def __init__(self, message: str, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


def _give_up(connection: http.client.HTTPSConnection) -> None:
    """Close a connection from outside, so that whatever is waiting on it stops waiting."""
    held = connection.sock
    if held is not None:
        try:
            held.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            held.close()
        except OSError:
            pass


def fetch(url: str, form: dict[str, str] | None = None, *, trusting: ssl.SSLContext | None = None) -> tuple[int, bytes]:
    """One https request: no redirects followed, no unbounded answer, and one deadline for all of
    it (the status line, the headers and the body of any answer), however slowly it trickles in."""
    if not url.startswith("https://"):
        raise SignInServiceError("refusing an address that is not https")
    connection = None
    try:
        parts = urlsplit(url)
        # ``trusting`` is for tests, which play a service with a certificate of their own making.
        started = time.monotonic()
        connection = http.client.HTTPSConnection(parts.hostname or "", parts.port or 443, timeout=TIMEOUT_SECONDS, context=trusting)
        # Connected first, so that the deadline has a connection to close: making it counts too.
        connection.connect()
        left = TOTAL_SECONDS - (time.monotonic() - started)
        if left <= 0:
            raise OSError("the sign-in service took too long to reach")
        # The timeout above restarts with every byte that arrives; this does not.
        watchdog = threading.Timer(left, _give_up, [connection])
        watchdog.daemon = True
        watchdog.start()
        try:
            body = urlencode(form).encode("ascii") if form is not None else None
            headers = {"Accept": "application/json", "User-Agent": "kaal"}
            if body is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            connection.request("POST" if body is not None else "GET", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), body=body, headers=headers)
            response = connection.getresponse()
            answer = b""
            while len(answer) <= MAX_ANSWER_BYTES:
                piece = response.read1(64 * 1024)
                if not piece:
                    break
                answer += piece
            return response.status, answer[:MAX_ANSWER_BYTES]
        finally:
            watchdog.cancel()
    except (http.client.HTTPException, OSError, ValueError) as error:
        raise SignInServiceError(f"could not reach the sign-in service ({type(error).__name__})", "unreachable") from None
    finally:
        if connection is not None:
            connection.close()


def seal_secret(installation_key: bytes, secret: str) -> str:
    """A client secret, encrypted with the installation's key, as text for the accounts store."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return base64.b64encode(nonce + AESGCM(installation_key).encrypt(nonce, secret.encode("utf-8"), _SECRET_AAD)).decode("ascii")


def open_secret(installation_key: bytes, sealed: str) -> str:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(sealed, validate=True)
        return AESGCM(installation_key).decrypt(raw[:12], raw[12:], _SECRET_AAD).decode("utf-8")
    except (InvalidTag, ValueError, binascii.Error):
        raise SignInServiceError("the stored client secret could not be opened with the installation's key", "misconfigured") from None


def _unpadded(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decoded(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_verifier() -> str:
    return secrets.token_urlsafe(48)


def challenge_for(verifier: str) -> str:
    return _unpadded(hashlib.sha256(verifier.encode("ascii")).digest())


def _json(body: bytes, what: str) -> dict[str, Any]:
    try:
        found = json.loads(body)
    except ValueError:
        found = None
    if not isinstance(found, dict):
        raise SignInServiceError(f"the service's {what} was not JSON")
    return found


def _public_key(jwk: dict[str, Any]) -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    number = lambda field: int.from_bytes(_decoded(jwk[field]), "big")  # noqa: E731
    if jwk.get("kty") == "RSA":
        return rsa.RSAPublicNumbers(number("e"), number("n")).public_key()
    if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
        return ec.EllipticCurvePublicNumbers(number("x"), number("y"), ec.SECP256R1()).public_key()
    raise SignInServiceError("the service signs with a kind of key Kaal does not know")


def _signature_holds(algorithm: str, key: Any, signature: bytes, signed: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        if algorithm == "RS256" and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        elif algorithm == "ES256" and isinstance(key, ec.EllipticCurvePublicKey) and len(signature) == 64:
            key.verify(encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")), signed, ec.ECDSA(hashes.SHA256()))
        else:
            return False
    except InvalidSignature:
        return False
    return True


def read_statement(token: str, keys: list[dict[str, Any]], *, issuer: str, client_id: str, nonce: str, now: float | None = None) -> dict[str, Any]:
    """The claims of an ID token, once everything about it has been checked."""
    try:
        head_text, body_text, signature_text = token.split(".")
        head, claims, signature = json.loads(_decoded(head_text)), json.loads(_decoded(body_text)), _decoded(signature_text)
    except (ValueError, binascii.Error, AttributeError):
        raise SignInServiceError("the service's statement could not be read") from None
    if not isinstance(head, dict) or not isinstance(claims, dict) or head.get("alg") not in {"RS256", "ES256"}:
        raise SignInServiceError("the service's statement is signed in a way Kaal does not accept")
    candidates = [key for key in keys if isinstance(key, dict) and key.get("use", "sig") == "sig" and (not head.get("kid") or key.get("kid") == head.get("kid"))]
    signed = f"{head_text}.{body_text}".encode("ascii")
    for candidate in candidates:
        try:
            public = _public_key(candidate)
        except (SignInServiceError, KeyError, ValueError, binascii.Error):
            continue
        if _signature_holds(head["alg"], public, signature, signed):
            break
    else:
        raise SignInServiceError("the service's statement is not signed by one of its keys", "unknown-key")
    moment = time.time() if now is None else now
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    # Google has also issued statements naming itself without the https:// in front.
    if claims.get("iss") not in {issuer, issuer.removeprefix("https://")}:
        raise SignInServiceError("the statement names another issuer")
    if client_id not in audiences or (len(audiences) > 1 and claims.get("azp") != client_id):
        raise SignInServiceError("the statement was made for another application")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < moment - CLOCK_SLACK_SECONDS:
        raise SignInServiceError("the statement has run out")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > moment + CLOCK_SLACK_SECONDS:
        raise SignInServiceError("the statement is dated in the future")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SignInServiceError("the statement does not belong to this sign-in")
    if not isinstance(claims.get("sub"), str) or not claims["sub"] or len(claims["sub"]) > 255:
        raise SignInServiceError("the statement does not say who signed in")
    return claims


class Services:
    """Talks to sign-in services, remembering what each publishes about itself."""

    def __init__(self, fetch_with: Fetch | None = None) -> None:
        self._fetch_with = fetch_with
        self._lock = threading.Lock()
        self._settings: dict[str, tuple[float, dict[str, Any]]] = {}
        self._keys: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # A service that is down is not asked again by every visitor: the failure is remembered
        # briefly, and only one request at a time goes out for each thing a service publishes.
        self._failed: dict[tuple[str, str], tuple[float, SignInServiceError]] = {}
        self._flights: dict[tuple[str, str], threading.Lock] = {}

    @contextlib.contextmanager
    def _flight(self, what: str, issuer: str) -> Iterator[None]:
        """One request at a time for each thing a service publishes; whoever waits too long for
        their turn is told to try again, rather than holding a thread behind a slow service."""
        with self._lock:
            lock = self._flights.setdefault((what, issuer), threading.Lock())
        if not lock.acquire(timeout=WAIT_SECONDS):
            raise SignInServiceError("the sign-in service is slow to answer", "unreachable")
        try:
            yield
        finally:
            lock.release()

    def _recent_failure(self, what: str, issuer: str) -> None:
        with self._lock:
            failed = self._failed.get((what, issuer))
            if failed and failed[0] > time.monotonic():
                raise failed[1]
            self._failed.pop((what, issuer), None)

    def _remember_failure(self, what: str, issuer: str, error: SignInServiceError) -> None:
        with self._lock:
            self._failed[(what, issuer)] = (time.monotonic() + FAIL_SECONDS, error)

    # Every request to a sign-in service holds one of the server's worker threads while it waits,
    # so only a few are in progress at once; the rest are told to try again rather than queue.
    _calls = threading.BoundedSemaphore(CALLS_AT_ONCE)

    def _fetch(self, url: str, form: dict[str, str] | None = None) -> tuple[int, bytes]:
        if not url.startswith("https://"):
            raise SignInServiceError("refusing an address that is not https")
        if not self._calls.acquire(timeout=WAIT_SECONDS):
            raise SignInServiceError("too many requests to sign-in services are in progress", "unreachable")
        try:
            return (self._fetch_with or fetch)(url, form)
        finally:
            self._calls.release()

    def settings(self, issuer: str) -> dict[str, Any]:
        """What the service publishes at /.well-known/openid-configuration."""
        def kept_now() -> dict[str, Any] | None:
            with self._lock:
                kept = self._settings.get(issuer)
            return kept[1] if kept and kept[0] > time.monotonic() else None

        found = kept_now()
        if found is not None:
            return found
        with self._flight("settings", issuer):
            found = kept_now()
            if found is not None:
                return found
            self._recent_failure("settings", issuer)
            try:
                status, body = self._fetch(f"{issuer}/.well-known/openid-configuration")
                if status != 200:
                    raise SignInServiceError(f"the service's settings answered {status}", "unreachable")
                found = _json(body, "settings")
                if str(found.get("issuer", "")).rstrip("/") != issuer:
                    raise SignInServiceError("the service's settings name another issuer")
                for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    if not str(found.get(field, "")).startswith("https://"):
                        raise SignInServiceError(f"the service's settings have no usable {field}")
            except SignInServiceError as error:
                self._remember_failure("settings", issuer, error)
                raise
            with self._lock:
                self._settings[issuer] = (time.monotonic() + KEEP_SECONDS, found)
            return found

    def _signing_keys(self, issuer: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        def kept_now() -> tuple[float, list[dict[str, Any]]] | None:
            with self._lock:
                kept = self._keys.get(issuer)
            return kept if kept and kept[0] > time.monotonic() else None

        kept = kept_now()
        if kept and not fresh:
            return kept[1]
        asked = time.monotonic()
        with self._flight("keys", issuer):
            kept = kept_now()
            # Someone else fetched them while this request waited its turn: those are fresh enough.
            if kept and (not fresh or kept[0] - KEEP_SECONDS >= asked):
                return kept[1]
            self._recent_failure("keys", issuer)
            try:
                status, body = self._fetch(self.settings(issuer)["jwks_uri"])
                keys = _json(body, "keys").get("keys") if status == 200 else None
                if not isinstance(keys, list):
                    raise SignInServiceError("the service's signing keys could not be fetched", "unreachable")
            except SignInServiceError as error:
                self._remember_failure("keys", issuer, error)
                raise
            with self._lock:
                self._keys[issuer] = (time.monotonic() + KEEP_SECONDS, keys)
            return keys

    def address_to_visit(self, service: dict[str, Any], *, redirect: str, state: str, nonce: str, verifier: str) -> str:
        """Where to send the browser so the person can sign in at the service."""
        query = {"response_type": "code", "client_id": service["client_id"], "redirect_uri": redirect, "scope": "openid email profile",
                 "state": state, "nonce": nonce, "code_challenge": challenge_for(verifier), "code_challenge_method": "S256", "prompt": "select_account"}
        return f"{self.settings(service['issuer'])['authorization_endpoint']}?{urlencode(query)}"

    def who_signed_in(self, service: dict[str, Any], secret: str, *, code: str, redirect: str, nonce: str, verifier: str) -> dict[str, Any]:
        """Exchange the one-time code for the service's statement and return its checked claims."""
        issuer = service["issuer"]
        status, body = self._fetch(self.settings(issuer)["token_endpoint"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect, "client_id": service["client_id"], "client_secret": secret, "code_verifier": verifier})
        if status != 200:
            # The answer may explain itself, but it is the service's text: only its short error word is kept.
            try:
                word = str(_json(body, "answer").get("error", ""))[:40]
            except SignInServiceError:
                word = ""
            raise SignInServiceError(f"the service refused the code ({status} {word})".replace(" )", ")"), "misconfigured" if word in {"invalid_client", "unauthorized_client"} else "failed")
        token = _json(body, "answer").get("id_token")
        if not isinstance(token, str):
            raise SignInServiceError("the service's answer has no statement of who signed in")
        check = {"issuer": issuer, "client_id": service["client_id"], "nonce": nonce}
        try:
            return read_statement(token, self._signing_keys(issuer), **check)
        except SignInServiceError as error:
            if error.kind != "unknown-key":
                raise
        # The service may have changed its keys since they were last fetched.
        try:
            return read_statement(token, self._signing_keys(issuer, fresh=True), **check)
        except SignInServiceError as error:
            raise SignInServiceError(str(error)) from None
