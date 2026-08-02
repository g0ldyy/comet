"""Capability-backed manifest badges for explicitly configured Usenet providers."""

import asyncio
from collections.abc import Mapping

from comet.core.capability_bindings import (
    build_playback_capability_bindings,
    native_instance_credential_material,
)
from comet.core.capability_states import CapabilityStateRepository
from comet.playback.tokens import CapabilityCodec

_USENET_PROVIDER_BADGES = {
    "torbox_usenet": "TORBOX-NZB",
    "stremio_nntp": "STREMIO-NNTP",
    "nzbdav": "NZBDAV",
    "comet_native_usenet": "COMET-NNTP",
    "altmount": "ALTMOUNT",
    "easynews": "EASYNEWS",
    "stremthru_newz": "STREMTHRU-NEWZ",
}


async def eligible_usenet_provider_badges(
    config: Mapping[str, object],
    database,
    *,
    usenet_offered: bool,
    capability_secret: str | None,
    native_access_token: str | None,
    native_servers: object,
) -> tuple[str, ...]:
    """Read current capability evidence without validation or provider I/O."""
    if (
        not usenet_offered
        or config.get("schemaVersion") != 2
        or "usenet" not in (config.get("enabledTransports") or ())
        or not capability_secret
    ):
        return ()
    bindings = build_playback_capability_bindings(
        config,
        CapabilityCodec(capability_secret),
        instance_credential_material={
            "comet_native_usenet": native_instance_credential_material(
                native_access_token,
                native_servers,
            )
        },
    )
    if not bindings:
        return ()

    repository = CapabilityStateRepository(database)
    states = await asyncio.gather(
        *(
            repository.effective(binding.binding.binding_fingerprint)
            for binding in bindings
        ),
    )
    badges = []
    for binding, state in zip(bindings, states, strict=True):
        badge = _USENET_PROVIDER_BADGES.get(binding.binding.binding_kind)
        if badge is not None and state.eligible and badge not in badges:
            badges.append(badge)
    return tuple(badges)
