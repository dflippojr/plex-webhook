"""Admin-token check for room administration routes.

The token comes only from the ADMIN_API_TOKEN environment variable and is a
shared client credential: a valid one proves the caller holds the configured
secret (identity ``owner-admin``), never which human used it. Request-supplied
actor labels, forwarded headers and Plex payload fields are never consulted.
"""
import hashlib
import hmac
import os

from app.audit import DENIAL_REASONS  # re-exported: main.py pre-creates metric labels from it

MIN_TOKEN_LENGTH = 16


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
