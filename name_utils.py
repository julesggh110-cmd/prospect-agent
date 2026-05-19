"""
Name utilities — clean up the messy Sirene/Pappers strings so the rest of
the pipeline (email patterns, LinkedIn search, Excel display) gets pretty
inputs.

Sirene returns gémonal stuff like:
- "DIDIER JACQUES EMMANUEL YVON HENRI VILLEMEY"   (5 middle names, all CAPS)
- "MARIE-ANNE PRUNE PRADET"                       (hyphenated first + middle)
- "MOHAMED IBN ABDALLAH"                          (composite name)
- "(M.) SMITH"                                    (with title prefix)

We want, in priority:
- A single (first, last) tuple for email pattern generation
- A pretty `display` form ("Didier Villemey")

Heuristics:
- Strip titles ("M.", "Mme", "Dr.", "Prof.", "(M.)", "(Mme)")
- First token = first name. Last token = last name.
- Particles ("de", "le", "van", "von", "du", "de la") attach to the last name.
- Hyphenated names stay hyphenated ("Marie-Anne").
- Apostrophes preserved ("D'Arcy", "O'Brien").
- Title-case applied (everything was CAPS).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

PARTICLES = {
    "de", "du", "des", "le", "la", "les", "d", "l",
    "van", "von", "der", "den", "ten",
    "el", "al", "ben", "bin", "ibn",
    "san", "sant", "santa",
}

TITLE_PREFIXES = re.compile(
    r"^\s*\(?\s*(m\.?|mr\.?|mme\.?|mrs\.?|miss|ms\.?|"
    r"docteur|dr\.?|professeur|prof\.?|me\.?|maître)\s*\)?\s+",
    re.IGNORECASE,
)


@dataclass
class CleanName:
    first: str             # "Didier"
    last: str              # "Villemey"
    display: str           # "Didier Villemey"
    raw: str               # original input

    def __bool__(self) -> bool:
        return bool(self.first and self.last)


def _title_case_french(word: str) -> str:
    """'JACQUES' → 'Jacques', 'MARIE-ANNE' → 'Marie-Anne', 'd'ARCY' → 'd'Arcy'."""
    if not word:
        return word
    out_parts: list[str] = []
    for piece in re.split(r"([-'])", word):
        if piece in ("-", "'"):
            out_parts.append(piece)
        else:
            out_parts.append(piece.capitalize())
    return "".join(out_parts)


def _strip_titles(name: str) -> str:
    while True:
        new = TITLE_PREFIXES.sub("", name, count=1)
        if new == name:
            return new
        name = new


def clean_person_name(raw: str) -> CleanName:
    """Best-effort split of a raw Sirene-style name into first/last.

    Strategy:
    1. Strip parenthesised junk and titles.
    2. First token = first name (the actual usual first name in French Sirene).
    3. Last meaningful token = last name. Bring back attached particles.
    4. Title-case everything for display.
    """
    if not raw:
        return CleanName(first="", last="", display="", raw=raw or "")

    # Remove (PM) / (Personne Physique) / generic parens
    s = re.sub(r"\([^)]+\)", " ", raw)
    s = _strip_titles(s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return CleanName(first="", last="", display="", raw=raw)

    tokens = s.split(" ")
    if len(tokens) == 1:
        # Single token — best we can do
        only = _title_case_french(tokens[0])
        return CleanName(first=only, last="", display=only, raw=raw)

    # First name = first token
    first = _title_case_french(tokens[0])

    # Walk from the end. Collect everything that's a "last name", which means
    # the last token + any preceding particles.
    idx = len(tokens) - 1
    last_parts: list[str] = [tokens[idx]]
    idx -= 1
    # If the previous token is a particle (e.g., 'de', 'la'), keep walking
    while idx > 0 and tokens[idx].lower().strip(".") in PARTICLES:
        last_parts.insert(0, tokens[idx])
        idx -= 1

    # Build the last name (particle stays lowercase except at sentence start)
    last_pretty_parts: list[str] = []
    for i, p in enumerate(last_parts):
        pl = p.lower().strip(".")
        if pl in PARTICLES and i != 0:
            last_pretty_parts.append(pl)
        elif pl in PARTICLES and i == 0:
            # Particle leading — capitalize ("De La Tour")
            last_pretty_parts.append(_title_case_french(pl))
        else:
            last_pretty_parts.append(_title_case_french(p))
    last = " ".join(last_pretty_parts)

    display = f"{first} {last}".strip()
    return CleanName(first=first, last=last, display=display, raw=raw)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Clean a raw Sirene/Pappers name.")
    p.add_argument("name", nargs="?", help="Raw name string. Omit to run a self-test.")
    args = p.parse_args()
    if args.name:
        c = clean_person_name(args.name)
        print(f"first  : {c.first}")
        print(f"last   : {c.last}")
        print(f"display: {c.display}")
        return
    # Self-test
    samples = [
        "DIDIER JACQUES EMMANUEL YVON HENRI VILLEMEY",
        "MARIE-ANNE PRUNE PRADET",
        "(M.) SMITH",
        "Jean De La Tour",
        "EDOUARD ABEILLE",
        "Paul Chantler",
        "MOHAMED IBN ABDALLAH",
        "CECILE IRÈNE MENARD (MENARD)",
        "VALERIO DUCHINI",
        "Christine D'ARCY",
    ]
    for s in samples:
        c = clean_person_name(s)
        print(f"  {s:60s} → {c.first:15s} | {c.last:30s} ({c.display})")


if __name__ == "__main__":
    _cli()
