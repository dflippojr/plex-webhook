"""Admin-token check for room administration routes, and the optional webhook URL token.

The token comes only from the ADMIN_API_TOKEN environment variable and is a
shared client credential: a valid one proves the caller holds the configured
secret (identity ``owner-admin``), never which human used it. Request-supplied
actor labels, forwarded headers and Plex payload fields are never consulted.

The webhook token comes only from WEBHOOK_TOKEN. Only its digest is kept, and
neither the token nor a presented value is ever logged, audited or returned.
"""
import hashlib
import hmac
import os
import re

from app.audit import DENIAL_REASONS  # re-exported: app.metrics pre-creates metric labels from it

MIN_TOKEN_LENGTH = 16
MIN_WEBHOOK_TOKEN_LENGTH = 32
# Characters that need no percent-encoding in a URL path segment.
_URL_SAFE = re.compile(r"[A-Za-z0-9._~-]+")


class WebhookTokenError(ValueError):
    """A configured WEBHOOK_TOKEN is unusable; the message never contains the value."""


def _digest(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


def denial_reason(authorization):
    """Return None for a valid admin credential, else a fixed reason code."""
    configured = os.environ.get("ADMIN_API_TOKEN", "")
    if len(configured) < MIN_TOKEN_LENGTH:
        return "auth_unconfigured"
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return "missing_credential"
    # Digest comparison keeps the check constant-time regardless of length.
    if not hmac.compare_digest(_digest(presented), _digest(configured)):
        return "invalid_credential"
    return None


def webhook_token_digest(configured):
    """Digest of the configured WEBHOOK_TOKEN value, or None when it is unset or empty.

    Raises WebhookTokenError, so startup fails, for a value shorter than
    MIN_WEBHOOK_TOKEN_LENGTH or one that would need encoding in a URL path.
    """
    if not configured:
        return None
    if len(configured) < MIN_WEBHOOK_TOKEN_LENGTH or not _URL_SAFE.fullmatch(configured):
        raise WebhookTokenError(f"WEBHOOK_TOKEN must be at least {MIN_WEBHOOK_TOKEN_LENGTH} characters from "
                                "A-Z, a-z, 0-9, '-', '_', '.' and '~'")
    return _digest(configured)


def webhook_token_matches(presented, digest):
    """Constant-time check of a presented webhook token against the configured digest."""
    return hmac.compare_digest(_digest(presented), digest)
