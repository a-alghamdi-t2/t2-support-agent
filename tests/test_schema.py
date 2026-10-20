"""Rules the database itself enforces, and the knowledge base names."""

from __future__ import annotations

import psycopg
import pytest

from support_agent.config import Settings


def _ticket(db) -> int:
    return db.query_one(
        """INSERT INTO tickets (t2_ticket_id, ticket_number, requester_email)
           VALUES ('g-1', 'T-1', 'x@y.sa') RETURNING id""")["id"]


def test_drafts_spam_needs_no_product_routing_does(db):
    tid = _ticket(db)
    db.execute("INSERT INTO drafts (ticket_id, action, summary, status) "
               "VALUES (%s, 'spam', 'SEO offer', 'rejected')", (tid,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO drafts (ticket_id, action, summary) VALUES (%s, 'route', 's')",
                   (tid,))


def test_a_redraft_points_at_the_one_it_replaced(db):
    tid = _ticket(db)
    first = db.query_one("INSERT INTO drafts (ticket_id, action, product, summary, product_sure, "
                         "status) VALUES (%s, 'route', 'Ole5', 's', false, 'rejected') "
                         "RETURNING id", (tid,))["id"]
    db.execute("INSERT INTO drafts (ticket_id, action, product, summary, replaces_draft_id) "
               "VALUES (%s, 'route', 'RiCH', 's', %s)", (tid, first))
    row = db.query_one("SELECT replaces_draft_id FROM drafts WHERE status = 'pending'")
    assert row["replaces_draft_id"] == first


def test_approve_and_send_writes_routing_then_reply_once_each(db):
    tid = _ticket(db)
    d = db.query_one("INSERT INTO drafts (ticket_id, action, product, summary) "
                     "VALUES (%s, 'answer', 'RiCH', 's') RETURNING id", (tid,))["id"]
    db.execute("INSERT INTO ticket_writes (draft_id, kind, payload) VALUES (%s, 'routing', '{}')",
               (d,))
    db.execute("INSERT INTO ticket_writes (draft_id, kind, payload) VALUES (%s, 'reply', '{}')",
               (d,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO ticket_writes (draft_id, kind, payload) "
                   "VALUES (%s, 'reply', '{}')", (d,))


def test_per_product_and_shared_urgent_words(db):
    db.execute("INSERT INTO urgent_keywords (product, keyword) VALUES (NULL, 'outage'), "
               "('RICH', 'outage'), ('RICH', 'OTP down')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO urgent_keywords (product, keyword) VALUES ('RICH', ' Outage ')")


def test_same_document_may_go_to_two_products_once_each(db):
    for product in ("RICH", "OLE5"):
        db.execute("INSERT INTO documents (product, filename, content_hash, uploaded_by) "
                   "VALUES (%s, 'guide.pdf', 'h1', 'me@t2.sa')", (product,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO documents (product, filename, content_hash, uploaded_by) "
                   "VALUES ('RICH', 'guide-copy.pdf', 'h1', 'me@t2.sa')")


def test_knowledge_base_named_after_the_product_code():
    s = Settings(database_url="postgresql://x@/y", kb_tenants="OLE5=ole5-docs")
    assert s.kb_tenant("RICH") == "rich" and s.kb_tenant("avl-sme") == "avl-sme"
    assert s.kb_tenant("Ole5") == "ole5-docs"
