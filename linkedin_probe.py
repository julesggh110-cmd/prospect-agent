"""
LinkedIn direct probing — find profile URLs by guessing slug patterns.

LinkedIn profile URLs follow a predictable pattern:
    https://www.linkedin.com/in/<slug>

Where <slug> is most often one of:
    - firstname-lastname
    - firstname-lastname-<6-8 hex chars>      (when the basic slug was taken)
    - firstnamelastname
    - firstname.lastname
    - lastname-firstname (rare)
    - firstname-middlename-lastname

Instead of searching (which DDG does poorly and even Brave/Google miss for
non-indexed profiles), we PROBE these patterns directly via HEAD request.

LinkedIn returns:
    200 OK     → profile exists and is public
    302 Found  → redirected to login (still means profile exists)
    404 Not Found → no such slug

We accept 200 + 999 (LinkedIn's anti-bot challenge that still indicates a
real profile) as "profile exists".

Important limitations:
    - LinkedIn aggressively rate-limits unauthenticated requests
    - We use a 2-second delay between probes
    - We cap attempts to ~6 per person to stay under the radar
    - We can't read the profile content (would need login + violates ToS) —
      we only get the URL, but that's all the user needs for outreach

Public API:
    probe_linkedin_url(first, last) -> Optional[str]
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Iterable, Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 6.0

_LAST_AT = 0.0
_MIN_INTERVAL = 2.0  # LinkedIn is unforgiving; throttle hard.


def _throttle() -> None:
    global _LAST_AT
    now = time.monotonic()
    delta = now - _LAST_AT
    if delta < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - delta)
    _LAST_AT = time.monotonic()


def _slug(s: str) -> str:
    """ASCII slug. 'Hervé' → 'herve', 'O\\'Brien' → 'obrien'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def _slug_with_dash(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _slug_variants(first: str, last: str) -> list[str]:
    """Generate the most plausible LinkedIn slugs for (first, last).

    Ordered by probability (most likely first). We don't try the 6-hex-suffix
    variants because there are 16^6 = 16M possibilities — search would catch
    those better.
    """
    f = _slug(first)
    l = _slug(last)
    fd = _slug_with_dash(first)
    ld = _slug_with_dash(last)
    if not f or not l:
        return []

    variants = [
        f"{fd}-{ld}",          # most common
        f"{f}{l}",             # contracted
        f"{f}-{l}",
        f"{f}.{l}",
        f"{ld}-{fd}",          # last-first inversion (rare)
        f"{l}-{f}",
    ]
    # Dedupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen and 4 <= len(v) <= 80:
            seen.add(v)
            out.append(v)
    return out


def _probe(slug: str) -> Optional[str]:
    """HEAD-probe one LinkedIn slug. Returns the URL if it looks real, None otherwise.

    LinkedIn's logged-out behaviour:
      - public profile  → 200 OK with HTML
      - any profile     → 302 to /login or /authwall (still confirms existence)
      - non-existent    → 404 Not Found
      - rate-limited    → 429 Too Many Requests
      - anti-bot wall   → 999 (their custom anti-scraping code)
    """
    url = f"https://www.linkedin.com/in/{slug}"
    try:
        _throttle()
        with httpx.Client(
            timeout=TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            },
            follow_redirects=False,
            verify=False,
        ) as c:
            r = c.head(url)
            # 200 = public profile, 302 = redirect to login (still exists)
            if r.status_code in (200, 302, 303):
                # 302 might redirect to /404 — check the Location header
                loc = (r.headers.get("location") or "").lower()
                if "/404" in loc or "uas/login" in loc and slug not in loc:
                    return None
                return url
            # 429/999 means LinkedIn is throttling us — we can't reliably tell,
            # so we treat as "unknown". Return None (caller may retry with a
            # different slug; if all fail we give up).
            if r.status_code == 999:
                # 999 = blocked; best to bail out completely
                return None
    except Exception:
        return None
    return None


def probe_linkedin_url(first: str, last: str, *, max_attempts: int = 4) -> Optional[str]:
    """Try a handful of plausible LinkedIn slugs for (first, last).

    Returns the first URL that looks like a real profile, or None. Caps at
    `max_attempts` probes to limit rate-limit exposure and time cost.
    """
    if not first or not last:
        return None
    variants = _slug_variants(first, last)[:max_attempts]
    for slug in variants:
        url = _probe(slug)
        if url:
            return url
    return None


def _cli() -> None:
    import argparse
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser(description="Probe LinkedIn for a person's profile.")
    p.add_argument("first")
    p.add_argument("last")
    p.add_argument("--max", type=int, default=4, help="Max probes (default 4)")
    args = p.parse_args()
    url = probe_linkedin_url(args.first, args.last, max_attempts=args.max)
    print(url or "(no profile found via probe)")


if __name__ == "__main__":
    _cli()
