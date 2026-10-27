"""Keep a history index in Qdrant in line with the database, one ticket at a
time.

Both history indexes -- similar tickets (precedent.py) and spam (spam.py) --
hold one entry per closed ticket, whose point id is derived from the ticket
number. So instead of rebuilding an index to add a week's tickets, sync()
compares:

    have   the ticket numbers the index holds now (read from Qdrant, no
           embedding)
    want   the ticket numbers it should hold (from the database)

and adds what is missing, removes what should not be there, and re-adds the
tickets named in `refresh` (the week's, whose text may have changed).

Comparing rather than trusting each step is what keeps it right: an entry left
behind by a run that failed half-way, a sender unblocked after their tickets
were learned as spam -- the next sync corrects it.

Entries are written with RAGent2's own writer and embedder, into the same
collection as the knowledge bases, under their own tenant (t2-precedent,
t2-spam), so a product's knowledge base and the history never mix.
"""

from __future__ import annotations

from support_agent.config import get_settings
from support_agent.knowledge import retriever
from support_agent.knowledge.documents import CHUNK_COLLECTION
from support_agent.logging import get_logger

log = get_logger(__name__)

BATCH = 200
"""Entries embedded per call."""


def _parts():
    """Qdrant, RAGent2's embedder and writer, and its settings."""
    from qdrant_client import QdrantClient
    from ragent2.store import writer

    rs = retriever.rag_settings()
    embedder = getattr(retriever._ragent(), "embedder", None)
    if embedder is None:
        from ragent2.embedding.models import Embedder

        embedder = Embedder(embedding_service_url=rs.embedding_service_url,
                            sparse_model_name=rs.sparse_model, bm25_lang=rs.bm25_lang,
                            timeout=rs.embedding_service_timeout)
    return QdrantClient(url=get_settings().qdrant_url), embedder, writer, rs


def entry(text: str, number: str, doc_id: str, **meta) -> dict:
    """One ticket as an index entry. Its number is the chunk_id, so the same
    ticket always gets the same point id and adding it again replaces it."""
    return {"page_content": text,
            "metadata": {"file_hash": doc_id, "chunk_id": number,
                         "document_name": f"ticket {number}", "source_file": number,
                         "ticket_number": number, **meta}}


def _number_of(payload: dict) -> str | None:
    """The ticket number an entry belongs to, wherever the writer put it."""
    for src in (payload or {}, (payload or {}).get("metadata") or {}):
        value = src.get("ticket_number") or src.get("chunk_id")
        if value:
            return str(value)
    return None


def _tenant_filter(tenant: str):
    from qdrant_client.http import models as qm

    return qm.Filter(must=[qm.FieldCondition(key="group_id",
                                             match=qm.MatchValue(value=tenant))])


def indexed(client, tenant: str) -> dict[str, list]:
    """Ticket number -> the point ids holding it, for one tenant."""
    out: dict[str, list] = {}
    unreadable = 0
    offset = None
    while True:
        try:
            points, offset = client.scroll(collection_name=CHUNK_COLLECTION,
                                           scroll_filter=_tenant_filter(tenant), limit=1000,
                                           offset=offset, with_payload=True,
                                           with_vectors=False)
        except Exception as exc:
            if "not found" in str(exc).lower() or "doesn't exist" in str(exc).lower():
                return out           # no collection yet: nothing indexed anywhere
            raise
        for p in points:
            number = _number_of(p.payload)
            if number:
                out.setdefault(number, []).append(p.id)
            else:
                unreadable += 1
        if offset is None:
            break
    # Entries exist but none says which ticket it is: the payload is laid out
    # differently from what this reads. Stop rather than conclude the index is
    # empty -- that would re-embed everything and hide the mismatch.
    if unreadable and not out:
        raise RuntimeError(f"{unreadable} entries in index {tenant!r}, but no ticket number "
                           "could be read from any of them; not syncing")
    return out


def sync(tenant: str, doc_id: str, want: dict[str, dict],
         refresh: set[str] | None = None) -> dict:
    """Bring one index in line with `want` (ticket number -> its entry).
    Returns what changed."""
    from qdrant_client.http import models as qm

    refresh = refresh or set()
    client, embedder, writer, rs = _parts()
    have = indexed(client, tenant)

    remove_ids = [pid for n, ids in have.items() if n not in want for pid in ids]
    remove_ids += [pid for n, ids in have.items() if n in want for pid in ids[1:]]
    add = [want[n] for n in want if n not in have or n in refresh]

    for i in range(0, len(remove_ids), 1000):
        client.delete(collection_name=CHUNK_COLLECTION,
                      points_selector=qm.PointIdsList(points=remove_ids[i:i + 1000]),
                      wait=True)
    for i in range(0, len(add), BATCH):
        writer.ingest(add[i:i + BATCH], user_id=tenant, client=client, embedder=embedder,
                      doc_id=doc_id, settings=rs)

    added = sum(1 for n in want if n not in have)
    out = {"added": added, "refreshed": len(add) - added, "removed": len(remove_ids),
           "total": len(want)}
    log.info("index synced", extra={"tenant": tenant, **out})
    return out


def size(tenant: str) -> int:
    """Entries in one index: a count in Qdrant, no embedding. Zero when Qdrant
    cannot be reached or nothing was indexed yet."""
    if not get_settings().qdrant_url:
        return 0
    try:
        from qdrant_client import QdrantClient

        return QdrantClient(url=get_settings().qdrant_url).count(
            collection_name=CHUNK_COLLECTION, count_filter=_tenant_filter(tenant),
            exact=True).count
    except Exception:
        log.warning("could not count an index", extra={"tenant": tenant})
        return 0


def search(tenant: str, text: str, limit: int) -> list[dict]:
    """The closest entries to this text: {"ticket_number", "text", "score"}.
    Raises when the search fails; the callers decide what that means."""
    passages = retriever._tenant(tenant).search(
        text, top_k=max(limit, 1), use_reranker=get_settings().kb_rerank)
    out = []
    for p in passages or []:
        meta = getattr(p, "metadata", None) or {}
        number = _number_of(meta) or _number_of(getattr(p, "__dict__", {}) or {})
        if not number:
            continue
        out.append({"ticket_number": number,
                    "text": (getattr(p, "text", None) or getattr(p, "page_content", None)
                             or ""),
                    "score": float(getattr(p, "score", 0) or 0)})
    return out
