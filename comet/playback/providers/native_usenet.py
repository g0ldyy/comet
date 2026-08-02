"""Access-gated Comet native Usenet provider descriptor."""

from comet.core.models import settings
from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderStatus,
    Readiness,
)
from comet.usenet.nntp_config import (
    NntpServerConfig,
    parse_instance_servers,
    parse_personal_servers,
)


class NativeUsenetProvider:
    descriptor = ProviderDescriptor(
        kind="comet_native_usenet",
        label="Comet NNTP",
        accepted_locator_kinds=frozenset({"nzb_artifact", "real_nzb"}),
        byte_paths=frozenset({BytePath.NATIVE_ENGINE}),
        mutates_upstream=False,
    )

    def __init__(self, access_error_code: str | None = None):
        self._access_error_code = access_error_code

    @staticmethod
    def servers_for(config: dict) -> tuple[NntpServerConfig, ...]:
        """Resolve exactly one request-authorized native source without persistence."""
        source = config.get("source")
        if source == "instance_pool":
            return parse_instance_servers(settings.USENET_NATIVE_SERVERS)
        if source == "personal_servers":
            return parse_personal_servers(config.get("servers"))
        raise ValueError("native NNTP server source is unavailable")

    async def validate_config(self, config: dict) -> ProviderStatus:
        if self._access_error_code is not None:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code=self._access_error_code,
                auth_failed=True,
            )
        source = config.get("source")
        if source not in {"instance_pool", "personal_servers"}:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="source_required",
            )
        if not settings.USENET_ENGINE_ENABLED:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="engine_unavailable",
            )
        if source == "instance_pool":
            try:
                self.servers_for(config)
            except ValueError:
                return ProviderStatus(
                    Readiness.TERMINAL_FAILURE,
                    Actionability.NONE,
                    code="servers_unavailable",
                )
        if (
            source == "personal_servers"
            and not settings.USENET_NATIVE_ALLOW_USER_SERVERS
        ):
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="personal_servers_disabled",
            )
        if source == "personal_servers":
            try:
                self.servers_for(config)
            except ValueError:
                return ProviderStatus(
                    Readiness.TERMINAL_FAILURE,
                    Actionability.NONE,
                    code="personal_servers_required",
                    auth_failed=True,
                )
        return ProviderStatus(
            Readiness.REQUIRES_PREPARE, Actionability.REMOTE_PREPARE, None
        )
