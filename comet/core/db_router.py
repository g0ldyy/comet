import asyncio
import contextvars
import socket
import time
from collections.abc import Sequence
from contextlib import contextmanager

from databases import Database
from sqlalchemy.engine.url import make_url

from comet.core.db_transactions import write_transaction
from comet.observability import log, metrics

_REPLICA_RETRY_DELAY_SECONDS = 30.0
_IPV4_RESOLUTION_TIMEOUT_SECONDS = 5.0


class ReplicaAwareDatabase:
    """Routes read queries to replicas while keeping writes on the primary."""

    def __init__(
        self,
        primary: Database,
        replicas: Sequence[Database] | None = None,
        force_ipv4: bool = False,
    ):
        self._primary = primary
        self._configured_replicas = list(replicas or [])
        self._force_ipv4 = force_ipv4
        self._active_replicas: list[Database] = []
        self._replica_retry_after = {}
        self._unavailable_replicas: set[Database] = set()
        self._replica_reconnect_lock = asyncio.Lock()
        self._replica_index = 0
        self._transaction_depth = contextvars.ContextVar(
            "comet_db_replica_tx_depth", default=0
        )
        self._force_primary_context = contextvars.ContextVar(
            "comet_db_replica_force_primary", default=False
        )

    @property
    def is_connected(self):
        return self._primary.is_connected

    @property
    def configured_replica_count(self) -> int:
        return len(self._configured_replicas)

    @property
    def active_replica_count(self) -> int:
        return len(self._active_replicas)

    @property
    def unavailable_replica_count(self) -> int:
        return len(self._unavailable_replicas)

    async def connect(self):
        # Handle IP resolution override
        if self._force_ipv4:
            self._primary = await self._resolve_and_recreate(self._primary)
            self._configured_replicas = [
                await self._resolve_and_recreate(replica)
                for replica in self._configured_replicas
            ]

        await self._primary.connect()

        healthy_replicas: list[Database] = []
        self._unavailable_replicas.clear()
        for slot, replica in enumerate(self._configured_replicas, start=1):
            try:
                await replica.connect()
            except Exception as exc:  # pragma: no cover - driver-specific
                self._unavailable_replicas.add(replica)
                self._deactivate_replica(replica)
                log.warning(
                    "database.replica.unavailable",
                    "Database replica became unavailable",
                    replica_slot=slot,
                    error_code="replica_connection_failed",
                    exc=exc,
                )
            else:
                healthy_replicas.append(replica)

        self._active_replicas = healthy_replicas
        self._replica_retry_after.clear()

    async def disconnect(self):
        for db in [self._primary, *self._configured_replicas]:
            if not db.is_connected:
                continue
            try:
                await db.disconnect()
            except Exception:  # pragma: no cover - defensive logging
                pass
        self._active_replicas = []
        self._replica_retry_after.clear()
        self._unavailable_replicas.clear()

    @staticmethod
    async def prepare_replicas(replicas: Sequence[Database]) -> list[Database]:
        replacements = list(replicas)
        connected: list[Database] = []
        try:
            for replica in replacements:
                await replica.connect()
                connected.append(replica)
        except BaseException:
            await asyncio.gather(
                *(replica.disconnect() for replica in connected),
                return_exceptions=True,
            )
            raise

        return replacements

    def replace_replicas(self, replacements: list[Database]) -> list[Database]:
        previous = self._configured_replicas
        self._configured_replicas = replacements
        self._active_replicas = replacements.copy()
        self._replica_retry_after.clear()
        self._unavailable_replicas.clear()
        self._replica_index = 0
        return previous

    @staticmethod
    async def retire_replicas(previous: Sequence[Database]) -> None:
        await asyncio.gather(
            *(replica.disconnect() for replica in previous if replica.is_connected),
            return_exceptions=True,
        )

    def transaction(self, *args, **kwargs):
        primary_transaction = write_transaction(self._primary, *args, **kwargs)
        return _ReplicaAwareTransaction(self, primary_transaction)

    async def execute(self, query, values=None, *, force_primary: bool = False):
        return await self._run_primary("execute", query, values)

    async def execute_many(self, query, values, *, force_primary: bool = False):
        return await self._run_primary("execute_many", query, values)

    async def _run_primary(self, operation: str, *args):
        if not metrics.enabled:
            return await getattr(self._primary, operation)(*args)
        return await self._run_observed(
            self._primary,
            operation,
            "primary",
            *args,
        )

    @staticmethod
    async def _run_observed(target, operation: str, target_name: str, *args):
        started_at = time.perf_counter()
        outcome = "success"
        try:
            return await getattr(target, operation)(*args)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "error"
            raise
        finally:
            metrics.observe_database(
                operation,
                target_name,
                outcome,
                time.perf_counter() - started_at,
            )

    async def fetch_all(self, query, values=None, *, force_primary: bool = False):
        return await self._run_read("fetch_all", force_primary, query, values)

    async def fetch_one(self, query, values=None, *, force_primary: bool = False):
        return await self._run_read("fetch_one", force_primary, query, values)

    async def fetch_val(
        self, query, values=None, column: int = 0, *, force_primary: bool = False
    ):
        return await self._run_read("fetch_val", force_primary, query, values, column)

    def _should_use_primary(self, explicit_force: bool):
        if explicit_force or self._force_primary_context.get():
            return True

        if not self._available_replicas():
            return True

        return self._transaction_depth.get() > 0

    def _next_replica(self):
        replicas = self._available_replicas()
        replica = replicas[self._replica_index % len(replicas)]
        self._replica_index = (self._replica_index + 1) % len(replicas)
        return replica

    def _available_replicas(self):
        now = time.monotonic()
        return [
            replica
            for replica in self._active_replicas
            if self._replica_retry_after.get(replica, 0) <= now
        ]

    def _deactivate_replica(self, replica):
        self._replica_retry_after[replica] = (
            time.monotonic() + _REPLICA_RETRY_DELAY_SECONDS
        )

    def _replica_slot(self, replica: Database) -> int:
        return self._configured_replicas.index(replica) + 1

    def _mark_replica_unavailable(
        self,
        replica: Database,
        exc: BaseException,
    ) -> None:
        if replica in self._unavailable_replicas:
            return
        self._unavailable_replicas.add(replica)
        log.warning(
            "database.replica.unavailable",
            "Database replica became unavailable",
            replica_slot=self._replica_slot(replica),
            error_code="replica_query_failed",
            exc=exc,
        )

    def _mark_replica_recovered(self, replica: Database) -> None:
        if replica not in self._unavailable_replicas:
            return
        self._unavailable_replicas.remove(replica)
        log.info(
            "database.replica.recovered",
            "Database replica recovered",
            replica_slot=self._replica_slot(replica),
        )

    async def _run_read(self, method_name: str, force_primary: bool, *args):
        if (
            not force_primary
            and not self._force_primary_context.get()
            and self._transaction_depth.get() == 0
        ):
            await self._restore_disconnected_replicas()
        target = (
            self._primary
            if self._should_use_primary(force_primary)
            else self._next_replica()
        )

        target_name = "primary" if target is self._primary else "replica"
        try:
            result = await (
                self._run_observed(target, method_name, target_name, *args)
                if metrics.enabled
                else getattr(target, method_name)(*args)
            )
            if target is not self._primary:
                self._mark_replica_recovered(target)
            return result
        except asyncio.CancelledError:  # pragma: no cover - propagate cancellations
            raise
        except Exception as exc:
            if target is not self._primary and self._primary.is_connected:
                self._deactivate_replica(target)
                self._mark_replica_unavailable(target, exc)
                metrics.observe_database_fallback(method_name)
                return await (
                    self._run_observed(
                        self._primary,
                        method_name,
                        "primary",
                        *args,
                    )
                    if metrics.enabled
                    else getattr(self._primary, method_name)(*args)
                )
            raise

    async def _restore_disconnected_replicas(self) -> None:
        now = time.monotonic()
        candidates = [
            replica
            for replica in self._configured_replicas
            if replica not in self._active_replicas
            and self._replica_retry_after.get(replica, 0) <= now
        ]
        if not candidates:
            return
        async with self._replica_reconnect_lock:
            for replica in candidates:
                if replica in self._active_replicas:
                    continue
                try:
                    await replica.connect()
                except Exception:
                    self._deactivate_replica(replica)
                    continue
                self._active_replicas.append(replica)
                self._replica_retry_after.pop(replica, None)
                self._mark_replica_recovered(replica)

    @contextmanager
    def force_primary(self):
        token = self._force_primary_context.set(True)
        try:
            yield self
        finally:
            self._force_primary_context.reset(token)

    async def _resolve_and_recreate(self, db_instance: Database) -> Database:
        """
        Resolves the hostname of the given Database instance to an IP address.
        If resolution succeeds and changes the host, returns a NEW Database instance.
        Otherwise, returns the original instance.
        """
        try:
            original_url_str = str(db_instance.url)
            url_obj = make_url(original_url_str)

            if not url_obj.host:
                return db_instance
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    url_obj.host,
                    url_obj.port or 5432,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                ),
                timeout=_IPV4_RESOLUTION_TIMEOUT_SECONDS,
            )
            if not addresses:
                raise socket.gaierror("database hostname has no IPv4 address")
            resolved_ip = addresses[0][4][0]

            if resolved_ip == url_obj.host:
                return db_instance  # No change
            # Reconstruct URL with IP
            url_obj = url_obj.set(host=resolved_ip)
            new_url_str = url_obj.render_as_string(hide_password=False)

            return Database(new_url_str)
        except Exception:
            return db_instance

    def __getattr__(self, item):
        return getattr(self._primary, item)


class _ReplicaAwareTransaction:
    def __init__(self, router: ReplicaAwareDatabase, transaction_cm):
        self._router = router
        self._transaction_cm = transaction_cm
        self._token = None

    async def __aenter__(self):
        transaction = await self._transaction_cm.__aenter__()
        current_depth = self._router._transaction_depth.get()
        self._token = self._router._transaction_depth.set(current_depth + 1)
        return transaction

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._transaction_cm.__aexit__(exc_type, exc, tb)
        finally:
            if self._token is not None:
                self._router._transaction_depth.reset(self._token)
