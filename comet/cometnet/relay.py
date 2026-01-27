"""
CometNet Relay Client

HTTP client for relaying torrent broadcasts to an external CometNet service.
Used in cluster deployments where Comet workers send torrents to a
dedicated CometNet standalone service.
"""

import asyncio
import traceback
from typing import Any, Dict, List, Optional

import aiohttp

from comet.cometnet.interface import CometNetBackend
from comet.core.logger import logger


class CometNetRelay(CometNetBackend):
    """
    HTTP client for relaying torrents to a standalone CometNet service.

    This is used when COMETNET_RELAY_URL is configured, allowing Comet workers
    to send torrent broadcasts to an external CometNet service instead of
    running their own P2P network.
    """

    def __init__(
        self, relay_url: str, timeout: float = 30.0, api_key: Optional[str] = None
    ):
        """
        Initialize the relay client.

        Args:
            relay_url: Base URL of the CometNet standalone service (e.g., http://cometnet:8766)
            timeout: Request timeout in seconds
            api_key: Optional API key for authentication
        """
        self.relay_url = relay_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._batch: List[Dict] = []
        self._batch_lock = asyncio.Lock()
        self._batch_task: Optional[asyncio.Task] = None
        self._running = False

        self.batch_size = 50
        self.batch_interval = 2.0

        self._total_relayed = 0
        self._total_errors = 0
        self._last_error: Optional[str] = None

    @property
    def running(self) -> bool:
        """Check if the relay is running."""
        return self._running

    async def start(self):
        """Start the relay client."""
        if self._running:
            return

        self._running = True

        headers = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            json_serialize=lambda x: __import__("orjson").dumps(x).decode(),
            headers=headers,
        )

        self._batch_task = asyncio.create_task(self._batch_flush_loop())

        logger.log("COMETNET", f"Relay client started - Target: {self.relay_url}")

    async def stop(self):
        """Stop the relay client and flush remaining batch."""
        self._running = False

        await self._flush_batch()

        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        logger.log(
            "COMETNET",
            f"Relay client stopped - Relayed: {self._total_relayed}, Errors: {self._total_errors}",
        )

    async def relay_torrent(
        self,
        info_hash: str,
        title: str,
        size: int,
        tracker: str = "",
        imdb_id: Optional[str] = None,
        file_index: Optional[int] = None,
        seeders: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        sources: Optional[List[str]] = None,
        parsed: Optional[dict] = None,
    ) -> bool:
        """
        Queue a torrent for relay to the standalone CometNet service.

        Returns True if queued successfully.
        """
        if not self._running:
            return False

        torrent_data = {
            "info_hash": info_hash,
            "title": title,
            "size": size,
            "tracker": tracker,
            "imdb_id": imdb_id,
            "file_index": file_index,
            "seeders": seeders,
            "season": season,
            "episode": episode,
            "sources": sources,
            "parsed": parsed,
        }

        async with self._batch_lock:
            self._batch.append(torrent_data)

            if len(self._batch) >= self.batch_size:
                asyncio.create_task(self._flush_batch())

        return True

    async def _batch_flush_loop(self):
        """Periodically flush the batch."""
        while self._running:
            try:
                await asyncio.sleep(self.batch_interval)
                await self._flush_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Relay batch flush error: {e}")

    async def _flush_batch(self):
        """Flush the current batch to the CometNet service."""
        async with self._batch_lock:
            if not self._batch:
                return

            batch_to_send = self._batch
            self._batch = []

        if not self._session:
            return

        try:
            if len(batch_to_send) == 1:
                await self._send_single(batch_to_send[0])
            else:
                await self._send_batch(batch_to_send)
        except asyncio.TimeoutError:
            self._total_errors += len(batch_to_send)
            logger.warning(
                f"Relay batch send timed out after {self.timeout}s - "
                f"Remote is likely overloaded ({len(batch_to_send)} torrents dropped)"
            )
        except Exception as e:
            self._total_errors += len(batch_to_send)
            logger.debug(f"Relay batch send failed: {e}")
            logger.debug(traceback.format_exc())

    async def _send_single(self, torrent: Dict) -> bool:
        """Send a single torrent to the relay."""
        try:
            async with self._session.post(
                f"{self.relay_url}/broadcast",
                json=torrent,
            ) as response:
                if response.status == 200:
                    self._total_relayed += 1
                    logger.log(
                        "COMETNET",
                        f"Relayed torrent {torrent['info_hash']} to {self.relay_url}",
                    )
                    return True
                else:
                    self._total_errors += 1
                    logger.warning(
                        f"Relay returned {response.status} from {self.relay_url}"
                    )
                    return False
        except aiohttp.ClientError as e:
            self._total_errors += 1
            logger.warning(f"Relay connection error to {self.relay_url}: {e}")
            return False

    async def _send_batch(self, torrents: List[Dict]) -> int:
        """Send a batch of torrents to the relay. Returns number successfully queued."""
        try:
            async with self._session.post(
                f"{self.relay_url}/broadcast/batch",
                json={"torrents": torrents},
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    queued = data.get("queued", 0)
                    errors = len(data.get("errors", []))
                    self._total_relayed += queued
                    self._total_errors += errors
                    logger.log(
                        "COMETNET",
                        f"Relayed batch of {queued} torrents to {self.relay_url}",
                    )
                    return queued
                else:
                    self._total_errors += len(torrents)
                    logger.warning(
                        f"Relay batch returned {response.status} from {self.relay_url}"
                    )
                    return 0
        except aiohttp.ClientError as e:
            self._total_errors += len(torrents)
            logger.warning(f"Relay batch connection error to {self.relay_url}: {e}")
            return 0

    async def health_check(self) -> bool:
        """Check if the relay target is healthy."""
        if not self._session:
            return False

        try:
            async with self._session.get(f"{self.relay_url}/health") as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("status") == "healthy"
                return False
        except Exception:
            return False

    async def get_stats(self) -> Dict:
        """Get relay statistics (merges remote stats with local relay stats)."""
        remote_stats = await self.fetch_remote_stats() or {}
        local_stats = {
            "relay_url": self.relay_url,
            "running": self._running,
            "total_relayed": self._total_relayed,
            "total_errors": self._total_errors,
            "batch_pending": len(self._batch),
            "last_error": self._last_error,
        }
        # Merge local relay stats under 'relay' key
        remote_stats["relay"] = local_stats
        return remote_stats

    async def fetch_remote_stats(self) -> Optional[Dict]:
        """Fetch stats from the remote CometNet standalone service."""
        if not self._session or not self._running:
            return None

        try:
            async with self._session.get(f"{self.relay_url}/stats") as response:
                if response.status == 200:
                    self._last_error = None
                    return await response.json()
                elif response.status == 401:
                    self._last_error = "Authentication failed: API Key required"
                elif response.status == 403:
                    self._last_error = "Authentication failed: Invalid API Key"
                else:
                    self._last_error = f"Remote error: {response.status}"
                return None
        except Exception as e:
            self._last_error = f"Connection failed: {str(e)}"
            logger.debug(f"Failed to fetch remote stats: {e}")
            return None

    async def fetch_remote_pools(self) -> Optional[Dict]:
        """Fetch pools from the remote CometNet standalone service."""
        if not self._session or not self._running:
            return None

        try:
            async with self._session.get(f"{self.relay_url}/pools") as response:
                if response.status == 200:
                    return await response.json()
                # Pools endpoint might not exist on older standalone versions
                return {"pools": {}, "memberships": [], "subscriptions": []}
        except Exception:
            # Return empty pools if not available
            return {"pools": {}, "memberships": [], "subscriptions": []}

    async def get_peers(self) -> Dict[str, Any]:
        """Get peers from the remote CometNet standalone service."""
        if not self._session or not self._running:
            return {"peers": [], "count": 0}

        try:
            async with self._session.get(f"{self.relay_url}/peers") as response:
                if response.status == 200:
                    return await response.json()
                return {"peers": [], "count": 0}
        except Exception:
            return {"peers": [], "count": 0}

    # --- Pool Management (proxied to standalone) ---

    async def _pool_request(
        self, method: str, path: str, json_data: Optional[Dict] = None
    ) -> Dict:
        """Make a pool management request to the standalone service."""
        if not self._session or not self._running:
            raise RuntimeError("Relay not running")

        url = f"{self.relay_url}{path}"
        try:
            if method == "GET":
                async with self._session.get(url) as response:
                    return await self._handle_pool_response(response)
            elif method == "POST":
                async with self._session.post(url, json=json_data or {}) as response:
                    return await self._handle_pool_response(response)
            elif method == "DELETE":
                async with self._session.delete(url) as response:
                    return await self._handle_pool_response(response)
            elif method == "PATCH":
                async with self._session.patch(url, json=json_data or {}) as response:
                    return await self._handle_pool_response(response)
            else:
                raise ValueError(f"Unsupported method: {method}")
        except aiohttp.ClientError as e:
            logger.warning(f"Pool request failed: {e}")
            raise RuntimeError(f"Failed to connect to standalone: {e}")

    async def _handle_pool_response(self, response) -> Dict:
        """Handle response from standalone pool endpoints."""
        if response.status == 200:
            return await response.json()
        elif response.status == 404:
            raise ValueError("Pool not found")
        elif response.status == 400:
            data = await response.json()
            raise ValueError(data.get("detail", "Bad request"))
        elif response.status == 403:
            raise PermissionError("Permission denied")
        else:
            raise RuntimeError(f"Standalone returned {response.status}")

    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        description: str = "",
        join_mode: str = "invite",
    ) -> Dict:
        """Create a pool on the standalone service."""
        return await self._pool_request(
            "POST",
            "/pools",
            {
                "pool_id": pool_id,
                "display_name": display_name,
                "description": description,
                "join_mode": join_mode,
            },
        )

    async def delete_pool(self, pool_id: str) -> bool:
        """Delete a pool on the standalone service."""
        try:
            await self._pool_request("DELETE", f"/pools/{pool_id}")
            return True
        except Exception:
            return False

    async def get_pools(self) -> Dict:
        """Get pools from the standalone service."""
        pools = await self.fetch_remote_pools()
        return pools if pools else {"pools": {}, "memberships": [], "subscriptions": []}

    async def join_pool_with_invite(
        self, pool_id: str, invite_code: str, node_url: Optional[str] = None
    ) -> bool:
        """Join a pool using an invite code."""
        try:
            await self._pool_request(
                "POST",
                f"/pools/{pool_id}/join",
                {"invite_code": invite_code, "node_url": node_url},
            )
            return True
        except Exception:
            return False

    async def create_pool_invite(
        self,
        pool_id: str,
        expires_in: Optional[int] = None,
        max_uses: Optional[int] = None,
    ) -> Optional[str]:
        """Create an invite for a pool."""
        try:
            result = await self._pool_request(
                "POST",
                f"/pools/{pool_id}/invite",
                {"expires_in": expires_in, "max_uses": max_uses},
            )
            return result.get("invite_link")
        except Exception:
            return None

    async def delete_pool_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete a pool invite."""
        try:
            await self._pool_request(
                "DELETE", f"/pools/{pool_id}/invites/{invite_code}"
            )
            return True
        except Exception:
            return False

    async def get_pool_invites(self, pool_id: str) -> Dict[str, Any]:
        """Get active invites for a pool."""
        try:
            return await self._pool_request("GET", f"/pools/{pool_id}/invites")
        except Exception:
            return {}

    async def subscribe_to_pool(self, pool_id: str) -> bool:
        """Subscribe to a pool."""
        try:
            await self._pool_request("POST", f"/pools/{pool_id}/subscribe")
            return True
        except Exception:
            return False

    async def unsubscribe_from_pool(self, pool_id: str) -> bool:
        """Unsubscribe from a pool."""
        try:
            await self._pool_request("DELETE", f"/pools/{pool_id}/subscribe")
            return True
        except Exception:
            return False

    async def add_pool_member(
        self, pool_id: str, member_key: str, role: str = "member"
    ) -> bool:
        """Add a member to a pool."""
        try:
            await self._pool_request(
                "POST",
                f"/pools/{pool_id}/members",
                {"member_key": member_key, "role": role},
            )
            return True
        except Exception:
            return False

    async def remove_pool_member(self, pool_id: str, member_key: str) -> bool:
        """Remove a member from a pool."""
        try:
            await self._pool_request("DELETE", f"/pools/{pool_id}/members/{member_key}")
            return True
        except Exception:
            return False

    async def get_pool_details(self, pool_id: str) -> Optional[Dict]:
        """Get detailed information about a pool including all members."""
        try:
            return await self._pool_request("GET", f"/pools/{pool_id}")
        except Exception:
            return None

    async def update_member_role(
        self, pool_id: str, member_key: str, new_role: str
    ) -> bool:
        """Change a member's role (promote to admin or demote to member)."""
        try:
            await self._pool_request(
                "PATCH",
                f"/pools/{pool_id}/members/{member_key}/role",
                {"role": new_role},
            )
            return True
        except ValueError as e:
            raise ValueError(str(e))
        except PermissionError as e:
            raise PermissionError(str(e))
        except Exception:
            return False

    async def leave_pool(self, pool_id: str) -> bool:
        """Leave a pool (self-removal). Any member except creator can leave."""
        try:
            await self._pool_request("POST", f"/pools/{pool_id}/leave")
            return True
        except ValueError as e:
            raise ValueError(str(e))
        except PermissionError as e:
            raise PermissionError(str(e))
        except Exception:
            return False

    async def broadcast_torrents(self, metadata_list: List[Any]) -> None:
        """Broadcast multiple torrents to the network (via relay)."""
        if not self._running:
            return

        batch_data = []
        for metadata in metadata_list:
            if hasattr(metadata, "model_dump"):
                data = metadata.model_dump()
            elif isinstance(metadata, dict):
                data = metadata
            else:
                continue

            info_hash = data.get("info_hash")
            if not info_hash or len(info_hash) != 40:
                continue

            torrent_data = {
                "info_hash": info_hash,
                "title": data.get("title", ""),
                "size": data.get("size", 0),
                "tracker": data.get("tracker", ""),
                "imdb_id": data.get("imdb_id"),
                "file_index": data.get("file_index"),
                "seeders": data.get("seeders"),
                "season": data.get("season"),
                "episode": data.get("episode"),
                "sources": data.get("sources"),
                "parsed": data.get("parsed"),
            }
            batch_data.append(torrent_data)

        if not batch_data:
            return

        async with self._batch_lock:
            self._batch.extend(batch_data)

            if len(self._batch) >= self.batch_size:
                asyncio.create_task(self._flush_batch())

    async def broadcast_torrent(self, metadata) -> None:
        """Broadcast a torrent to the network (via relay)."""
        await self.broadcast_torrents([metadata])


_relay_instance: Optional[CometNetRelay] = None


def get_relay() -> Optional[CometNetRelay]:
    """Get the global relay instance."""
    return _relay_instance


async def init_relay(relay_url: str, api_key: Optional[str] = None) -> CometNetRelay:
    """Initialize the global relay instance."""
    global _relay_instance

    if _relay_instance is not None:
        await _relay_instance.stop()

    _relay_instance = CometNetRelay(relay_url, api_key=api_key)
    await _relay_instance.start()

    return _relay_instance


async def stop_relay():
    """Stop the global relay instance."""
    global _relay_instance

    if _relay_instance is not None:
        await _relay_instance.stop()
        _relay_instance = None
