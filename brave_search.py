"""
Brave Search API wrapper — drop-in replacement for ddgs.DDGS.

Why: DDG returns non-deterministic results, throttles aggressively, and
sometimes returns zero results for the same query. Brave is faster,
deterministic, and has a generous 2 000 req/month free tier.

The wrapper exposes a `BraveSearch` class with a `.text(query, max_results)`
method that mimics `DDGS.text()` — returns a list of dicts with `href`,
`title`, and `body` keys, so existing callers don't need to change shape.

Set env var `BRAVE_SEARCH_API_KEY` (`BSA...`). If missing, callers can fall
back to DDG via the `search_text()` convenience function in `search_backend`.
"""
from __future__ import annotations

import os
import time
from typing import Iterable, Iterator, Optional

import httpx

API_URL = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_TIMEOUT = 10.0

from http_safe import Throttle  # noqa: E402

# Free tier = 1 req/sec. Pad slightly to stay safely under. Thread-safe so
# parallel workers in pipeline.enrich_companies_parallel don't double up.
_THROTTLE = Throttle(min_interval_s=1.05)


def _throttle() -> None:
    _THROTTLE.acquire()


class BraveSearch:
    """Mimics ddgs.DDGS for the bits the prospect-agent uses."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "BRAVE_SEARCH_API_KEY is not set. "
                "Get one for free at https://api-dashboard.search.brave.com/"
            )
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )

    def text(self, query: str, max_results: int = 10) -> list[dict]:
        """Return a list of result dicts compatible with DDGS.text().

        Each dict has keys: href, url (alias), title, body.
        """
        _throttle()
        try:
            resp = self._client.get(
                API_URL,
                params={
                    "q": query,
                    "count": min(max(max_results, 1), 20),
                    "country": "FR",
                    "search_lang": "fr",
                    "safesearch": "moderate",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        try:
            from quotas import mark_used
            mark_used("brave_search")
        except Exception:
            pass

        out: list[dict] = []
        for item in (data.get("web") or {}).get("results") or []:
            url = item.get("url") or ""
            if not url:
                continue
            out.append(
                {
                    "href": url,
                    "url": url,
                    "title": item.get("title") or "",
                    "body": item.get("description") or "",
                }
            )
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BraveSearch":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Backend selector (used by website_finder / social_finder)
# ---------------------------------------------------------------------------

def have_brave_key() -> bool:
    return bool(os.environ.get("BRAVE_SEARCH_API_KEY"))


def search_text(query: str, max_results: int = 10) -> list[dict]:
    """Run a search. Backend priority: Serper > Google CSE > Brave > DDG.

    Why this order (May 2026):
      - Serper.dev gives REAL Google results: 2,500 free queries one-shot,
        then $0.30/1k. Best LinkedIn / Insta discovery in 2026.
      - Google CSE is CLOSED to new customers since 2025 — only useful for
        users who already had a key. Kept as a fallback for those.
      - Brave is solid for general web search, 2k/month free, but the monthly
        quota is easy to exhaust.
      - DDG is the last resort: no API key, but a weak index for social.

    Each backend is tried in order; if a backend returns empty (could be
    "no results" OR "quota exhausted"), we fall through to the next.
    Returns [] on any total failure, never raises.
    """
    # 1. Serper.dev (best for LinkedIn; default in 2026)
    try:
        from serper_search import serper_search, have_serper_key
        if have_serper_key():
            results = serper_search(query, max_results=max_results)
            if results:
                return results
    except Exception:
        pass

    # 2. Google CSE (only for legacy users with a key)
    try:
        from google_cse import google_cse_search, have_google_cse_key
        if have_google_cse_key():
            results = google_cse_search(query, max_results=max_results)
            if results:
                return results
    except Exception:
        pass

    # 3. Brave
    if have_brave_key():
        try:
            with BraveSearch() as b:
                results = b.text(query, max_results=max_results)
                if results:
                    return results
        except Exception:
            pass

    # 4. DDG fallback
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:
        return []
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results)) or []
    except Exception:
        return []


def _cli() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Test Brave Search API.")
    parser.add_argument("query", help="The search query")
    parser.add_argument("--count", type=int, default=5, help="Max results (default 5)")
    args = parser.parse_args()

    results = search_text(args.query, max_results=args.count)
    backend = "brave" if have_brave_key() else "ddg"
    print(f"# Backend: {backend}, {len(results)} results")
    for r in results:
        print(json.dumps({"url": r.get("href"), "title": r.get("title"), "body": (r.get("body") or "")[:100]},
                          ensure_ascii=False))


if __name__ == "__main__":
    _cli()
