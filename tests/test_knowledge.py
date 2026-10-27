"""The knowledge base documents and team scopes, with RAGent2 faked and a
real database."""

from __future__ import annotations

import sys
import types

import pytest

from support_agent import config, options
from support_agent.knowledge import documents, retriever, scopes
from support_agent.orchestrator import route

ADMIN = "ai.admin@t2.sa"
GUIDE = "# Sender names\n\nTo renew a sender name, send the signed contract.\n".encode()


class Duplicate(Exception):
    pass


class FakeIndex:
    """One product's index in a fake RAGent2."""

    def __init__(self, store, code):
        self.store, self.code = store, code

    async def aadd(self, path):
        self.store.added.append((self.code, path))
        if self.store.fail:
            raise RuntimeError(self.store.fail)
        if self.store.duplicate:
            raise Duplicate("already indexed")
        return types.SimpleNamespace(doc_id=f"doc-{len(self.store.added)}", chunk_count=3)

    def remove(self, doc_id):
        if self.store.remove_fails:
            raise RuntimeError("qdrant down")
        self.store.removed.append((self.code, doc_id))


@pytest.fixture
def rag(monkeypatch, tmp_path):
    """RAGent2 installed (faked), Qdrant set, uploads in a temporary folder."""
    store = types.SimpleNamespace(added=[], removed=[], fail=None, duplicate=False,
                                  remove_fails=False)
    errors = types.ModuleType("ragent2.errors")
    errors.DuplicateDocumentError = Duplicate
    package = types.ModuleType("ragent2")
    package.errors = errors
    monkeypatch.setitem(sys.modules, "ragent2", package)
    monkeypatch.setitem(sys.modules, "ragent2.errors", errors)
    monkeypatch.setitem(sys.modules, "qdrant_client", None)      # by doc id only
    monkeypatch.setattr(retriever, "tenant", lambda code: FakeIndex(store, code))
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    monkeypatch.setenv("DOCLING_URL", "http://localhost:8092")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    config.env_settings.cache_clear()
    config.refresh_settings()
    return store


@pytest.fixture
def values(db, t2, monkeypatch):
    monkeypatch.setattr(options, "_last_sync", float("-inf"))
    options.invalidate()
    options.sync(t2=t2, force=True)
    yield
    options.invalidate()


def test_upload_index_and_search_one_products_knowledge_base(db, values, rag, tmp_path):
    assert retriever.unavailable("RICH") == "no documents uploaded for this product yet"
    doc, new = documents.upload("rich", "sender-names.md", GUIDE, who="AI.Admin@t2.sa")
    assert new and doc["status"] == "pending" and doc["product"] == "RICH"
    assert (tmp_path / "rich" / doc["content_hash"][:16] / "sender-names.md").read_bytes() == GUIDE

    again, new = documents.upload("RICH", "copy.md", GUIDE, who=ADMIN)
    assert not new and again["id"] == doc["id"]                  # same file, same product
    assert documents.upload("OLE5", "sender-names.md", GUIDE, who=ADMIN)[1]   # another product

    assert documents.index_pending() == (2, 0)
    assert [code for code, _ in rag.added] == ["RICH", "OLE5"]   # each into its own index
    row = documents.get(doc["id"])
    assert (row["status"], row["ragent_doc_id"], row["chunk_count"]) == ("indexed", "doc-1", 3)
    assert retriever.unavailable("RICH") is None                 # the agent may search now
    audit = db.query_one("SELECT * FROM audit_log WHERE action = 'upload'")
    assert (audit["who"], audit["name"]) == ("ai.admin@t2.sa", "RICH / sender-names.md")


def test_what_cannot_be_uploaded(db, values, rag, monkeypatch):
    with pytest.raises(documents.DocumentError, match="cannot be indexed"):
        documents.upload("RICH", "run.exe", b"x", who=ADMIN)
    with pytest.raises(documents.DocumentError, match="UTF-8"):
        documents.upload("RICH", "guide.md", "دليل".encode("cp1256"), who=ADMIN)
    with pytest.raises(documents.DocumentError, match="not a product"):
        documents.upload("EMAIL", "guide.md", GUIDE, who=ADMIN)
    with pytest.raises(documents.DocumentError, match="empty"):
        documents.upload("RICH", "guide.md", b"", who=ADMIN)
    assert documents.upload("RICH", "guide.pdf", b"%PDF-1.7", who=ADMIN)[1]
    monkeypatch.delenv("DOCLING_URL")             # RAGent2 0.2.0 reads every file with it
    config.env_settings.cache_clear()
    config.refresh_settings()
    with pytest.raises(documents.DocumentError, match="need Docling"):
        documents.upload("RICH", "notes.md", GUIDE, who=ADMIN)
    assert documents.index_pending() == (0, 0)    # what waits, waits


def test_addresses_must_be_addresses(monkeypatch):
    def problems() -> str:
        config.env_settings.cache_clear()
        try:
            config.env_settings()
            return ""
        except config.ConfigError as exc:
            return str(exc)
        finally:
            config.env_settings.cache_clear()

    monkeypatch.setenv("DOCLING_URL", "DOCLING_URL=http://localhost:8092")   # a typo in .env
    assert "DOCLING_URL" in problems() and "http://" in problems()
    monkeypatch.setenv("DOCLING_URL", "http://localhost:8092/")
    assert "DOCLING_URL" not in problems()


def test_indexing_keeps_one_event_loop(db, values, rag):
    """RAGent2's HTTP clients belong to the loop they were first used on: a
    second document on a new loop failed with "Event loop is closed"."""
    import asyncio
    loops = []
    real = FakeIndex.aadd

    async def remember(self, path):
        loops.append(asyncio.get_running_loop())
        return await real(self, path)

    FakeIndex.aadd = remember
    try:
        documents.upload("RICH", "a.md", GUIDE, who=ADMIN)
        documents.upload("RICH", "b.md", GUIDE + b"more", who=ADMIN)
        assert documents.index_pending() == (2, 0)
        assert documents.index_pending() == (0, 0)
        documents.upload("RICH", "c.md", GUIDE + b"again", who=ADMIN)
        assert documents.index_pending() == (1, 0)
    finally:
        FakeIndex.aadd = real
    assert len(loops) == 3 and len({id(x) for x in loops}) == 1


def test_a_failed_index_is_kept_and_retried(db, values, rag):
    doc, _ = documents.upload("RICH", "guide.md", GUIDE, who=ADMIN)
    rag.fail = "embedding service timed out"
    assert documents.index_pending() == (0, 1)
    row = documents.get(doc["id"])
    assert row["status"] == "failed" and "embedding service timed out" in row["error"]
    rag.fail = None
    documents.retry(doc["id"], who=ADMIN)
    assert documents.index_pending() == (1, 0)


def test_a_document_already_in_the_index_counts_as_indexed(db, values, rag):
    rag.duplicate = True
    doc, _ = documents.upload("OLE5", "faq.md", GUIDE, who=ADMIN)
    assert documents.index_pending() == (1, 0)
    assert documents.get(doc["id"])["status"] == "indexed"


def test_delete(db, values, rag, tmp_path):
    doc, _ = documents.upload("RICH", "guide.md", GUIDE, who=ADMIN)
    db.execute("UPDATE documents SET status = 'indexing' WHERE id = %s", (doc["id"],))
    with pytest.raises(documents.DocumentError, match="being indexed"):
        documents.delete(doc["id"], who=ADMIN)
    db.execute("UPDATE documents SET status = 'pending' WHERE id = %s", (doc["id"],))
    documents.index_pending()

    rag.remove_fails = True                                      # kept, to try again
    with pytest.raises(documents.DocumentError, match="still listed"):
        documents.delete(doc["id"], who=ADMIN)
    assert documents.listed("RICH")

    rag.remove_fails = False
    documents.delete(doc["id"], who=ADMIN)
    assert rag.removed == [("RICH", "doc-1")]
    assert documents.listed("RICH") == []
    assert not (tmp_path / "rich" / doc["content_hash"][:16]).exists()
    assert db.query_one("SELECT * FROM audit_log WHERE action = 'remove'")["name"] == (
        "RICH / guide.md")


def test_nothing_is_indexed_without_a_knowledge_base(db, values, monkeypatch, tmp_path):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("DOCLING_URL", "http://localhost:8092")   # Docling, but no Qdrant
    config.env_settings.cache_clear()
    config.refresh_settings()
    documents.upload("RICH", "guide.md", GUIDE, who=ADMIN)      # accepted, waits
    assert documents.index_pending() == (0, 0)
    assert documents.listed("RICH")[0]["status"] == "pending"


def test_team_scopes_per_product_and_for_all(db, values):
    scopes.save("RICH", "Billing: invoices and refunds.\nTechnical: delivery and OTP.",
                who=ADMIN, filename="rich-teams.md")
    scopes.save(None, "Sales: prices and new customers, for every product.", who=ADMIN)
    v2 = scopes.save("RICH", b"Billing: invoices.\nTechnical: delivery.", who=ADMIN)
    assert scopes.save("RICH", "Billing: invoices.\nTechnical: delivery.",
                       who=ADMIN)["id"] == v2["id"]              # unchanged: no new version
    assert scopes.current("RICH")["versions"] == 2
    block = route._scopes_block("RICH")
    assert "Billing: invoices." in block and "Sales: prices" in block
    assert "Billing: invoices and refunds" not in block          # only the newest
    assert "Sales: prices" in route._scopes_block("OLE5")        # the shared one
    with pytest.raises(scopes.ScopesError):
        scopes.save("RICH", "x", who=ADMIN, filename="teams.pdf")
    rows = db.query("SELECT name, new_value FROM audit_log WHERE what = 'team scopes' "
                    "ORDER BY id")
    assert [r["name"] for r in rows] == ["RICH", "all products", "RICH"]


def test_a_search_result_as_ragent2_020_returns_it(db, monkeypatch):
    """0.2.0 puts the document's name in a passage's metadata, and in a trace
    entry's sources only: both must reach the draft."""
    ns = types.SimpleNamespace
    src = lambda name: ({"chunk_id": "x", "document_name": name, "page_number": None},)  # noqa: E731
    result = ns(
        chunks=(ns(chunk_id="c1", text="Use the Latest filter.", relation="DIRECT", score=3,
                   reason="answers it", sources=src("OLE5 FAQ"),
                   metadata={"document_name": "OLE5 FAQ", "section": "Search",
                             "source_file": "OLE5_KB.md"}),),
        diagnostics=ns(queries_tried=("search order",), stop_reason="enough_evidence",
                       trace=(ns(relation="DIRECT", returned=True, sources=src("OLE5 FAQ")),
                              ns(relation="IGNORE", returned=False, sources=src("OLE5 BRD")),
                              ns(relation=None, returned=False, sources=src("OLE5 Guide")))))
    asked = {}

    class Index:
        def find(self, question, **kw):
            asked.update(kw)
            return result

    monkeypatch.setattr(retriever, "_tenant", lambda name: Index())
    monkeypatch.setenv("KB_RERANK", "false")
    config.env_settings.cache_clear()
    config.refresh_settings()
    r = retriever.retrieve("Why are results not newest first?", "OLE5")
    assert asked == {"use_reranker": False}
    e = r.evidence[0]
    assert (e.document, e.section, e.source_file, e.relation) == (
        "OLE5 FAQ", "Search", "OLE5_KB.md", "DIRECT")
    assert r.considered == ["OLE5 BRD (ignore)", "OLE5 Guide (ungraded)"]
