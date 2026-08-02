"""The deliberately small native-Usenet access gate."""

import secrets


class NativeAccessAuthorizer:
    """Authorizes only the request-local token configured by the operator."""

    def __init__(self, configured_token: str | None):
        self._configured_token = configured_token.encode() if configured_token else b""

    def authorized(self, provided_token: str | None) -> bool:
        """Return false without creating a token identity or persistent record."""
        return self.error_code(provided_token) is None

    def error_code(self, provided_token: str | None) -> str | None:
        if provided_token is None:
            return "native_access_token_required"
        if not self._configured_token or not secrets.compare_digest(
            self._configured_token,
            provided_token.encode(),
        ):
            return "native_access_token_rejected"
        return None
