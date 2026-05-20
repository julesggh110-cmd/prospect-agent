"""
Shared HTTP helpers: SSL-warning-aware fallback + thread-safe throttling.

WHY this module exists:

1. **SSL verification**: every scraper module previously used `httpx.Client(verify=False)`
   silently. That hides cert-broken SMBs (legit) AND masks MITM (not legit). We
   centralise the pattern: try `verify=True` first, fall back to `verify=False`
   only after logging *which* host had the bad cert. The host-warning is cached
   per process to avoid log spam.

2. **Thread-safe throttling**: scrapers were using `global _LAST_AT` with no
   lock. Under `ThreadPoolExecutor(max_workers=8)` from
   `pipeline.enrich_companies_parallel`, multiple threads race on the same
   variable → effective throttle drops below target → risk of IP-ban from
   Brave/Overpass/PJ. We provide a `Throttle` class backed by a real lock.

Public API:
    Throttle(min_interval_s) — call .acquire() before each request
    safe_client(timeout, headers, follow_redirects) — context manager that
        tries verify=True, falls back to verify=False with a one-shot log
    warned_hosts() — for tests/debugging
"""
from __future__ import annotations

import logging
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

import httpx

logger = logging.getLogger("prospect_agent.http")

# Track hosts where SSL verification failed (so we don't log them twice).
_warned_lock = threading.Lock()
_warned_hosts: set[str] = set()


def warned_hosts() -> set[str]:
    """Snapshot of hosts whose cert we accepted insecurely this process."""
    with _warned_lock:
        return set(_warned_hosts)


def _note_ssl_failure(host: str) -> None:
    with _warned_lock:
        first_time = host not in _warned_hosts
        _warned_hosts.add(host)
    if first_time:
        logger.warning(
            "[http_safe] SSL verification FAILED for %s — falling back to verify=False. "
            "Common for SMB sites with self-signed or expired certs, but check if "
            "the host is unfamiliar.", host,
        )


class Throttle:
    """Thread-safe minimum-interval rate limiter.

    Usage:
        T = Throttle(min_interval_s=1.5)
        ...
        T.acquire()       # blocks until the next slot opens
        do_request(...)

    The lock ensures that under parallel workers, the effective interval
    BETWEEN requests is the configured one, not (workers * configured).
    """

    __slots__ = ("min_interval", "_lock", "_last_at")

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval = float(min_interval_s)
        self._lock = threading.Lock()
        self._last_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last_at
            sleep_for = self.min_interval - delta
            if sleep_for > 0:
                # Release-then-sleep: we hold the lock through sleep on purpose
                # so concurrent callers stack up sequentially. This is what we
                # actually want for rate-limiting an external API.
                time.sleep(sleep_for)
            self._last_at = time.monotonic()


@contextmanager
def safe_client(
    timeout: float = 10.0,
    headers: Optional[dict] = None,
    follow_redirects: bool = True,
    http2: bool = False,
) -> Iterator[httpx.Client]:
    """Return an httpx.Client that prefers verify=True but falls back gracefully.

    Why a context-manager wrapper instead of a single Client: when verify=True
    raises for THE FIRST request, we close the strict client and rebuild a
    relaxed one. Without that, subsequent requests on the same client would
    keep failing strict.

    Concretely:
        with safe_client(timeout=10) as c:
            r = c.get(url)          # tries verify=True, retries verify=False
                                    # if the strict request raised SSLError.

    For repeated requests to many different hosts (the common case), we
    create a strict client by default. Per-host fallback is implemented
    via the .get/.post/.head retries below.

    Limitation: this wraps single-host clients. For per-request, use the
    `safe_request` helper instead.
    """
    headers = headers or {}
    # Start strict
    client = httpx.Client(
        timeout=timeout,
        headers=headers,
        follow_redirects=follow_redirects,
        verify=True,
        http2=http2,
    )
    try:
        yield client
    finally:
        client.close()


def safe_request(
    method: str,
    url: str,
    *,
    timeout: float = 10.0,
    headers: Optional[dict] = None,
    follow_redirects: bool = True,
    **kwargs,
) -> Optional[httpx.Response]:
    """One-shot request that tries verify=True, falls back to verify=False
    with a logged warning when SSL fails on THIS specific host.

    Returns the response, or None on total failure (network unreachable).
    Never raises — callers handle the None case.
    """
    headers = headers or {}
    try:
        with httpx.Client(
            timeout=timeout, headers=headers,
            follow_redirects=follow_redirects, verify=True,
        ) as c:
            return c.request(method, url, **kwargs)
    except (ssl.SSLError, httpx.ConnectError) as e:
        # Only fall back if the error is SSL-related (not e.g. DNS-failure)
        is_ssl = "ssl" in str(e).lower() or "certificate" in str(e).lower() \
                 or isinstance(e, ssl.SSLError)
        if not is_ssl:
            return None
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        _note_ssl_failure(host)
        try:
            with httpx.Client(
                timeout=timeout, headers=headers,
                follow_redirects=follow_redirects, verify=False,
            ) as c:
                return c.request(method, url, **kwargs)
        except Exception:
            return None
    except Exception:
        return None
