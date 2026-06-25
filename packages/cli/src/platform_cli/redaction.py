"""Shared metadata redaction helpers for support artifacts."""

from __future__ import annotations

SENSITIVE_METADATA_TOKENS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
NON_SECRET_TOKEN_METRIC_KEYS = {"input_tokens", "output_tokens", "total_tokens"}


def redact_metadata(value: object) -> object:
    """Recursively redact secret-looking keys in support artifacts."""
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if is_sensitive_metadata_key(normalized):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_metadata(item)
        return redacted
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    return value


def is_sensitive_metadata_key(normalized_key: str) -> bool:
    """Return whether a metadata key should be redacted as a secret."""
    if normalized_key in NON_SECRET_TOKEN_METRIC_KEYS:
        return False
    return any(token in normalized_key for token in SENSITIVE_METADATA_TOKENS)
