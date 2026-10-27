"""Retrieval, over RAGent2, one knowledge base per product.

One call to find(), their agentic retrieval: it reformulates the question,
searches several times, and grades each passage DIRECT or INFERENTIAL with a
reason. That grading is most of a verdict already, so this file maps rather
than reasons -- no second model call.

Each product has its own index, named after its code (RICH -> rich, see
Settings.kb_tenant), and the agent searches only the one of the product it
chose. An index is made by the first upload to it; until then the product has
no documents and unavailable() says so, so the agent is not offered a search
that can only come back empty.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.knowledge.contracts import Evidence, Retrieval, Verdict
from support_agent.logging import get_logger

log = get_logger(__name__)

UNRELIABLE_STOPS = {"timeout", "error", "model_failed"}


class KnowledgeUnavailable(RuntimeError):
    """RAGent2 is not installed or not configured."""


def unavailable(code: str) -> str | None:
    """Why the product's knowledge base cannot be searched, or None when it
    can. Checked before the agent is offered the search."""
    s = get_settings()
    if not s.qdrant_url:
        return "QDRANT_URL is not set"
    if not s.groq_api_key:
        return "GROQ_API_KEY is not set"
    try:
        import ragent2  # noqa: F401
    except ImportError:
        return "ragent2 is not installed"
    row = postgres.query_one(
        "SELECT count(*) AS n FROM documents WHERE upper(product) = %s AND status = 'indexed'",
        (code.strip().upper(),))
    if not row or not row["n"]:
        return "no documents uploaded for this product yet"
    return None


@lru_cache(maxsize=1)
def _ragent() -> Any:
    """The RAGent2 client. Built once; it caches models."""
    s = get_settings()
    if not s.groq_api_key:
        raise KnowledgeUnavailable("GROQ_API_KEY is not set")
    try:
        from ragent2 import Ragent
        from ragent2.config import Settings as RagSettings
    except ImportError as exc:
        raise KnowledgeUnavailable(
            "ragent2 is not installed; it comes from the private feed") from exc

    values = {
        "groq_api_key": s.groq_api_key.get_secret_value(),
        "qdrant_url": s.qdrant_url,
        "docling_serve_api_url": s.docling_url,
        "candidate_pool_size": s.kb_candidate_pool,
        "hybrid_prefetch_limit": s.kb_candidate_pool,
        "top_k": s.kb_top_k,
        "tool_call_repair_attempts": s.kb_tool_repair_attempts,
        "max_search_rounds": s.kb_max_rounds,
        "embedding_service_timeout": s.kb_embed_timeout,
    }
    # Only what is set: RAGent2 refuses None for its text settings and has its
    # own defaults for them. Searching needs no Docling; indexing does, and
    # documents.py does not index without DOCLING_URL.
    return Ragent(settings=RagSettings(**{k: v for k, v in values.items() if v is not None}))


@lru_cache(maxsize=32)
def _tenant(name: str) -> Any:
    log.info("knowledge ready", extra={"tenant": name})
    return _ragent().tenant(name)


def tenant(code: str) -> Any:
    """The product's index in RAGent2, for adding and removing documents."""
    return _tenant(get_settings().kb_tenant(code))


def _document_name(item: Any) -> str | None:
    """The document a passage or a trace entry comes from. RAGent2 0.2.0 has
    no document_name of its own on either: a passage has it in metadata, a
    trace entry only in its sources."""
    name = getattr(item, "document_name", None)
    if name:
        return name
    meta = getattr(item, "metadata", None) or {}
    if meta.get("document_name"):
        return meta["document_name"]
    for src in getattr(item, "sources", None) or ():
        if isinstance(src, dict) and src.get("document_name"):
            return src["document_name"]
    return None


def _to_evidence(chunk: Any) -> Evidence:
    meta = getattr(chunk, "metadata", None) or {}
    return Evidence(
        text=getattr(chunk, "text", "") or "",
        document=_document_name(chunk) or "unknown",
        section=meta.get("section"),
        source_file=meta.get("source_file"),
        relation=(getattr(chunk, "relation", None) or "UNKNOWN").upper(),
        score=int(getattr(chunk, "score", 0) or 0),
        reason=getattr(chunk, "reason", "") or "",
    )


def _verdict(evidence: list[Evidence]) -> Verdict:
    """Their grading, mapped to ours.

    DIRECT means a passage answers the question. INFERENTIAL means it is on the
    subject without answering -- worth showing a reviewer, not worth replying
    from, which is why it is partial rather than answerable.
    """
    if any(e.relation == "DIRECT" for e in evidence):
        return Verdict.ANSWERABLE
    if evidence:
        return Verdict.PARTIAL
    return Verdict.NOT_IN_KB


def retrieve(question: str, code: str) -> Retrieval:
    """Ask one product's knowledge base. Never raises: a failure is a verdict.

    Retrieval breaking must not stop a ticket being drafted. It becomes
    NOT_IN_KB with the error recorded, the agent routes, and a human sees
    why -- which is the same outcome as an unanswerable question, and the safe
    one.
    """
    started = time.perf_counter()
    tenant = get_settings().kb_tenant(code)

    try:
        # use_reranker is RAGent2 0.2.0's; left out when on, so 0.1.4 works too.
        kwargs = {} if get_settings().kb_rerank else {"use_reranker": False}
        result = _tenant(tenant).find(question, **kwargs)
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        log.exception("retrieval failed", extra={"ms": elapsed, "tenant": tenant})
        return Retrieval(query=question, verdict=Verdict.NOT_IN_KB, elapsed_ms=elapsed,
                         error=f"{type(exc).__name__}: {exc}")

    elapsed = int((time.perf_counter() - started) * 1000)
    evidence = [_to_evidence(c) for c in (getattr(result, "chunks", None) or ())]
    diagnostics = getattr(result, "diagnostics", None)

    retrieval = Retrieval(
        query=question,
        verdict=_verdict(evidence),
        evidence=evidence,
        queries_tried=list(getattr(diagnostics, "queries_tried", ()) or ()),
        stop_reason=getattr(diagnostics, "stop_reason", None),
        warnings=list(getattr(result, "warnings", ()) or ()),
        elapsed_ms=elapsed,
    )

    returned = {e.document for e in evidence}
    seen: dict[str, str] = {}
    for t in getattr(diagnostics, "trace", ()) or ():
        name = _document_name(t)
        if not name or getattr(t, "returned", False) or name in returned:
            continue
        # IGNORE means graded and rejected; None means never graded at all.
        seen[name] = getattr(t, "relation", None) or "ungraded"
    retrieval.considered = sorted(f"{n} ({r.lower()})" for n, r in seen.items())

    if not evidence and (retrieval.stop_reason in UNRELIABLE_STOPS or retrieval.warnings):
        retrieval.error = retrieval.error or f"retrieval incomplete: {retrieval.stop_reason}"
        log.warning("retrieval incomplete", extra={"stop": retrieval.stop_reason,
                                                   "query": question[:80]})

    log.info("retrieved", extra={"tenant": tenant, "verdict": retrieval.verdict.value,
                                 "chunks": len(evidence), "direct": len(retrieval.direct),
                                 "ms": elapsed, "stop": retrieval.stop_reason})
    return retrieval
