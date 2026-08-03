"""Canonical credential extraction shared by the discovery and playback registries."""

API_CREDENTIAL_KEYS = ("apiKey", "api_key", "token")
EASYNEWS_CREDENTIAL_KEYS = ("username", "password")
ACCOUNT_METADATA_KEYS = frozenset({"kind"})


def account_options(value: object) -> dict[str, object]:
    """Expose credential/options fields without leaking envelope metadata."""
    if value is None:
        return {}
    return {
        key: nested for key, nested in value.items() if key not in ACCOUNT_METADATA_KEYS
    }


def api_credential(value: object) -> str | None:
    """Return the first non-blank API credential a binding carries, in key order."""
    if value is None:
        return None
    for key in API_CREDENTIAL_KEYS:
        candidate = value.get(key)
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def easynews_credentials(value: object) -> tuple[str, str] | None:
    """Return a complete Easynews pair, or None so the account binding can supply one."""
    if value is None:
        return None
    username, password = (value.get(key) for key in EASYNEWS_CREDENTIAL_KEYS)
    if not username or not password:
        return None
    return username, password
