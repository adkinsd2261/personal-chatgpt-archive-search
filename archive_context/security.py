from __future__ import annotations

import hashlib
import hmac


TOKEN_ENV = "ARCHIVE_CONTEXT_TOKEN"
MINIMUM_TOKEN_CHARACTERS = 32


class TokenConfigurationError(RuntimeError):
    pass


class BearerTokenVerifier:
    """Verify bearer tokens without retaining the configured plaintext token."""

    def __init__(self, token: str | None) -> None:
        if token is None or not token.strip():
            raise TokenConfigurationError(f"{TOKEN_ENV} must be set before the service starts.")
        if token != token.strip():
            raise TokenConfigurationError(f"{TOKEN_ENV} must not have leading or trailing whitespace.")
        if len(token) < MINIMUM_TOKEN_CHARACTERS:
            raise TokenConfigurationError(
                f"{TOKEN_ENV} must contain at least {MINIMUM_TOKEN_CHARACTERS} characters."
            )
        self._expected_digest = hashlib.sha256(token.encode("utf-8")).digest()

    def matches_authorization_header(self, value: str | None) -> bool:
        if value is None:
            return False
        parts = value.split()
        if len(parts) != 2 or parts[0].casefold() != "bearer":
            return False
        supplied_digest = hashlib.sha256(parts[1].encode("utf-8", errors="replace")).digest()
        return hmac.compare_digest(self._expected_digest, supplied_digest)
