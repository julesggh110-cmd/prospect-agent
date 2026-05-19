"""
Triangulation & confidence scoring — the rigor layer.

Every field on a Lead has:
- a value (or None if no source could confirm it)
- a list of source URLs that produced it
- a confidence score 0-100

Scoring rules:
- 2+ independent sources agree on a value          → 90
- 1 source + an active verification step           → 75
  (e.g. email passed SMTP deliverable, URL HTTP-200)
- 1 source, no corroboration                       → 35
- 0 sources                                        → 0 (field stays None)

A lead's "overall_score" is the average of present-field scores.

By default we DROP a lead from the deliverable when:
- decision_maker_name has confidence < 60          (no idea who to contact)
- AND no email above 60                            (no way to reach them)
"""
from __future__ import annotations

from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Field models
# ---------------------------------------------------------------------------

class ScoredField(BaseModel):
    """A single field with its value, sources, and a confidence score."""
    model_config = ConfigDict(extra="allow")

    value: Optional[str] = None
    sources: list[str] = Field(default_factory=list)
    confidence: int = 0
    note: Optional[str] = None  # e.g. "catch_all", "smtp_250", "single_source"

    @classmethod
    def missing(cls) -> "ScoredField":
        return cls(value=None, sources=[], confidence=0)

    @classmethod
    def from_single(cls, value: Optional[str], source: str, *,
                    verified: bool = False, note: Optional[str] = None) -> "ScoredField":
        if not value:
            return cls.missing()
        return cls(
            value=value,
            sources=[source],
            confidence=75 if verified else 35,
            note=note,
        )

    @classmethod
    def from_multiple(cls, value: str, sources: list[str], *,
                      note: Optional[str] = None) -> "ScoredField":
        if not value:
            return cls.missing()
        # 2+ sources -> 90, 3+ -> 95
        conf = 95 if len(sources) >= 3 else (90 if len(sources) >= 2 else 35)
        return cls(value=value, sources=sources, confidence=conf, note=note)


# ---------------------------------------------------------------------------
# Lead model
# ---------------------------------------------------------------------------

class Lead(BaseModel):
    """One verified prospect ready for export."""
    model_config = ConfigDict(extra="allow")

    # Company
    company_name: str
    company_siren: Optional[str] = None
    company_naf: Optional[str] = None
    company_naf_label: Optional[str] = None
    company_city: Optional[str] = None
    company_address: Optional[str] = None
    company_size: Optional[str] = None
    company_website: Optional[str] = None
    company_linkedin: ScoredField = Field(default_factory=ScoredField.missing)
    company_instagram: ScoredField = Field(default_factory=ScoredField.missing)
    company_facebook: Optional[str] = None
    company_phone: ScoredField = Field(default_factory=ScoredField.missing)

    # Decision-maker
    person_name: ScoredField = Field(default_factory=ScoredField.missing)
    person_role: ScoredField = Field(default_factory=ScoredField.missing)
    person_email: ScoredField = Field(default_factory=ScoredField.missing)
    person_phone: ScoredField = Field(default_factory=ScoredField.missing)
    person_linkedin: ScoredField = Field(default_factory=ScoredField.missing)
    person_instagram: ScoredField = Field(default_factory=ScoredField.missing)

    # Pipeline metadata
    overall_score: int = 0
    dropped: bool = False
    drop_reason: Optional[str] = None

    @property
    def all_scored_fields(self) -> dict[str, ScoredField]:
        return {
            "company_linkedin": self.company_linkedin,
            "company_instagram": self.company_instagram,
            "company_phone": self.company_phone,
            "person_name": self.person_name,
            "person_role": self.person_role,
            "person_email": self.person_email,
            "person_phone": self.person_phone,
            "person_linkedin": self.person_linkedin,
            "person_instagram": self.person_instagram,
        }

    def compute_overall(self) -> None:
        """Average the confidence across fields that have a value."""
        scores = [f.confidence for f in self.all_scored_fields.values() if f.value]
        self.overall_score = sum(scores) // len(scores) if scores else 0

    def evaluate(self, *, min_person_conf: int = 60, min_contact_conf: int = 50) -> None:
        """Mark the lead as dropped if it doesn't meet quality thresholds.

        For small businesses (size 00..11, i.e., ≤19 employees), Sirene IS the
        source of truth for the gérant identity and the company phone is the
        contact channel — so we accept company_phone as a person-level channel
        and we relax the contact threshold slightly.
        """
        self.compute_overall()
        SMB_SIZES = {"00", "01", "02", "03", "11", "12", ""}
        is_smb = (self.company_size or "") in SMB_SIZES
        if self.person_name.confidence < min_person_conf:
            self.dropped = True
            self.drop_reason = (
                f"person_name confidence {self.person_name.confidence} "
                f"< {min_person_conf} (no reliable decision-maker)"
            )
            return
        # For SMBs, the company phone IS the way to reach the gérant.
        candidates = [
            self.person_email.confidence,
            self.person_phone.confidence,
            self.person_linkedin.confidence,
        ]
        if is_smb:
            candidates.append(self.company_phone.confidence)
        best_contact = max(candidates)
        if best_contact < min_contact_conf:
            self.dropped = True
            self.drop_reason = (
                f"no contact channel above {min_contact_conf} confidence "
                f"(email={self.person_email.confidence}, "
                f"phone={self.person_phone.confidence}, "
                f"linkedin={self.person_linkedin.confidence})"
            )


# ---------------------------------------------------------------------------
# Cross-source helpers
# ---------------------------------------------------------------------------

def triangulate_url(values: Iterable[tuple[Optional[str], str]],
                    *, note: Optional[str] = None) -> ScoredField:
    """Given a list of (value, source_url) pairs, return a ScoredField.

    Compares values case-insensitively after stripping trailing slashes & query.
    """
    by_norm: dict[str, list[str]] = {}
    by_orig: dict[str, str] = {}
    for value, source in values:
        if not value:
            continue
        norm = value.split("?", 1)[0].rstrip("/").lower()
        by_norm.setdefault(norm, []).append(source)
        by_orig[norm] = value
    if not by_norm:
        return ScoredField.missing()
    best_norm = max(by_norm.keys(), key=lambda k: len(by_norm[k]))
    sources = by_norm[best_norm]
    value = by_orig[best_norm]
    if len(sources) >= 2:
        return ScoredField.from_multiple(value, sources, note=note)
    return ScoredField.from_single(value, sources[0], verified=False, note=note)


def triangulate_phone(values: Iterable[tuple[Optional[str], str]],
                      *, note: Optional[str] = None) -> ScoredField:
    """Normalize phone numbers (digits only) for comparison."""
    import re
    by_norm: dict[str, list[str]] = {}
    by_orig: dict[str, str] = {}
    for value, source in values:
        if not value:
            continue
        norm = re.sub(r"\D", "", value)
        if not norm:
            continue
        # Strip leading country code 33 to normalize 0X vs +33X
        if norm.startswith("33") and len(norm) >= 11:
            norm = "0" + norm[2:]
        by_norm.setdefault(norm, []).append(source)
        by_orig[norm] = value
    if not by_norm:
        return ScoredField.missing()
    best = max(by_norm.keys(), key=lambda k: len(by_norm[k]))
    sources = by_norm[best]
    value = by_orig[best]
    if len(sources) >= 2:
        return ScoredField.from_multiple(value, sources, note=note)
    return ScoredField.from_single(value, sources[0], verified=False, note=note)
