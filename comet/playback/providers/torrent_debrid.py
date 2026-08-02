"""Existing torrent/debrid delivery behind the shared playback-provider contract."""

from __future__ import annotations

from collections.abc import Mapping

import aiohttp

from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
)
from comet.debrid.manager import get_debrid
from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderStatus,
    Readiness,
)


class TorrentDebridProvider:
    """One credential-bound StremThru store selected by a stable v2 provider ID."""

    def __init__(
        self,
        session,
        provider_kind: str,
        api_key: str,
        client_ip: str,
    ):
        self._session = session
        self._provider_kind = provider_kind
        self._api_key = api_key
        self._client_ip = client_ip
        self.descriptor = ProviderDescriptor(
            provider_kind,
            provider_kind,
            frozenset({"torrent"}),
            frozenset({BytePath.CLOUD_REDIRECT, BytePath.SERVER_RELAY}),
            True,
        )

    async def validate_config(self, _options: Mapping[str, object]) -> ProviderStatus:
        """Perform the existing read-only account/subscription preflight."""
        client = get_debrid(
            self._session,
            "",
            "",
            self._provider_kind,
            self._api_key,
            self._client_ip,
        )
        try:
            async with self._session.get(
                f"{client.base_url}/user",
                params={"client_ip": self._client_ip},
                headers={
                    **client._headers(),
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=8,
                    connect=3,
                    sock_connect=3,
                    sock_read=5,
                ),
            ) as response:
                if response.status in {401, 403}:
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code="credentials_rejected",
                        auth_failed=True,
                    )
                if response.status == 429 or response.status >= 500:
                    return ProviderStatus(
                        Readiness.RETRYABLE_FAILURE,
                        Actionability.NONE,
                        code="validation_unavailable",
                    )
                if not is_success_status(response.status):
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code="plan_incompatible",
                    )
                payload = await read_provider_json(response)
        except (aiohttp.ClientError, TimeoutError, ProviderJsonError):
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.NONE,
                code="validation_unavailable",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="plan_incompatible",
            )
        return ProviderStatus(
            Readiness.READY,
            Actionability.SERVER_ON_DEMAND,
        )

    async def generate_download_link(
        self,
        *,
        info_hash: str,
        file_index: int | None,
        selection_title: str,
        display_title: str,
        video_id: str,
        media_only_id: str,
        season: int | None,
        episode: int | None,
        aliases: dict,
        client_ip: str,
    ) -> str | None:
        client = get_debrid(
            self._session,
            video_id,
            media_only_id,
            self._provider_kind,
            self._api_key,
            client_ip,
        )
        return await client.generate_download_link(
            info_hash,
            "n" if file_index is None else str(file_index),
            display_title,
            selection_title,
            season,
            episode,
            [],
            aliases,
        )

    @property
    def account_key(self) -> str:
        return self._api_key
