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
