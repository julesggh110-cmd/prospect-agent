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


def _smtp_probe(mx: str, sender: str, recipient: str) -> tuple[Optional[int], Optional[str]]:
    """Attempt EHLO/MAIL FROM/RCPT TO against `mx`. Return (code, message)."""
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
    """Try each pattern and return (best_match, all_checks)."""
    candidates = generate_email_patterns(first, last, domain)
    if not candidates:
        return None, []

    checks: list[EmailCheck] = []
    best: Optional[EmailCheck] = None
    for addr in candidates:
        c = verify_email(addr)
        checks.append(c)
        if c.status == "deliverable":
            return c, checks  # stop early on first deliverable
        if best is None or c.confidence > best.confidence:
            best = c

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
