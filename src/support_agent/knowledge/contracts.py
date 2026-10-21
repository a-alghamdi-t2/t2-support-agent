"""What retrieval hands the orchestrator.

Our own shape, not RAGent2's. The orchestrator reads this, so replacing what is
underneath -- find(), search() plus a verdict agent, something else entirely --
changes this file and nothing above it.

A verdict about the knowledge base, not about the ticket. Whether the customer
has given us enough to act, and what to write back, are decided with the ticket
in hand and belong to the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    ANSWERABLE = "answerable"
    """At least one passage answers the question directly."""

    PARTIAL = "partial"
    """Something related was found, but nothing that answers it. Usually a
    route, and the note should say what was close."""

    NOT_IN_KB = "not_in_kb"
    """Nothing relevant. The knowledge base does not cover this."""


@dataclass
class Evidence:
    text: str
    document: str
    section: str | None
    source_file: str | None
    relation: str          # DIRECT | INFERENTIAL, as RAGent2 grades it
    score: int             # 3 direct, 2 inferential
    reason: str            # why it was judged relevant

    @property
    def language(self) -> str | None:
        """From the filename convention: KB-001-AR-... / KB-001-EN-...

        The frontmatter has a lang field but RAGent2 does not keep it, and the
        filename does carry it, so this is a convention rather than a guess.
        """
        if not self.source_file:
            return None
        parts = self.source_file.upper().split("-")
        for part in parts:
            if part in ("AR", "EN"):
                return part.lower()
        return None


@dataclass
class Retrieval:
    query: str
    verdict: Verdict
    evidence: list[Evidence] = field(default_factory=list)
    queries_tried: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None
    considered: list[str] = field(default_factory=list)
    """Documents the retriever surfaced and then did not return."""


    @property
    def direct(self) -> list[Evidence]:
        return [e for e in self.evidence if e.relation == "DIRECT"]

    @property
    def documents(self) -> list[str]:
        """Distinct documents, in the order first seen. For the audit trail."""
        seen: list[str] = []
        for e in self.evidence:
            if e.document not in seen:
                seen.append(e.document)
        return seen

    def render(self) -> str:
        """The evidence as text, for the prompt."""
        if not self.evidence:
            return "KNOWLEDGE BASE\n  Nothing found. This is not covered."

        lines = [f"KNOWLEDGE BASE ({self.verdict.value})"]
        for i, e in enumerate(self.evidence, 1):
            where = f"{e.document}" + (f" / {e.section}" if e.section else "")
            lines += [
                "",
                f"  [{i}] {where}  ({e.relation.lower()})",
                "  " + e.text.strip().replace("\n", "\n  "),
            ]
        return "\n".join(lines)

    def as_json(self) -> dict:
        """For the drafts.evidence column and the audit log."""
        return {
            "query": self.query,
            "verdict": self.verdict.value,
            "stop_reason": self.stop_reason,
            "queries_tried": self.queries_tried,
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
            "error": self.error,
            "considered": self.considered,
            "evidence": [
                {
                    "document": e.document,
                    "section": e.section,
                    "source_file": e.source_file,
                    "relation": e.relation,
                    "score": e.score,
                    "reason": e.reason,
                    "language": e.language,
                    "text": e.text,
                }
                for e in self.evidence
            ],
        }

def strongest(retrievals: list[Retrieval]) -> Verdict:
    """The best verdict across several searches.

    A ticket can ask more than one thing, so the orchestrator searches more than
    once. A search that found nothing must not bury one that succeeded: the
    strongest wins, and every search is kept for the audit trail.
    """
    if any(r.verdict == Verdict.ANSWERABLE for r in retrievals):
        return Verdict.ANSWERABLE
    if any(r.verdict == Verdict.PARTIAL for r in retrievals):
        return Verdict.PARTIAL
    return Verdict.NOT_IN_KB


def all_evidence(retrievals: list[Retrieval]) -> list[Evidence]:
    """Every passage from every search, deduplicated on its text."""
    seen: set[str] = set()
    out: list[Evidence] = []
    for r in retrievals:
        for e in r.evidence:
            key = e.text[:200]
            if key not in seen:
                seen.add(key)
                out.append(e)
    return out