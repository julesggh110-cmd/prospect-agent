"""
Google Custom Search Engine (CSE) wrapper — drop-in alt to brave_search.

WHY: Brave's 2k/month free tier was exhausted mid-month. DDG's index is weak
for LinkedIn discovery. Google CSE gives us 100 free queries/day (~3000/month),
real Google quality (best in the world for LinkedIn profile discovery), and
costs $5 per 1000 queries past the free tier.

SETUP (5 minutes, user does this once):

1. Create an API key at:
       https://console.cloud.google.com/apis/credentials
   → "Create credentials" → "API key"
   → Restrict to "Custom Search API" once created.

2. Create a Programmable Search Engine at:
       https://programmablesearchengine.google.com/controlpanel/create
   → "What to search": "Search the entire web"
   → Get the "Search engine ID" (cx parameter)

3. Enable Custom Search API at:
       https://console.cloud.google.com/apis/library/customsearch.googleapis.com

4. Add to .env:
       GOOGLE_CSE_API_KEY=AIzaSy...
       GOOGLE_CSE_ID=017643...

The wrapper exposes a `GoogleCSE` class + `search_text()` convenience that
matches the Brave wrapper's signature exactly.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx


API_URL = "https://www.googleapis.com/customsearch/v1"
DEFAULT_TIMEOUT = 10.0
# Generous throttle since the 100/day budget is small — don't burn it on bursts.
_MIN_INTERVAL_S = 0.4
_LAST_AT = 0.0


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - delta)
    _LAST_AT = time.monotonic()


class GoogleCSE:
    """Thin Google Custom Search Engine wrapper, drop-in for BraveSearch."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        cse_id: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOOGLE_CSE_API_KEY")
        self.cse_id = cse_id or os.environ.get("GOOGLE_CSE_ID")
        if not self.api_key or not self.cse_id:
            raise RuntimeError(
                "GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID must both be set. "
                "See google_cse.py docstring for the 5-minute setup."
            )
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    def text(self, query: str, max_results: int = 10) -> list[dict]:
        """Run a query. Returns Brave-compatible result dicts.

        Google CSE caps `num` at 10 per request. For up to 10 we're fine. For
        more, we'd have to paginate (start=11), each page = 1 quota credit.
        """
        _throttle()
        try:
            resp = self._client.get(
                API_URL,
                params={
                    "key": self.api_key,
                    "cx": self.cse_id,
                    "q": query,
                    "num": min(max(max_results, 1), 10),
                    "gl": "fr",         # geo: France
                    "hl": "fr",         # interface lang
                    "safe": "off",
                },
            )
            if resp.status_code == 429:
                # Rate-limited — don't retry, just bail.
                return []
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        out: list[dict] = []
        for item in data.get("items") or []:
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

    def __enter__(self) -> "GoogleCSE":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def have_google_cse_key() -> bool:
    return bool(os.environ.get("GOOGLE_CSE_API_KEY") and os.environ.get("GOOGLE_CSE_ID"))


def google_cse_search(query: str, max_results: int = 10) -> list[dict]:
    """Convenience: one-shot query, never raises (returns [] on any error)."""
    if not have_google_cse_key():
        return []
    try:
        with GoogleCSE() as g:
            return g.text(query, max_results=max_results)
    except Exception:
        return []


def _cli() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Test Google CSE.")
    p.add_argument("query", help="Search query")
    p.add_argument("--count", type=int, default=5)
    args = p.parse_args()

    if not have_google_cse_key():
        print("GOOGLE_CSE_API_KEY / GOOGLE_CSE_ID not set — see google_cse.py docstring.")
        return
    results = google_cse_search(args.query, max_results=args.count)
    print(f"# Google CSE returned {len(results)} results")
    for r in results:
        print(json.dumps(
            {"url": r["href"], "title": r["title"], "body": r["body"][:120]},
            ensure_ascii=False,
        ))


if __name__ == "__main__":
    _cli()
