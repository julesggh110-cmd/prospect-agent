"""
Phone classification — separate mobile (06/07) from fixed lines (01-05, 09).

Rule for `person_phone`: ONLY a personal mobile is acceptable. Anything else
(fixed line, switchboard, 0800 freephone, foreign number) belongs in
`company_phone` because it routes through a receptionist or shared inbox,
not to the dirigeant directly.

FR numbering plan (ARCEP):
  06, 07       = mobile (the only personal-direct lines)
  01           = Île-de-France fixed line (Paris area)
  02           = NW France fixed line
  03           = NE France fixed line
  04           = SE France fixed line (Marseille, Lyon, Toulouse area = 04 or 05)
  05           = SW France fixed line (Bordeaux, Toulouse)
  08           = special services (0800 freephone, 081x premium)
  09           = VoIP / nomadic (Free, Bouygues etc. — usually a landline)

Foreign formats (+1 USA, +44 UK, +49 DE etc.) are foreign and useless for FR
prospection — route to company_phone with a warning instead of dropping silently.

Public API:
    normalize_fr_phone(raw)              -> Optional[str]  # "+33 6 12 34 56 78"
    is_french_mobile(phone)              -> bool
    is_french_fixed_line(phone)          -> bool
    classify(raw)                        -> "mobile" | "fixed" | "special" | "foreign" | "invalid"
"""
from __future__ import annotations

import re
from typing import Optional


_DIGITS_RX = re.compile(r"\D")


def _digits(raw: str) -> str:
    return _DIGITS_RX.sub("", raw or "")


def normalize_fr_phone(raw: str) -> Optional[str]:
    """Return a canonical FR phone string '+33 X XX XX XX XX' or None if invalid.

    Accepts inputs like:
      - '06 12 34 56 78'
      - '06.12.34.56.78'
      - '0612345678'
      - '+33 6 12 34 56 78'
      - '+33612345678'
      - '0033612345678'
    """
    if not raw:
        return None
    d = _digits(raw)
    # 00... prefix → strip
    if d.startswith("00"):
        d = d[2:]
    # 33XXXXXXXXX (international, no +)
    if d.startswith("33") and len(d) == 11:
        d = d[2:]
    # 0XXXXXXXXX (national) → strip the leading 0
    elif d.startswith("0") and len(d) == 10:
        d = d[1:]
    # Otherwise must be a 9-digit subscriber number
    if len(d) != 9:
        return None
    if not d[0] in "123456789":
        return None
    # Pretty-format: +33 X XX XX XX XX
    pairs = " ".join(d[i:i+2] for i in range(1, 9, 2))
    return f"+33 {d[0]} {pairs}"


def classify(raw: str) -> str:
    """One of: 'mobile', 'fixed', 'special', 'foreign', 'invalid'.

    Used to route phones to the right field (person_phone vs company_phone).
    """
    if not raw:
        return "invalid"
    d = _digits(raw)
    if not d:
        return "invalid"

    # Foreign country code (anything not +33 / 0033 / starting 0)
    if d.startswith("00") and not d.startswith("0033"):
        return "foreign"
    if d.startswith("1") and len(d) >= 10:   # USA bare 10-digit incl. area
        return "foreign"
    if d.startswith(("44", "49", "34", "39", "1")) and len(d) >= 11:
        return "foreign"

    # Strip 33/0033 to leave the subscriber number
    if d.startswith("0033"):
        d = d[4:]
    if d.startswith("33") and len(d) == 11:
        d = d[2:]
    # 9-digit subscriber number (no leading 0)
    if len(d) == 9 and d[0] in "123456789":
        first = d[0]
    elif len(d) == 10 and d[0] == "0":
        first = d[1]
    else:
        return "invalid"

    if first in ("6", "7"):
        return "mobile"
    if first in ("1", "2", "3", "4", "5", "9"):
        return "fixed"
    if first == "8":
        return "special"
    return "invalid"


def is_french_mobile(phone: str) -> bool:
    """True iff `phone` is a real FR mobile (06/07 / +336 / +337)."""
    return classify(phone) == "mobile"


def is_french_fixed_line(phone: str) -> bool:
    return classify(phone) == "fixed"


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Classify a phone number.")
    p.add_argument("phone")
    args = p.parse_args()
    print(f"raw:        {args.phone}")
    print(f"classify:   {classify(args.phone)}")
    print(f"normalized: {normalize_fr_phone(args.phone)}")
    print(f"is_mobile:  {is_french_mobile(args.phone)}")
    print(f"is_fixed:   {is_french_fixed_line(args.phone)}")
