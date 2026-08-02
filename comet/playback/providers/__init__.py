"""Product-specific playback providers."""

from .altmount import AltMountProvider
from .easynews import EasynewsProvider
from .native_usenet import NativeUsenetProvider
from .nzbdav import NzbDavProvider
from .stremio_nntp import StremioNntpProvider
from .stremthru_newz import StremThruNewzProvider
from .torbox_usenet import TorBoxUsenetProvider
from .torrent_debrid import TorrentDebridProvider

__all__ = (
    "AltMountProvider",
    "EasynewsProvider",
    "NativeUsenetProvider",
    "NzbDavProvider",
    "StremThruNewzProvider",
    "StremioNntpProvider",
    "TorBoxUsenetProvider",
    "TorrentDebridProvider",
)
