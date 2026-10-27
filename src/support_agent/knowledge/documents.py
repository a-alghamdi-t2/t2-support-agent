"""The knowledge base documents, per product: upload, index, list, delete.

An upload is checked, saved under upload_dir/<code>/<hash>/<filename>, and
recorded as pending. Indexing it -- parsing, chunking, embedding -- takes from
seconds to minutes, so it is never done during the upload: index_pending()
does it, from the background loop (runtime.py) or scripts/kb.py. A document
goes pending -> indexing -> indexed, or failed with the reason, and a failed
one can be retried.

Each product has its own index (Settings.kb_tenant: RICH -> rich), and a
document is only ever added to or removed from its product's index. The same
file can be in two products, once each; uploading it twice to one product is
answered with the one already there.

Every file goes through Docling (DOCLING_URL) first: RAGent2 0.2.0 has it turn
even Markdown into its own Markdown, and reads PDFs page by page with a vision
model. So without DOCLING_URL nothing is accepted for upload, and nothing
waiting is indexed. The same code runs locally and in production; only the
addresses differ.

Every upload and delete goes in the audit log, with who did it.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import threading
from pathlib import Path

from support_agent import options
from support_agent.config import get_settings
from support_agent.db import audit, postgres
from support_agent.knowledge import retriever
from support_agent.logging import get_logger

log = get_logger(__name__)

TEXT_TYPES = (".md", ".txt")
"""Indexed as they are."""
DOCLING_TYPES = (".pdf", ".docx", ".pptx", ".xlsx")
"""Turned into text by Docling first."""

CHUNK_COLLECTION = "default"
"""RAGent2's one collection in Qdrant; tenants are separated inside it."""


class DocumentError(ValueError):
    """The change cannot be made. The message is for the person."""


def _product(code: str) -> str:
    o = options.product(code or "")
    if o is None or o.removed or (o.product or "").upper() == \
            get_settings().intake_product.upper():
        raise DocumentError(f"{code!r} is not a product")
    return o.product


def _who(who: str) -> str:
    who = (who or "").strip().lower()
    if "@" not in who[1:]:
        raise DocumentError("say who is doing this: an email")
    return who


def _folder(code: str, content_hash: str) -> Path:
    return get_settings().upload_dir / code.lower() / content_hash[:16]


def path_of(row: dict) -> Path:
    """Where the uploaded file is kept."""
    return _folder(row["product"], row["content_hash"]) / row["filename"]


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def listed(code: str | None = None) -> list[dict]:
    """A product's documents, newest first, or every product's."""
    if code:
        return postgres.query("SELECT * FROM documents WHERE upper(product) = %s "
                              "ORDER BY created_at DESC", (code.upper(),))
    return postgres.query("SELECT * FROM documents ORDER BY product, created_at DESC")


def get(document_id: int) -> dict:
    row = postgres.query_one("SELECT * FROM documents WHERE id = %s", (document_id,))
    if row is None:
        raise DocumentError(f"no document {document_id}")
    return row


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def upload(code: str, filename: str, raw: bytes, *, who: str) -> tuple[dict, bool]:
    """Take a file for a product's knowledge base. Returns (the document,
    whether it is new). The same file already in the product is returned as
    it is, not added again."""
    who = _who(who)
    code = _product(code)
    s = get_settings()
    name = Path((filename or "").replace("\\", "/")).name.strip()
    suffix = Path(name).suffix.lower()
    if not name or suffix not in TEXT_TYPES + DOCLING_TYPES:
        raise DocumentError(f"{suffix or 'that file'} cannot be indexed; accepted: "
                            + ", ".join(TEXT_TYPES + DOCLING_TYPES))
    if not s.docling_url:
        raise DocumentError("documents need Docling (DOCLING_URL) to be read, and it is "
                            "not set")
    if not raw:
        raise DocumentError("the file is empty")
    if len(raw) > s.max_upload_mb * 1024 * 1024:
        raise DocumentError(f"larger than {s.max_upload_mb} MB")
    if suffix in TEXT_TYPES:
        try:
            raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DocumentError("the file is not UTF-8 text; save it as UTF-8 and upload "
                                "again") from None

    digest = hashlib.sha256(raw).hexdigest()
    existing = postgres.query_one("SELECT * FROM documents WHERE product = %s "
                                  "AND content_hash = %s", (code, digest))
    if existing:
        return existing, False

    folder = _folder(code, digest)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(raw)
    row = postgres.query_one(
        "INSERT INTO documents (product, filename, content_hash, size_bytes, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *", (code, name, digest, len(raw), who))
    audit.record(who=who, action="upload", what="document", name=f"{code} / {name}",
                 new=f"{len(raw):,} bytes")
    log.info("document uploaded", extra={"product": code, "file": name, "id": row["id"]})
    return row, True


# ---------------------------------------------------------------------------
# indexing
# ---------------------------------------------------------------------------

def _claim(limit: int) -> list[dict]:
    """Pending documents, marked indexing in the same statement so a second
    pass running at once cannot take the same ones."""
    return postgres.query(
        """
        UPDATE documents SET status = 'indexing', error = NULL
        WHERE id IN (SELECT id FROM documents WHERE status = 'pending'
                     ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED)
        RETURNING *
        """, (limit,))


_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_LOCK = threading.Lock()


def _add(code: str, path: Path):
    """RAGent2's add, which is async, from synchronous code.

    Always on the same event loop, kept for the life of the process: RAGent2
    is built once (retriever._ragent) and its HTTP clients belong to the loop
    they were first used on. A new loop per document (asyncio.run) closes
    the old one under them, and the next document fails with "Event loop is
    closed". The lock keeps it to one document at a time."""
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            _LOOP = asyncio.new_event_loop()
        return _LOOP.run_until_complete(retriever.tenant(code).aadd(str(path)))


def index_pending(*, limit: int = 5) -> tuple[int, int]:
    """Index what is waiting. Returns (indexed, failed). Never raises for one
    document. Without a knowledge base configured, does nothing."""
    s = get_settings()
    if not s.qdrant_url or not s.docling_url:
        return 0, 0
    try:
        from ragent2.errors import DuplicateDocumentError
    except ImportError:
        return 0, 0

    done = failed = 0
    for row in _claim(limit):
        path = path_of(row)
        error = None
        try:
            if not path.is_file():
                raise FileNotFoundError(f"the uploaded file is gone: {path}")
            result = _add(row["product"], path)
            postgres.execute(
                "UPDATE documents SET status = 'indexed', ragent_doc_id = %s, chunk_count = %s, "
                "indexed_at = now(), error = NULL WHERE id = %s",
                (getattr(result, "doc_id", None), getattr(result, "chunk_count", None),
                 row["id"]))
            done += 1
            log.info("document indexed", extra={"id": row["id"], "product": row["product"],
                                                "chunks": getattr(result, "chunk_count", None)})
            continue
        except DuplicateDocumentError:
            # Already in the index under this name (indexed before, by us or
            # an earlier system): it can be searched, so it counts as indexed.
            postgres.execute("UPDATE documents SET status = 'indexed', indexed_at = now(), "
                             "error = 'already in the index' WHERE id = %s", (row["id"],))
            done += 1
            continue
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("indexing failed", extra={"id": row["id"]})
        postgres.execute("UPDATE documents SET status = 'failed', error = %s WHERE id = %s",
                         (error, row["id"]))
        failed += 1
    return done, failed


def retry(document_id: int, *, who: str) -> None:
    """A failed document, back to pending."""
    who = _who(who)
    row = get(document_id)
    if row["status"] != "failed":
        raise DocumentError(f"this document is {row['status']}, not failed")
    postgres.execute("UPDATE documents SET status = 'pending', error = NULL WHERE id = %s "
                     "AND status = 'failed'", (document_id,))
    audit.record(who=who, action="change", what="document",
                 name=f"{row['product']} / {row['filename']}", old="failed", new="retry")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def _remove_from_index(row: dict) -> None:
    """Take the document out of its product's index, or raise.

    By RAGent2's document ID first. Then by our content hash, which RAGent2
    records on every chunk: that also clears what a failed or interrupted
    indexing left behind, and counting by it afterwards is the proof that
    nothing is left. The tenant filter is not optional: one collection holds
    every product's index."""
    if row["ragent_doc_id"]:
        retriever.tenant(row["product"]).remove(row["ragent_doc_id"])
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm
    except ImportError:
        return
    hits = qm.Filter(must=[
        qm.FieldCondition(key="group_id", match=qm.MatchValue(
            value=get_settings().kb_tenant(row["product"]))),
        qm.FieldCondition(key="metadata.file_hash", match=qm.MatchValue(
            value=row["content_hash"])),
    ])
    client = QdrantClient(url=get_settings().qdrant_url)
    client.delete(collection_name=CHUNK_COLLECTION,
                  points_selector=qm.FilterSelector(filter=hits), wait=True)
    left = client.count(collection_name=CHUNK_COLLECTION, count_filter=hits,
                        exact=True).count
    if left:
        raise RuntimeError(f"{left} chunk(s) still in the index")


def delete(document_id: int, *, who: str) -> None:
    """Remove a document from its product's index, then its file and row.

    Refused while it is being indexed: removing it then would leave the
    chunks the indexing is about to write. If it cannot be taken out of the
    index, the row stays, so it can be tried again: a row gone while chunks
    remain is a document the agent answers from that nobody can see."""
    who = _who(who)
    row = get(document_id)
    if row["status"] == "indexing":
        raise DocumentError("still being indexed; delete it when it has finished")
    if row["status"] != "pending" and get_settings().qdrant_url:
        try:
            _remove_from_index(row)
        except Exception as exc:
            log.exception("document not removed from the index", extra={"id": document_id})
            raise DocumentError(f"it could not be removed from the knowledge base ({exc}); "
                                "it is still listed, so try again") from None
    elif row["status"] == "indexed":
        raise DocumentError("the knowledge base is not reachable (QDRANT_URL), so the "
                            "document cannot be taken out of it")

    gone = postgres.execute("DELETE FROM documents WHERE id = %s AND status <> 'indexing'",
                            (document_id,))
    if not gone:
        raise DocumentError("it started indexing a moment ago; try again when it finishes")
    folder = _folder(row["product"], row["content_hash"])
    root = get_settings().upload_dir.resolve()
    try:
        if folder.resolve().is_dir() and root in folder.resolve().parents:
            shutil.rmtree(folder.resolve())
    except OSError:
        log.warning("uploaded file not removed", extra={"folder": str(folder)})
    audit.record(who=who, action="remove", what="document",
                 name=f"{row['product']} / {row['filename']}", old=row["status"])
    log.info("document deleted", extra={"id": document_id, "product": row["product"]})
