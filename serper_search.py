"""
Serper.dev — Google Search via REST API. Drop-in alternative to brave_search.

WHY (May 2026):
- Brave's 2k/month free tier was exhausted mid-month → HTTP 402.
- Google Custom Search Engine is CLOSED to new customers since 2025.
- DDG fallback's LinkedIn index is too weak for FR SMB gérants.
- Serper.dev delivers REAL Google results: 2,500 free queries one-shot
  (no card required), then $0.30/1k. The cheapest reliable SERP API in 2026.

Setup (1 minute):
  1. Sign up with Google at https://serper.dev (no card)
  2. Dashboard → copy API key
  3. Add to .env: SERPER_API_KEY=...

Public API matches brave_search.BraveSearch.text() for drop-in use.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx

API_URL = "https://google.serper.dev/search"
DEFAULT_TIMEOUT = 10.0

from http_safe import Throttle  # noqa: E402

# Serper has no documented per-second rate limit; pad lightly to be polite.
# Thread-safe so parallel workers don't burst.
_THROTTLE = Throttle(min_interval_s=0.2)


def _throttle() -> None:
    _THROTTLE.acquire()


class SerperSearch:
    """Thin Serper.dev wrapper. Drop-in for BraveSearch / DDG."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("SERPER_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "SERPER_API_KEY is not set. "
                "Sign up free (2,500 queries) at https://serper.dev"
            )
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
            },
        )

    def text(self, query: str, max_results: int = 10) -> list[dict]:
        """Run a Google search via Serper. Returns Brave-compatible results.

        Each result dict: {href, url, title, body}.
        """
        _throttle()
        try:
            resp = self._client.post(
                API_URL,
                json={
                    "q": query,
                    "num": min(max(max_results, 1), 20),
                    "gl": "fr",      # geo
                    "hl": "fr",      # interface lang
                },
            )
            # 429 → silently bail (quota exhausted)
            if resp.status_code == 429:
                return []
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        # Track quota consumption — one Serper call = 1 credit
        try:
            from quotas import mark_used
            mark_used("serper")
        except Exception:
            pass

        out: list[dict] = []
        for item in data.get("organic") or []:
            url = item.get("link") or ""
            if not url:
                continue
            out.append(
                {
                    "href": url,
                    "url": url,
                    "title": item.get("title") or "",
                    "body": item.get("snippet") or "",
                }
            )
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SerperSearch":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def have_serper_key() -> bool:
    return bool(os.environ.get("SERPER_API_KEY"))


def serper_search(query: str, max_results: int = 10) -> list[dict]:
    """One-shot convenience: returns [] on any failure, never raises."""
    if not have_serper_key():
        return []
    try:
        with SerperSearch() as s:
            return s.text(query, max_results=max_results)
    except Exception:
        return []


def _cli() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Test Serper.dev search.")
    p.add_argument("query", help="Search query")
    p.add_argument("--count", type=int, default=5)
    args = p.parse_args()

    if not have_serper_key():
        print("SERPER_API_KEY not set — sign up at https://serper.dev (2500 free queries)")
        return
    results = serper_search(args.query, max_results=args.count)
    print(f"# Serper returned {len(results)} results")
    for r in results:
        print(json.dumps(
            {"url": r["href"], "title": r["title"], "body": r["body"][:120]},
            ensure_ascii=False,
        ))


if __name__ == "__main__":
    _cli()
