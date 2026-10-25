"""Secret redaction, bridge signatures, URL checks and text sanitising.

Everything an agent prints goes through RedactingStream, so a secret from .env
never reaches a terminal or log file even if a library includes it in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import socket
import sys
import time
from typing import TextIO
from urllib.parse import urlsplit

# Values of these variables are masked in all output. Seeds are private keys.
SECRET_ENV_NAMES = (
    "GROQ_API_KEY",
    "AGENTVERSE_API_KEY",
    "SPACETIME_TOKEN",
    "APIFY_API_TOKEN",
    "JWT_SECRET",
    "BRIDGE_SHARED_SECRET",
    "ORCHESTRATOR_SEED",
    "WEBSITE_AGENT_SEED",
    "CAREGIVER_SEED",
    "TECHNICAL_HELPER_SEED",
    "SCRAPER_SEED",
    "CARTESIA_API_KEY",
)
MASK = "***"


def secret_values() -> list[str]:
    values = {os.getenv(name, "").strip() for name in SECRET_ENV_NAMES}
    # Longest first, so a secret that contains another is masked whole.
    return sorted((value for value in values if len(value) >= 8), key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    for secret in secrets if secrets is not None else secret_values():
        if secret in text:
            text = text.replace(secret, MASK)
    return text


class RedactingStream:
    """A text stream that masks secret values before writing."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._secrets = secret_values()

    def refresh(self) -> None:
        self._secrets = secret_values()

    def write(self, text: str) -> int:
        self._stream.write(redact(text, self._secrets))
        return len(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def install_redaction() -> None:
    """Mask secrets in everything written to stdout and stderr. Safe to call twice."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            continue
        if isinstance(stream, RedactingStream):
            stream.refresh()
        else:
            setattr(sys, name, RedactingStream(stream))


# Bridge requests from the backend are signed with BRIDGE_SHARED_SECRET.
BRIDGE_MAX_SKEW_SECONDS = 300


def bridge_signature(secret: str, timestamp: int, nonce: str, user_id: str, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    payload = f"{timestamp}.{nonce}.{user_id}.{digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


class NonceCache:
    """Remembers recent nonces so a signed bridge request can't be replayed."""

    def __init__(self, ttl: float = 2 * BRIDGE_MAX_SKEW_SECONDS) -> None:
        self._ttl = ttl
        self._seen: dict[str, float] = {}

    def add(self, nonce: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        for old in [key for key, at in self._seen.items() if now - at > self._ttl]:
            del self._seen[old]
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True


def verify_bridge(
    secret: str,
    timestamp: int,
    nonce: str,
    user_id: str,
    body: str,
    signature: str,
    nonces: NonceCache,
    now: float | None = None,
) -> str | None:
    """None if the request is genuine, otherwise the reason it isn't."""
    if not secret:
        return "bridge_disabled"
    now = time.time() if now is None else now
    if abs(now - timestamp) > BRIDGE_MAX_SKEW_SECONDS:
        return "stale_request"
    if not nonce or len(nonce) > 64:
        return "bad_nonce"
    expected = bridge_signature(secret, timestamp, nonce, user_id, body)
    if not hmac.compare_digest(expected, signature or ""):
        return "bad_signature"
    if not nonces.add(nonce, now):
        return "replayed_request"
    return None


# Outbound URLs that come from people or scraped data.
class UnsafeURL(ValueError):
    pass


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def check_public_url(url: str, *, resolve: bool = True) -> str:
    """Return the URL if it is http(s) on a public host and a normal port; raise UnsafeURL otherwise.

    Stops requests to localhost, private networks, link-local metadata services and
    other internal addresses (SSRF).
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise UnsafeURL("Only http and https addresses are allowed.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("That address has no host.")
    if parts.username or parts.password:
        raise UnsafeURL("Addresses with a username or password aren't allowed.")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("That address has an invalid port.") from exc
    if port not in (None, 80, 443):
        raise UnsafeURL("Only the standard web ports are allowed.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home")):
        raise UnsafeURL("Local addresses aren't allowed.")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not _public_ip(host):
            raise UnsafeURL("Private or local network addresses aren't allowed.")
        return url
    if "." not in host:
        raise UnsafeURL("That doesn't look like a public web address.")
    if resolve:
        try:
            infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeURL("I couldn't find that website.") from exc
        addresses = {info[4][0] for info in infos}
        if not addresses or not all(_public_ip(address) for address in addresses):
            raise UnsafeURL("That website points to a private network address.")
    return url


def host_matches(url: str, allowed_hosts: set[str] | frozenset[str]) -> bool:
    """True if the URL's host is one of allowed_hosts or a subdomain of one."""
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


# Scraped text is untrusted. Keep it short, plain and free of markup.
_TAGS = re.compile(r"<[^>]{0,500}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_SPACE = re.compile(r"\s+")


def clean_text(value: object, max_len: int = 300) -> str:
    if value is None:
        return ""
    text = str(value)
    text = _TAGS.sub(" ", text)
    text = _CONTROL.sub("", text)
    text = _SPACE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "..."
    return text


def user_ref(identity: str) -> str:
    """A stable pseudonymous id for a person, so their address isn't stored or logged."""
    return "hmh-u-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


def short_address(address: str) -> str:
    if len(address) <= 16:
        return address
    return f"{address[:10]}...{address[-4:]}"


URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
_BARE_URL = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|co|uk|us|ca|au|io|info|de|fr|in)\b)(/\S*)?", re.I)


def find_url(text: str) -> str | None:
    """The first web address in a message, with https:// added to a bare domain."""
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;:!?")
    bare = _BARE_URL.search(text)
    if bare:
        return "https://" + bare.group(0).rstrip(".,;:!?")
    return None


_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_LONG_NUMBER = re.compile(r"\b(?:\d[ -]?){12,19}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}\b")


def mask_personal(text: str) -> str:
    """Hide email addresses, phone numbers and card-like numbers before text leaves the orchestrator."""
    text = _EMAIL.sub("[email]", text)
    text = _LONG_NUMBER.sub("[number]", text)
    return _PHONE.sub("[phone number]", text)
