from typing import Any


class DebridError(Exception):
    """Base exception for debrid-related errors."""

    def __init__(self, message: str, display_message: str = None):
        self.message = message
        self.display_message = display_message or message
        super().__init__(self.message)


class DebridAuthError(DebridError):
    """Raised when debrid authentication fails (not premium, invalid API key, etc.)."""

    def __init__(self, debrid_name: str, message: str = None):
        self.debrid_name = debrid_name
        default_message = f"{debrid_name}: Authentication failed or not premium"
        display_message = (
            message
            or f"{debrid_name}: Invalid API key or no active subscription.\nPlease check your debrid account."
        )
        super().__init__(default_message, display_message)


class DebridLinkGenerationError(DebridError):
    """Raised when StremThru returns an error while generating a playback link."""

    def __init__(
        self,
        debrid_name: str,
        message: str,
        *,
        error_code: str | None = None,
        upstream_error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ):
        self.debrid_name = debrid_name
        self.error_code = error_code
        self.upstream_error_code = upstream_error_code
        self.payload = payload or {}
        super().__init__(message, message)

    @property
    def status_keys(self) -> list[str]:
        codes = []
        if self.upstream_error_code:
            codes.append(self.upstream_error_code)
        if self.error_code and self.error_code not in codes:
            codes.append(self.error_code)
        return codes
