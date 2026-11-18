"""Password hashing for accounts.

Argon2id, with a fresh salt per password. A stored hash reads
``argon2id$m=19456,t=2,p=1$<salt>$<hash>`` (base64 without padding), so the
cost can be raised later and old hashes still verify. Nothing here logs or
returns a password.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import threading

# OWASP's recommended Argon2id setting: 19 MiB of memory, two passes, one lane.
MEMORY_KIB, ITERATIONS, LANES, LENGTH = 19456, 2, 1, 32
MIN_LENGTH, MAX_LENGTH = 10, 256


class PasswordError(Exception):
    """The password offered cannot be used; the message is safe to show."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


class PasswordBusy(Exception):
    """Too many passwords are being checked at once; the caller should say so and let the person try again."""


# Each check takes about 20 MiB and a core for a moment. Only a few run at once, so a flood of
# sign-in attempts slows sign-in down instead of taking the whole server's memory and threads.
_checks = threading.BoundedSemaphore(4)
WAIT_SECONDS = 3.0


def set_concurrency(count: int) -> None:
    global _checks
    _checks = threading.BoundedSemaphore(max(1, count))


def _derive(password: str, salt: bytes, memory: int, iterations: int, lanes: int, length: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

    gate = _checks
    if not gate.acquire(timeout=WAIT_SECONDS):
        raise PasswordBusy()
    try:
        return Argon2id(salt=salt, length=length, iterations=iterations, lanes=lanes, memory_cost=memory).derive(password.encode("utf-8"))
    finally:
        gate.release()


def check_new(password: str, *, login: str = "") -> None:
    """Refuse a password that is too short, too long, or the person's own sign-in name."""
    if not isinstance(password, str) or len(password) < MIN_LENGTH:
        raise PasswordError(f"Choose a password of at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"A password can be at most {MAX_LENGTH} characters.")
    if login and password.strip().lower() == login.strip().lower():
        raise PasswordError("Choose a password that is not your sign-in name.")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt, MEMORY_KIB, ITERATIONS, LANES, LENGTH)
    return f"argon2id$m={MEMORY_KIB},t={ITERATIONS},p={LANES}${_b64(salt)}${_b64(derived)}"


def verify_password(stored: str | None, password: str) -> bool:
    """Whether ``password`` matches. Takes about as long whether or not there is a stored hash."""
    try:
        scheme, costs, salt_text, hash_text = (stored or _decoy()).split("$")
        if scheme != "argon2id":
            raise ValueError
        cost = dict(part.split("=") for part in costs.split(","))
        salt, expected = _unb64(salt_text), _unb64(hash_text)
        derived = _derive(password if isinstance(password, str) else "", salt, int(cost["m"]), int(cost["t"]), int(cost["p"]), len(expected))
    except (ValueError, KeyError):
        return False
    return hmac.compare_digest(derived, expected) and stored is not None


def needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"argon2id$m={MEMORY_KIB},t={ITERATIONS},p={LANES}$")


_DECOY: str | None = None


def _decoy() -> str:
    """Verified against when an account has no password, so a sign-in attempt
    for a missing account costs the same as one for a real account."""
    global _DECOY
    if _DECOY is None:
        _DECOY = hash_password(secrets.token_urlsafe(24))
    return _DECOY
