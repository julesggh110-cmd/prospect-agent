"""
Email finder — generate likely email addresses for a person at a company and
verify them as far as we can without sending mail.

Two stages:
1. Pattern generation — produce ~10 candidate emails from (first, last, domain).
2. Verification:
   a. SYNTAX check via email-validator
   b. MX lookup (domain has mail servers?)
   c. SMTP RCPT TO probe — connect to the MX, do EHLO/MAIL FROM/RCPT TO and
      read the response. We never send a body. Many mail servers return
      250/251 for valid recipients, 550 for unknown. Some return 250 for
      everything (catch-all) — we flag those as unverifiable.

This is the most fragile part of the stack: anti-spam systems can blackhole
our probes, give false positives, or rate-limit us. Use confidence scoring
to communicate uncertainty to the caller.
"""
from __future__ import annotations

import smtplib
import socket
import unicodedata
from dataclasses import dataclass
from typing import Optional

import dns.exception
import dns.resolver
from email_validator import EmailNotValidError, validate_email

PROBE_SENDER = "verify@example.com"
DNS_TIMEOUT_S = 5.0
SMTP_TIMEOUT_S = 8.0


# ---------------------------------------------------------------------------
# Pattern generation
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _slug(s: str) -> str:
    """Lowercase ASCII slug suitable for an email local-part."""
    s = _strip_accents(s).lower()
    s = "".join(c if c.isalnum() else "" for c in s)
    return s


def generate_email_patterns(first: str, last: str, domain: str) -> list[str]:
    """Produce ordered, deduplicated likely email candidates for one person."""
    f = _slug(first)
    l = _slug(last)
    d = domain.lower().lstrip("@")
    if not f or not l or not d:
        return []

    fi, li = f[0], l[0]
    patterns = [
        f"{f}.{l}@{d}",     # prenom.nom@
        f"{f}{l}@{d}",      # prenomnom@
        f"{fi}.{l}@{d}",    # p.nom@
        f"{fi}{l}@{d}",     # pnom@
        f"{f}-{l}@{d}",     # prenom-nom@
        f"{f}_{l}@{d}",     # prenom_nom@
        f"{l}.{f}@{d}",     # nom.prenom@
        f"{l}{fi}@{d}",     # nomp@
        f"{f}@{d}",         # prenom@
        f"{l}@{d}",         # nom@
        f"{fi}{li}@{d}",    # pn@
    ]
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class EmailCheck:
    email: str
    syntax_ok: bool = False
    mx_records: list[str] | None = None
    smtp_code: Optional[int] = None
    smtp_message: Optional[str] = None
    catch_all: Optional[bool] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        """Human-readable verdict."""
        if not self.syntax_ok:
            return "invalid_syntax"
        if not self.mx_records:
            return "no_mx"
        if self.smtp_code is None:
            return "smtp_unreachable"
        if self.catch_all:
            return "catch_all"
        if 200 <= self.smtp_code < 300:
            return "deliverable"
        if self.smtp_code in (550, 551, 553):
            return "not_deliverable"
        return f"smtp_{self.smtp_code}"

    @property
    def confidence(self) -> int:
        """0-100 confidence that this email reaches a real person."""
        s = self.status
        return {
            "deliverable": 85,
            "catch_all": 35,
            "smtp_unreachable": 25,
            "no_mx": 0,
            "invalid_syntax": 0,
            "not_deliverable": 0,
        }.get(s, 15)


def _lookup_mx(domain: str) -> list[str]:
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = DNS_TIMEOUT_S
        answers = resolver.resolve(domain, "MX")
        records = sorted(
            (r for r in answers),
            key=lambda r: getattr(r, "preference", 100),
        )
        return [str(r.exchange).rstrip(".") for r in records]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
            dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    except Exception:
        return []


# Port-25-egress probe — many cloud VPS (AWS, GCP, OVH, etc.) block outbound
# port 25 by default. Without this, every SMTP probe silently times out and
# we return zero verified emails — the user thinks the agent is broken.
# We test once per process and remember the result.
_SMTP_AVAILABLE: Optional[bool] = None


def _is_smtp_outbound_open() -> bool:
    """One-shot check: can we open a TCP connection to a well-known SMTP MX?

    We test against gmail-smtp-in.l.google.com:25 with a 3s timeout. If it
    fails, we're on a host where port 25 is blocked → mark all subsequent
    SMTP probes as unreachable so the pipeline can fall back to pattern-only
    detection or Dropcontact's verified emails.
    """
    global _SMTP_AVAILABLE
    if _SMTP_AVAILABLE is not None:
        return _SMTP_AVAILABLE
    try:
        with socket.create_connection(
            ("gmail-smtp-in.l.google.com", 25), timeout=3.0
        ) as s:
            # Drain a banner to be sure the conversation works
            try:
                s.recv(64)
            except (socket.timeout, OSError):
                pass
        _SMTP_AVAILABLE = True
    except (socket.timeout, ConnectionError, OSError):
        _SMTP_AVAILABLE = False
    return _SMTP_AVAILABLE


def smtp_outbound_available() -> bool:
    """Public accessor — lets callers (pipeline, run_campaign) print a warning."""
    return _is_smtp_outbound_open()


def _smtp_probe(mx: str, sender: str, recipient: str) -> tuple[Optional[int], Optional[str]]:
    """Attempt EHLO/MAIL FROM/RCPT TO against `mx`. Return (code, message).

    Short-circuits with (None, "port-25-blocked") if a startup check has
    proven the host can't open outbound 25. Avoids wasted timeouts at scale.
    """
    if not _is_smtp_outbound_open():
        return None, "port-25-blocked"
    try:
        with smtplib.SMTP(timeout=SMTP_TIMEOUT_S) as s:
            s.connect(mx, 25)
            s.ehlo_or_helo_if_needed()
            try:
                s.mail(sender)
            except smtplib.SMTPSenderRefused as e:
                return e.smtp_code, e.smtp_error.decode("utf-8", "ignore")
            code, msg = s.rcpt(recipient)
            return code, msg.decode("utf-8", "ignore") if isinstance(msg, bytes) else str(msg)
    except (socket.timeout, smtplib.SMTPException, ConnectionError, OSError):
        return None, None


def _catch_all_check(mx: str, domain: str) -> Optional[bool]:
    """Probe a guaranteed-nonexistent address. If accepted, the domain is catch-all."""
    fake = f"definitely-not-a-real-user-zzz123abc@{domain}"
    code, _ = _smtp_probe(mx, PROBE_SENDER, fake)
    if code is None:
        return None
    return 200 <= code < 300


def verify_email(address: str) -> EmailCheck:
    """Full verification of one email address. Best-effort; tolerates failures."""
    check = EmailCheck(email=address)

    # 1. Syntax
    try:
        v = validate_email(address, check_deliverability=False)
        check.syntax_ok = True
        domain = v.domain
    except EmailNotValidError as e:
        check.error = str(e)
        return check

    # 2. MX
    mx = _lookup_mx(domain)
    check.mx_records = mx
    if not mx:
        return check

    # 3. SMTP probe
    code, msg = _smtp_probe(mx[0], PROBE_SENDER, address)
    check.smtp_code = code
    check.smtp_message = msg

    # 4. Catch-all check (only if the address looked accepted)
    if code is not None and 200 <= code < 300:
        check.catch_all = _catch_all_check(mx[0], domain)

    return check


def find_best_email(first: str, last: str, domain: str) -> tuple[Optional[EmailCheck], list[EmailCheck]]:
    """Try each pattern, smart-exit early to save SMTP probes.

    Early-exit rules:
    - First probe `smtp_unreachable` (firewall blocks port 25) → STOP after 2 retries on different patterns;
      every pattern hits the same MX and will fail the same way.
    - First probe `no_mx` → STOP immediately, the domain has no mail server.
    - First `deliverable` → STOP immediately (already implemented).
    - First catch-all → STOP after recording it (return the most-likely pattern with
      low confidence; trying more patterns is pointless on catch-all).
    - After 4 consecutive `not_deliverable` (550) → STOP; server is responsive
      but we're not guessing right, more tries are unlikely to find the real one.
    """
    candidates = generate_email_patterns(first, last, domain)
    if not candidates:
        return None, []

    # If outbound SMTP is blocked, we CAN'T verify anything. Return the
    # most-likely pattern with a low-confidence "pattern-guess" marker so the
    # pipeline can show it to the user with a clear caveat instead of leaving
    # the email field completely empty.
    if not _is_smtp_outbound_open():
        pattern_only = EmailCheck(
            email=candidates[0],
            syntax_ok=True,
            mx_records=[],
            smtp_code=None,
            smtp_message="port-25-blocked: not verified",
            catch_all=None,
        )
        return pattern_only, [pattern_only]

    checks: list[EmailCheck] = []
    best: Optional[EmailCheck] = None
    consecutive_550 = 0

    for i, addr in enumerate(candidates):
        c = verify_email(addr)
        checks.append(c)

        # Update best-so-far
        if best is None or c.confidence > best.confidence:
            best = c

        status = c.status
        if status == "deliverable":
            return c, checks  # win, stop
        if status == "catch_all":
            return c, checks  # all addresses on this domain will succeed; the pattern is a guess
        if status == "no_mx":
            return c, checks  # domain has no mail server, all patterns will fail
        if status == "smtp_unreachable" and i >= 1:
            # First probe couldn't reach the SMTP server. Likely firewall blocking
            # port 25 from cloud IPs. Other patterns will hit the same wall.
            break
        if status == "not_deliverable":
            consecutive_550 += 1
            if consecutive_550 >= 4:
                break
        else:
            consecutive_550 = 0

    return best, checks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Generate + verify email patterns for a person.")
    parser.add_argument("first", help="First name")
    parser.add_argument("last", help="Last name")
    parser.add_argument("domain", help="Email domain (e.g. example.com)")
    parser.add_argument("--patterns-only", action="store_true",
                        help="Skip SMTP verification, just print candidates")
    args = parser.parse_args()

    patterns = generate_email_patterns(args.first, args.last, args.domain)
    if args.patterns_only:
        print("\n".join(patterns))
        return

    best, checks = find_best_email(args.first, args.last, args.domain)
    out = {
        "candidates": patterns,
        "best": {
            "email": best.email if best else None,
            "status": best.status if best else None,
            "confidence": best.confidence if best else None,
        },
        "checks": [
            {
                "email": c.email,
                "status": c.status,
                "confidence": c.confidence,
                "smtp_code": c.smtp_code,
            }
            for c in checks
        ],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _cli()
