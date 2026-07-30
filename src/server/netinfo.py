"""Non-blocking hostname → address resolution.

The topology view wants to show the IP behind each login node, but DNS on a
restricted network can hang for seconds and the dashboard's HTTP handlers are
synchronous. So resolution happens on a small background pool and callers only
ever read the cache: the first request for a host returns ``None`` and the
answer shows up on the next poll a few seconds later.
"""

from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


class HostResolver:
    """Cached, background-threaded DNS lookups that never block the caller."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        ttl_seconds: int = 3600,
        negative_ttl_seconds: int = 300,
        max_workers: int = 4,
    ):
        self.enabled = enabled
        self.ttl = timedelta(seconds=ttl_seconds)
        self.negative_ttl = timedelta(seconds=negative_ttl_seconds)
        self._cache: Dict[str, Tuple[Optional[str], datetime]] = {}
        self._inflight: set = set()
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = (
            ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dns")
            if enabled
            else None
        )

    def lookup(self, hostname: Optional[str]) -> Optional[str]:
        """Return the cached IPv4 address for a host, resolving in the background.

        Returns None when disabled, when the name does not resolve, or when
        the answer is not cached yet.
        """
        if not self.enabled or not hostname:
            return None
        host = str(hostname).strip().lower()
        if not host or "://" in host or "/" in host:
            return None

        now = datetime.utcnow()
        with self._lock:
            cached = self._cache.get(host)
            if cached:
                value, stamp = cached
                ttl = self.ttl if value else self.negative_ttl
                if now - stamp < ttl:
                    return value
            if host in self._inflight:
                return cached[0] if cached else None
            self._inflight.add(host)

        if self._pool is not None:
            self._pool.submit(self._resolve, host)
        return cached[0] if cached else None

    def _resolve(self, host: str) -> None:
        address: Optional[str] = None
        try:
            # getaddrinfo ignores socket timeouts (it is a blocking libc
            # call), and setting a global default would leak into every
            # other socket in the process — so the pool thread just blocks
            # until the system resolver gives up.
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            if infos:
                address = infos[0][4][0]
        except Exception:
            address = None
        finally:
            with self._lock:
                self._cache[host] = (address, datetime.utcnow())
                self._inflight.discard(host)

    def prime(self, hostnames) -> None:
        """Warm the cache for a batch of hosts (fire and forget)."""
        for host in hostnames or []:
            self.lookup(host)

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
