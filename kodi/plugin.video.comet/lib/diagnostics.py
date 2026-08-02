"""Fail-closed Kodi diagnostics with no request or configuration payloads."""

import re

import xbmc

_EVENTS = frozenset(
    {
        "kodi.http.failed",
        "kodi.main.failed",
        "kodi.setup.failed",
        "kodi.setup.poll.degraded",
        "kodi.setup.poll.recovered",
        "kodi.setup.completed",
        "kodi.setup_elementum.failed",
        "kodi.setup_tmdb.failed",
    }
)
_OUTCOMES = frozenset({"ok", "failed", "timeout", "cancelled"})
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)


def emit(event, level=xbmc.LOGERROR, *, outcome=None, error=None, status=None):
    """Emit one bounded record; invalid diagnostics are silently discarded."""
    if event not in _EVENTS:
        return
    fields = []
    if outcome in _OUTCOMES:
        fields.append("outcome=" + outcome)
    if error is not None:
        error_type = type(error).__name__
        if _ERROR_TYPE.fullmatch(error_type):
            fields.append("error_type=" + error_type)
    if type(status) is int and 100 <= status <= 599:
        fields.append("status=" + str(status))
    suffix = " " + " ".join(fields) if fields else ""
    xbmc.log("[Comet] event=" + event + suffix, level)


def run_boundary(event, operation):
    """Own an executable Kodi script boundary without exposing argv or URLs."""
    try:
        return operation()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        emit(event, error=exc, outcome="failed")
        return None
