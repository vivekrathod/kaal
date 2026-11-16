"""A made-up sign-in service for tests: it speaks just enough OpenID Connect
to stand in for Google, and never touches the network."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _number(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC = ec.generate_private_key(ec.SECP256R1())


class FakeService:
    """One sign-in service. ``sign_in`` plays the person at the service's own pages."""

    def __init__(self, issuer="https://accounts.google.com", client_id="made-up-client.apps.example", client_secret="made-up client secret"):
        self.issuer, self.client_id, self.client_secret = issuer, client_id, client_secret
        self.codes: dict[str, dict] = {}
        self.asked: list[str] = []
        self.down = False
        self.algorithm = "RS256"
        self.kid = "key-1"
        self.signer = _RSA
        # Changes made to the next statement before it is signed, to play a service that lies.
        self.twist = lambda claims: claims
        self.raw_statement = None

    def keys(self):
        public = _RSA.public_key().public_numbers()
        curve = _EC.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "key-1", "n": _number(public.n), "e": _number(public.e)},
                         {"kty": "EC", "crv": "P-256", "use": "sig", "kid": "key-ec", "x": b64(curve.x.to_bytes(32, "big")), "y": b64(curve.y.to_bytes(32, "big"))}]}

    def statement(self, claims: dict) -> str:
        head = {"alg": self.algorithm, "typ": "JWT", "kid": self.kid}
        signed = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(claims).encode())}"
        if self.algorithm == "none":
            return signed + "."
        if self.algorithm == "ES256":
            r, s = decode_dss_signature(_EC.sign(signed.encode(), ec.ECDSA(hashes.SHA256())))
            return f"{signed}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"{signed}.{b64(self.signer.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256()))}"

    def use_wrong_key(self):
        self.signer = _OTHER_RSA

    def fetch(self, url: str, form=None):
        self.asked.append(url)
        if self.down:
            return 503, b"down"
        if url == f"{self.issuer}/.well-known/openid-configuration":
            return 200, json.dumps({"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize", "token_endpoint": f"{self.issuer}/token",
                                    "jwks_uri": f"{self.issuer}/keys"}).encode()
        if url == f"{self.issuer}/keys":
            return 200, json.dumps(self.keys()).encode()
        if url == f"{self.issuer}/token":
            trip = self.codes.pop((form or {}).get("code", ""), None)
            if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
                return 401, b'{"error": "invalid_client"}'
            challenge = b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if trip is None or form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != trip["redirect_uri"] or challenge != trip["challenge"]:
                return 400, b'{"error": "invalid_grant"}'
            if self.raw_statement is not None:
                return 200, json.dumps({"id_token": self.raw_statement}).encode()
            now = int(time.time())
            claims = self.twist({"iss": self.issuer, "aud": self.client_id, "sub": trip["sub"], "email": trip["email"], "email_verified": trip["verified"],
                                 "name": trip["name"], "nonce": trip["nonce"], "iat": now, "exp": now + 300})
            return 200, json.dumps({"access_token": "unused", "token_type": "Bearer", "id_token": self.statement(claims)}).encode()
        return 404, b"{}"

    def sign_in(self, url: str, sub: str, email: str = "", name: str = "", verified: bool = True, refuse: bool = False) -> str:
        """The person signs in at the service as ``sub``; returns the path the browser is sent back to."""
        asked = urlsplit(url)
        query = {key: values[0] for key, values in parse_qs(asked.query).items()}
        assert f"{asked.scheme}://{asked.netloc}{asked.path}" == f"{self.issuer}/authorize"
        assert query["response_type"] == "code" and query["client_id"] == self.client_id and query["code_challenge_method"] == "S256"
        assert query["scope"] == "openid email profile"
        back = urlsplit(query["redirect_uri"])
        if refuse:
            return f"{back.path}?{urlencode({'error': 'access_denied', 'state': query['state']})}"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"sub": sub, "email": email, "name": name, "verified": verified, "nonce": query["nonce"], "challenge": query["code_challenge"],
                            "redirect_uri": query["redirect_uri"]}
        return f"{back.path}?{urlencode({'code': code, 'state': query['state']})}"
