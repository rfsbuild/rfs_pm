#!/usr/bin/env python3
"""Cloudflare Access identity for the PM board's PUBLIC listener.

WHY THIS FILE EXISTS
    The board at 127.0.0.1:8789 is Hadassa's daily working surface and has no
    auth, deliberately — only she can reach loopback. On 2026-09-14 she decided
    Rafael should answer inside that same board rather than over Slack, which
    means one listener has to be reachable from the internet.

    That listener cannot trust its peer address: cloudflared connects FROM
    127.0.0.1, so any "local is fine" exemption would be an internet-wide
    exemption. It cannot trust the Host header either — cloudflared forwards
    whatever Host arrives, so a remote request can claim to be localhost. The
    only thing it can trust is a signature it checks itself.

    So: a SECOND port (PUBLIC_PORT), and on it every single request must carry a
    Cloudflare Access JWT signed by the team's JWKS, for THIS application's aud,
    unexpired. Same contract as scripts/rbaadmin_server.py in the dashboard repo,
    which Rafael already authenticates through (logs/rbaadmin.out shows
    "profit page served to rafael@rfsbuilders.com").

FAILS CLOSED
    If PM_ACCESS_TEAM or PM_ACCESS_AUD is unset, the public listener does not
    start AT ALL (see pm_server.main). There is no bypass flag and no debug
    mode. An unconfigured deployment is an offline one, never an open one.
"""
import base64
import json
import time
import urllib.request

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes

_JWKS_TTL = 600          # re-fetch the signing keys at most this often
_jwks: dict = {}
_jwks_at: float = 0.0


class AccessDenied(Exception):
    """Carries the reason, which is logged but never sent to the client —
    a precise auth error is a hint to whoever is probing."""


def _b64(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _pubkey(jwk):
    n = int.from_bytes(_b64(jwk["n"]), "big")
    e = int.from_bytes(_b64(jwk["e"]), "big")
    return RSAPublicNumbers(e, n).public_key()


def _keys(team: str, force: bool = False) -> dict:
    global _jwks, _jwks_at
    if not force and _jwks and (time.time() - _jwks_at) < _JWKS_TTL:
        return _jwks
    url = "https://%s.cloudflareaccess.com/cdn-cgi/access/certs" % team
    with urllib.request.urlopen(url, timeout=10) as r:
        doc = json.loads(r.read().decode())
    _jwks = {k["kid"]: k for k in doc.get("keys", [])}
    _jwks_at = time.time()
    return _jwks


def token_from(headers, cookie_header: str = "") -> str:
    """Access puts the JWT in a header on proxied requests and in a cookie on
    browser navigations. Accept either; prefer the header."""
    tok = headers.get("Cf-Access-Jwt-Assertion") or ""
    if tok:
        return tok
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "CF_Authorization":
            return v
    return ""


def verify(token: str, team: str, aud: str) -> str:
    """Return the verified e-mail, or raise AccessDenied. Checks, in order:
    shape · algorithm · signature against the team JWKS · aud · issuer · exp."""
    if not team or not aud:
        raise AccessDenied("PM_ACCESS_TEAM / PM_ACCESS_AUD not configured")
    if token.count(".") != 2:
        raise AccessDenied("no Access token on the request")
    h64, p64, s64 = token.split(".")
    try:
        hdr = json.loads(_b64(h64))
        pay = json.loads(_b64(p64))
        sig = _b64(s64)
    except Exception:
        raise AccessDenied("malformed token")
    if hdr.get("alg") != "RS256":
        raise AccessDenied("unexpected algorithm %r" % hdr.get("alg"))

    jwk = _keys(team).get(hdr.get("kid"))
    if jwk is None:                      # key rotated → one forced refresh
        jwk = _keys(team, force=True).get(hdr.get("kid"))
    if jwk is None:
        raise AccessDenied("unknown signing key")
    try:
        _pubkey(jwk).verify(sig, ("%s.%s" % (h64, p64)).encode(),
                            padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        raise AccessDenied("bad signature")

    auds = pay.get("aud")
    auds = auds if isinstance(auds, list) else [auds]
    if aud not in auds:
        raise AccessDenied("token is for a different application")
    if pay.get("iss") != "https://%s.cloudflareaccess.com" % team:
        raise AccessDenied("token issued by a different team")
    if float(pay.get("exp", 0)) < time.time():
        raise AccessDenied("token expired")
    email = (pay.get("email") or "").strip().lower()
    if not email:
        raise AccessDenied("token carries no e-mail")
    return email
