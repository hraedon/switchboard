"""Runtime provider lifecycle: add, remove, replace — with honest draining.

Plan 020 WI-2. Providers were boot-time-only state; the GUI config work
needs them to change while the proxy serves. The invariants that make that
safe live here, not in callers:

- **The provider map is copy-on-swap, never mutated in place.** Readers
  (admission, status, metrics) grab ``manager.providers`` and work on that
  snapshot; a request routed before a removal finishes streaming against
  the context it started with. In-place mutation would hand concurrent
  iterators a changing dict; an atomic reference swap hands them a stale
  but consistent one, which is exactly right for in-flight work.
- **Deregister first, drain second.** Removal swaps the map before touching
  the context, so no *new* snapshot can admit to it. Requests that
  snapshotted earlier may still hold or acquire permits; the gate is then
  resized to 0 (never revokes in-flight permits — see gate.py) so queued
  waiters stop being granted, and the drain waits for ``gate.held`` to
  reach zero before closing anything.
- **Close order: drain → reconcile.stop() → truth_source.close() →
  http_client.aclose().** Closing the httpx client is the destructive
  step — it kills live streams — so it strictly follows the drain. The
  reconcile loop stays up during the drain on purpose: its recording
  methods are what in-flight requests call as they finish.
- **Drains are bounded.** A stream that never ends must not wedge a
  removal; after ``drain_timeout`` the close proceeds and the affected
  request count is logged loudly. This mirrors the lifespan shutdown
  contract in proxy.py.

The manager is asyncio-single-threaded like everything else in the shell:
no locks, and the atomic swap is atomic by virtue of never awaiting between
building the new map and assigning it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from switchboard.providers import ProviderContext

log = logging.getLogger("switchboard.provider_manager")

_DRAIN_POLL_INTERVAL = 0.1


class ProviderManager:
    """Owns the live provider map and the lifecycle of its contexts."""

    def __init__(
        self,
        providers: dict[str, ProviderContext],
        *,
        drain_timeout: float = 25.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers: dict[str, ProviderContext] = dict(providers)
        self._drain_timeout = drain_timeout
        self._clock = clock
        self._drain_tasks: set[asyncio.Task[None]] = set()

    @property
    def providers(self) -> dict[str, ProviderContext]:
        """The current provider map. Treat as an immutable snapshot."""
        return self._providers

    @property
    def draining_count(self) -> int:
        """Contexts currently being drained (observability only)."""
        return len(self._drain_tasks)

    async def add(
        self, name: str, ctx: ProviderContext, *, start: bool = True
    ) -> None:
        """Register a new provider and start its reconcile loop.

        Raises ValueError if the name is taken — replacing an existing
        provider is a different operation (:meth:`replace`) with different
        draining semantics, and conflating them is how a config typo
        silently kills a live provider.
        """
        if name in self._providers:
            raise ValueError(f"provider '{name}' already exists")
        if start:
            await ctx.reconcile.start()
        self._providers = {**self._providers, name: ctx}
        log.info("provider added: %s -> %s", name, ctx.upstream_url)

    async def remove(self, name: str) -> bool:
        """Deregister a provider and drain it in the background.

        Returns False if the name is unknown (double-remove is a no-op:
        the second caller's intent — provider gone — is already true).
        The context disappears from the map immediately; its teardown
        completes asynchronously once in-flight work releases its permits.
        """
        ctx = self._providers.get(name)
        if ctx is None:
            return False
        remaining = dict(self._providers)
        del remaining[name]
        self._providers = remaining
        self._spawn_drain(name, ctx)
        log.info("provider removed: %s (draining %d held)", name, ctx.gate.held)
        return True

    async def replace(self, name: str, new_ctx: ProviderContext) -> None:
        """Atomically swap the context serving ``name``.

        For immutable-field changes (upstream, credential, type): the new
        context starts first, the map swap makes it live in one step, and
        the old context drains in the background under the same rules as
        a removal. There is no instant during which ``name`` is absent.
        """
        old = self._providers.get(name)
        await new_ctx.reconcile.start()
        self._providers = {**self._providers, name: new_ctx}
        if old is not None:
            self._spawn_drain(name, old)
        log.info(
            "provider replaced: %s -> %s (old %s)",
            name,
            new_ctx.upstream_url,
            "draining" if old is not None else "absent",
        )

    def _spawn_drain(self, name: str, ctx: ProviderContext) -> None:
        task = asyncio.create_task(self._drain_and_close(name, ctx))
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def _drain_and_close(self, name: str, ctx: ProviderContext) -> None:
        # Stop new grants without touching in-flight permits: requests that
        # snapshotted the old map may still be queued on this gate, and a
        # zero-capacity gate lets them time out through the normal admission
        # path instead of granting work to a provider that is going away.
        await ctx.gate.resize(0)

        deadline = self._clock() + self._drain_timeout
        while ctx.gate.held > 0:
            remaining = deadline - self._clock()
            if remaining <= 0:
                log.warning(
                    "drain timeout for removed provider %s — closing with "
                    "%d request(s) in-flight",
                    name,
                    ctx.gate.held,
                )
                break
            await asyncio.sleep(min(_DRAIN_POLL_INTERVAL, remaining))

        try:
            await ctx.reconcile.stop()
        except Exception:
            log.warning("reconcile stop failed for %s", name, exc_info=True)
        with contextlib.suppress(Exception):
            await ctx.truth_source.close()
        with contextlib.suppress(Exception):
            await ctx.http_client.aclose()
        log.info("provider drained and closed: %s", name)

    async def shutdown(self) -> None:
        """Await all outstanding drains (proxy lifespan shutdown hook).

        Boot providers are stopped by the lifespan handler itself; this
        settles the teardown of anything removed or replaced at runtime so
        process exit never abandons a half-closed context.
        """
        if not self._drain_tasks:
            return
        await asyncio.gather(*tuple(self._drain_tasks), return_exceptions=True)
