"""
decision_trace.py — observabilité structurée par lead.

POURQUOI CE MODULE EXISTE
=========================
Le doc taxonomie agent 2026 §2.6 dit :
  "Sans cette couche [observabilité], un agent est une boîte noire ingouvernable."

Aujourd'hui prospect-agent fait des `print()` partout — utile en dev mais
inauditeable. Pas de moyen de répondre "POURQUOI ce lead a été gardé / jeté".
Le user le ressent : "les leads sont jamais pertinents" — il n'a aucun outil
pour comprendre POURQUOI.

CE QUE FAIT CE MODULE
=====================
Pour chaque lead traité, capture un trace JSON structuré avec :
  - Le lead (nom, SIREN)
  - La séquence des spans (Sirene fetch, web scrape, GMB lookup, LLM call…)
  - Les décisions prises à chaque étape (keep/drop, scores, raisonnements)
  - Le verdict final + path final dans le pipeline

Sauvegardé en JSONL (1 ligne = 1 lead) dans
`output/<campaign_id>-trace.jsonl`. Auditable par humain ou par autre agent.

Compatible LangSmith-like : on peut migrer vers Langfuse/LangSmith plus tard
en transformant ce JSONL en spans OTLP (1 jour de dev).

USAGE
=====
    from decision_trace import LeadTrace, TraceWriter

    with TraceWriter("output/my-campaign-trace.jsonl") as writer:
        for lead in candidates:
            trace = LeadTrace(siren=lead.siren, name=lead.name)
            trace.span("sirene_fetch", duration_ms=120, data={...})
            trace.decision("preliminary_score", score=65, kept=True)
            trace.span("llm_reasoner", duration_ms=850,
                       data={"verdict": "STRONG_FIT", "score": 87})
            trace.final_verdict("kept", reason="LLM strong fit + score 87")
            writer.write(trace)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class TraceSpan:
    """Une étape unitaire du pipeline pour un lead."""
    name: str                       # ex. "sirene_fetch", "web_enrichment", "llm_reasoner"
    started_at: float               # epoch seconds
    duration_ms: int                # ms
    data: dict                      # payload (status, sources, scores...)
    error: Optional[str] = None


@dataclass
class TraceDecision:
    """Une décision prise sur le lead (keep/drop/score)."""
    step: str                       # ex. "quality_gate", "llm_reasoner", "human_review"
    decision: str                   # "keep" | "drop" | "borderline"
    reason: str                     # texte humain
    score: Optional[float] = None   # 0-100 si applicable
    data: Optional[dict] = None     # détails


@dataclass
class LeadTrace:
    """Trace complète d'un lead à travers le pipeline.

    Contient les spans (étapes techniques) + les decisions (verdicts business)
    + un final_verdict. Sérialise en JSON pour export JSONL.
    """
    siren: Optional[str] = None
    company_name: str = ""
    campaign_id: str = ""
    started_at: float = field(default_factory=time.time)
    spans: list[TraceSpan] = field(default_factory=list)
    decisions: list[TraceDecision] = field(default_factory=list)
    final_status: str = "in_progress"   # in_progress | kept | dropped | error
    final_reason: str = ""

    def span(self, name: str, *, duration_ms: int = 0,
             data: Optional[dict] = None, error: Optional[str] = None) -> None:
        """Enregistre une étape technique terminée."""
        self.spans.append(TraceSpan(
            name=name,
            started_at=time.time(),
            duration_ms=duration_ms,
            data=data or {},
            error=error,
        ))

    def decision(self, step: str, *, decision: str, reason: str,
                 score: Optional[float] = None,
                 data: Optional[dict] = None) -> None:
        """Enregistre une décision business prise sur le lead."""
        self.decisions.append(TraceDecision(
            step=step,
            decision=decision,
            reason=reason,
            score=score,
            data=data,
        ))

    def final_verdict(self, status: str, reason: str) -> None:
        """Verdict final du lead (kept / dropped / error)."""
        self.final_status = status
        self.final_reason = reason

    def to_dict(self) -> dict:
        return {
            "siren": self.siren,
            "company_name": self.company_name,
            "campaign_id": self.campaign_id,
            "started_at": self.started_at,
            "duration_total_ms": int(sum(s.duration_ms for s in self.spans)),
            "n_spans": len(self.spans),
            "spans": [
                {"name": s.name, "duration_ms": s.duration_ms,
                 "data": s.data, "error": s.error}
                for s in self.spans
            ],
            "decisions": [
                {"step": d.step, "decision": d.decision, "reason": d.reason,
                 "score": d.score, "data": d.data}
                for d in self.decisions
            ],
            "final_status": self.final_status,
            "final_reason": self.final_reason,
        }


class TraceWriter:
    """Writer JSONL append-safe (1 fichier par campagne).

    Usage en context-manager :
        with TraceWriter("output/run-trace.jsonl") as w:
            w.write(trace1)
            w.write(trace2)

    Si le fichier existe déjà, on append. Permet de reprendre une campagne
    interrompue ou d'agréger plusieurs sessions.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def __enter__(self):
        # Append mode: persistant à travers les runs
        self._fh = open(self.path, "a", encoding="utf-8")
        return self

    def __exit__(self, *args):
        if self._fh:
            self._fh.close()
            self._fh = None

    def write(self, trace: LeadTrace) -> None:
        """Ajoute 1 ligne JSON au fichier trace."""
        if not self._fh:
            self._fh = open(self.path, "a", encoding="utf-8")
        line = json.dumps(trace.to_dict(), ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()


# ---------------------------------------------------------------------------
# Lecture / analyse offline d'une trace
# ---------------------------------------------------------------------------

def load_trace(path: str | Path) -> list[dict]:
    """Charge un fichier JSONL en list de dicts."""
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def summarize_trace(path: str | Path) -> dict:
    """Compute un résumé statistique d'une trace de campagne."""
    traces = load_trace(path)
    if not traces:
        return {"n_leads": 0}
    n_kept = sum(1 for t in traces if t["final_status"] == "kept")
    n_dropped = sum(1 for t in traces if t["final_status"] == "dropped")
    n_error = sum(1 for t in traces if t["final_status"] == "error")
    avg_dur = sum(t.get("duration_total_ms", 0) for t in traces) / len(traces)
    # Distribution des reasons de drop
    from collections import Counter
    drop_reasons = Counter(
        t["final_reason"][:80] for t in traces if t["final_status"] == "dropped"
    )
    return {
        "n_leads": len(traces),
        "n_kept": n_kept,
        "n_dropped": n_dropped,
        "n_error": n_error,
        "avg_duration_ms_per_lead": int(avg_dur),
        "top_drop_reasons": drop_reasons.most_common(5),
    }


def _cli() -> None:
    """CLI: summarize <path> ou print <path>."""
    import argparse
    p = argparse.ArgumentParser(description="Decision trace inspector")
    sub = p.add_subparsers(dest="cmd", required=True)
    s1 = sub.add_parser("summary", help="Show campaign summary")
    s1.add_argument("path")
    s2 = sub.add_parser("dump", help="Dump all traces as JSON")
    s2.add_argument("path")
    args = p.parse_args()
    if args.cmd == "summary":
        print(json.dumps(summarize_trace(args.path), indent=2, ensure_ascii=False))
    elif args.cmd == "dump":
        for t in load_trace(args.path):
            print(json.dumps(t, indent=2, ensure_ascii=False))
            print("---")


if __name__ == "__main__":
    _cli()
