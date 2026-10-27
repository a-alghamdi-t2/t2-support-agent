"""The ticket system client against the fake TicketAPI (tests/fake_t2.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import fake_t2 as fake
from support_agent.ticketsystem import Note, T2Client, T2Error, connect
from support_agent.ticketsystem import client as client_module

RIYADH = timezone(timedelta(hours=3))


@pytest.fixture
def c(monkeypatch):
    fake.reset()
    monkeypatch.setattr(client_module, "RETRY_WAIT", (0.0, 0.0))
    cl = T2Client(base_url="http://t2.test/TicketAPI/", user=fake.USER, password=fake.PASSWORD,
                  min_interval=0)
    cl._http = httpx.Client(transport=fake.TRANSPORT)
    yield cl
    cl.close()


def test_intake_finds_only_waiting_email_tickets(c):
    a = fake.add_email("OTP not delivered", "Our OTP to Zain fails.", minutes_ago=50)
    b = fake.add_email("Renew sender", "Please renew ALWAHA.", minutes_ago=10)
    routed = fake.add_email("Old one", "Already routed.", minutes_ago=90)
    c.update(routed, product="RiCH", queue="RiCH Support", type="Inquiry",
             subtype="User Permissions")
    assert c.intake() == [a.lower(), b.lower()]           # oldest first, lower case
    sent = fake.calls[-1][1]
    assert sent["Products"] == ["EMAIL"] and sent["RequiresClassification"] == 1
    assert sent["Unassigned"] == 1 and sent["States"] == ["Open"] and sent["Limit"] == 50


def test_get_reads_every_field(c):
    tid = fake.add_email("OTP not delivered", "Line one\nLine two", "IT@Najd.sa", minutes_ago=56)
    t = c.get(tid.upper())
    assert t.id == tid.lower() and t.number.startswith("T-")
    assert t.product == "Unclassified Email" and t.product_code == "EMAIL" and t.queue == ""
    assert t.state == "Open" and t.state_type == "open" and not t.is_closed
    assert t.requires_classification and t.customer_email == "it@najd.sa"
    assert t.customer_status == "Pending" and t.customer_org == "Pending Email Contacts"
    assert t.created == datetime(2026, 10, 19, 8, 4, tzinfo=RIYADH)
    assert t.subtype == "" and t.conclusions == ""           # no Value at all comes back
    first = t.first_article
    assert first.from_customer and first.via_email and first.visible
    assert first.body == "Line one\nLine two"                # \r\n normalised
    assert t.history == ()


def test_routing_an_email_ticket_in_one_update(c):
    tid = fake.add_email("Renew sender", "Please renew.")
    r = c.update(tid, product="RiCH", queue="RiCH Support", type="Request",
                 subtype="Sender Name - Renewal Contract",
                 conclusions="The client requests the renewal of an existing contract",
                 priority="Medium", state="Waiting for Concerned Department",
                 note=Note("Support Agent: routed to RiCH Support", "Why: renewal request."))
    assert r.ticket_id == tid.lower() and r.article_id
    t = c.get(tid)
    assert (t.product_code, t.queue, t.type, t.subtype) == (
        "RICH", "RiCH Support", "Request", "Sender Name - Renewal Contract")
    assert not t.requires_classification and t.priority == "Medium"
    note = t.articles[-1]
    assert not note.from_customer and not note.visible and note.body == "Why: renewal request."
    moves = t.queue_moves
    assert [(m.from_queue, m.to_queue) for m in moves] == [("", "RiCH Support")]
    assert {h.action for h in t.history} >= {"Product Changed", "Reassigned",
                                            "Status Changed", "Priority Changed",
                                            "Internal Note Added"}
    assert c.intake() == []


def test_refusals_come_back_as_t2error(c):
    tid = fake.add_email("x", "y")
    with pytest.raises(T2Error) as e:
        c.update(tid, product="RiCH", queue="Nowhere")
    assert e.value.code == "TicketUpdate.InvalidQueue" and e.value.refused
    assert not e.value.maybe_applied
    with pytest.raises(T2Error) as e:
        c.update(tid, queue="RiCH Support")       # still Unclassified Email: no such queue there
    assert e.value.code == "TicketUpdate.InvalidQueue"
    with pytest.raises(T2Error) as e:
        c.get("00000000-0000-0000-0000-000000000000")
    assert e.value.code == "TicketGet.NotFound"


def test_bad_credentials(c):
    c._auth = {"UserLogin": fake.USER, "Password": "wrong"}
    with pytest.raises(T2Error) as e:
        c.ping()
    assert e.value.code == "TicketSearch.AccessDenied"


def test_long_conclusions_are_cut_not_refused(c):
    tid = fake.add_email("x", "y")
    c.update(tid, product="RiCH", queue="RiCH Support", subtype="User Permissions",
             conclusions="word " * 200)
    t = c.get(tid)
    assert len(t.conclusions) <= 500 and t.conclusions.endswith("…")


def test_reads_retry_a_busy_server_writes_do_not(c):
    tid = fake.add_email("x", "y")
    fake.fail_next.extend([503, 429])
    assert c.get(tid).id == tid.lower()                  # two failures, third try answers
    fake.fail_next.append(503)
    with pytest.raises(T2Error) as e:
        c.update(tid, product="RiCH", queue="RiCH Support")
    assert e.value.code == "Transport.HTTP503" and e.value.maybe_applied
    assert c.get(tid).queue == ""                        # not sent again


def test_a_write_that_never_left_is_retried(c, monkeypatch):
    tid = fake.add_email("x", "y")
    real = fake.handle
    tries = {"n": 0}

    def flaky(request):
        tries["n"] += 1
        if tries["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return real(request)

    c._http = httpx.Client(transport=httpx.MockTransport(flaky))
    c.update(tid, product="RiCH", queue="RiCH Support")
    assert tries["n"] == 2 and c.get(tid).queue == "RiCH Support"


def test_a_write_with_no_answer_is_not_repeated(c):
    tid = fake.add_email("x", "y")

    def slow(request):
        fake.handle(request)                              # applied on their side ...
        raise httpx.ReadTimeout("no answer", request=request)   # ... answer lost

    c._http = httpx.Client(transport=httpx.MockTransport(slow))
    with pytest.raises(T2Error) as e:
        c.update(tid, product="RiCH", queue="RiCH Support", note=Note("s", "b"))
    assert e.value.code == "Transport.Timeout" and e.value.maybe_applied
    assert sum(1 for th in fake.T[tid.upper()]["threads"] if not th["customer"]) == 1


def test_closed_between_sends_riyadh_times(c):
    tid = fake.add_email("x", "y")
    c.update(tid, product="RiCH", queue="RiCH Support", state="closed successful")
    start = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)     # 08:00 Riyadh
    end = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)       # 10:00 Riyadh
    assert c.closed_between(start, end, ["RICH"]) == [tid.lower()]
    sent = fake.calls[-1][1]
    assert sent["TicketCloseTimeNewerDate"] == "2026-10-19 08:00:00"
    assert sent["StateType"] == ["closed"]
    with pytest.raises(ValueError):
        c.closed_between(datetime(2026, 10, 19), end)             # naive time refused


def test_description_without_a_message_is_the_first_article(c):
    tid = fake.add_email("Created on screen", "The customer's request.")
    fake.T[tid]["threads"].clear()                    # their screen, no email to the customer
    c.update(tid, product="RiCH", queue="RiCH Support", note=Note("s", "agent note"))
    t = c.get(tid)
    assert t.first_article.is_description and t.first_article.body == "The customer's request."
    assert t.articles[1].body == "agent note"


def test_lookups_with_and_without_reasons(c):
    lk = c.lookups()
    rich = lk.product("rich")
    assert rich.name == "RiCH" and "RiCH Technical" in rich.queues
    assert [s.name for s in rich.types["Inquiry"]] == ["DLR - Call Barring", "User Permissions"]
    assert rich.types["Inquiry"][0].reason is None
    assert ("closed successful", True) in lk.statuses and lk.priorities[-1] == "Critical"
    assert lk.product("Unclassified Email").queues == ()
    fake.subtypes_with_reasons = True
    rich = c.lookups("RiCH").product("RICH")
    assert rich.types["Inquiry"][0].reason.startswith("Issue is between")
    assert len(c.lookups("RICH").products) == 1


def test_create_for_tests(c):
    r = c.create(title="Test", customer_email="t@example.com", body="Hello",
                 product="RiCH", queue="RiCH Support")
    assert c.get(r.ticket_id).queue == "RiCH Support"


def test_client_needs_configuration(monkeypatch):
    from support_agent import config
    s = config.Settings(database_url="postgresql://x@/y", t2_base_url=None, t2_user=None,
                        t2_password=None)
    monkeypatch.setattr(client_module, "get_settings", lambda: s)
    with pytest.raises(T2Error) as e:
        connect()
    assert e.value.code == "Config.Missing" and not e.value.refused


def test_a_reply_is_emailed_on_its_own_and_reports_its_message_id(c):
    tid = fake.add_email("Renew sender", "Please renew.")
    fake.T[tid]["contact"] = "Active"
    c.update(tid, product="RiCH", queue="RiCH Support", type="Request",
             note=Note("Support Agent: routed", "why"))
    r = c.reply(tid, subject="Renewing your sender name", body="Please send the contract.")
    assert r.email_sent and r.message_id and r.article_id
    assert fake.sent_mail[-1]["subject"].startswith(f"[{fake.T[tid]['number']}]")
    reply = c.get(tid).articles[-1]
    assert reply.visible and reply.via_email


def test_an_internal_note_says_no_email(c):
    tid = fake.add_email("One", "x")
    r = c.update(tid, product="RiCH", queue="RiCH Support", note=Note("s", "b"))
    assert r.email_sent is False and r.message_id is None


def test_reply_refusals_before_sending_are_told_apart(c):
    tid = fake.add_email("One", "x")
    with pytest.raises(T2Error) as e:                     # the sender is still Pending
        c.reply(tid, subject="s", body="b")
    assert e.value.email_not_tried == "contact pending" and not e.value.email_uncertain
    fake.T[tid]["contact"] = "Active"
    with pytest.raises(T2Error) as e:                     # not classified yet
        c.reply(tid, subject="s", body="b")
    assert e.value.email_not_tried == "not classified"
    fake.T[tid].update(requires=False, notifications=False)
    with pytest.raises(T2Error) as e:                     # the customer's notifications off
        c.reply(tid, subject="s", body="b")
    assert e.value.email_not_tried == "notifications off"
    fake.T[tid]["notifications"] = True
    fake.mail_fails.append("not_attempted")
    with pytest.raises(T2Error) as e:                     # another check before sending
        c.reply(tid, subject="s", body="b")
    assert e.value.email_not_tried == "not attempted" and not e.value.email_uncertain
    assert fake.sent_mail == []


def test_assigned_staff_and_history_order(c):
    tid = fake.add_email("One", "x")
    assert c.get(tid).assigned_staff == ""
    fake.T[tid]["assigned_staff"] = "omar@t2.sa"
    c.update(tid, product="RiCH", queue="RiCH Support", note=Note("s", "b"))
    t = c.get(tid)
    assert t.assigned_staff == "omar@t2.sa"
    seq = [h.sequence for h in t.history]
    assert None not in seq and seq == sorted(seq)
    assert [h.action for h in t.history][-2:] == ["Reassigned", "Internal Note Added"]


def test_mark_spam(c):
    tid = fake.add_email("Offer", "Cheap SEO", "seo@spam.example")
    other = fake.add_email("Offer again", "Cheaper SEO", "seo@spam.example")
    r = c.mark_spam(tid)
    assert (r.contact_status, r.tickets_closed, r.ticket_id) == ("Blocked", 2, tid.lower())
    assert fake.T[other]["status"] == "closed successful"
    with pytest.raises(T2Error) as e:                     # not twice
        c.mark_spam(tid)
    assert e.value.code == "ContactMarkSpam.ContactNotPending" and e.value.refused


def test_a_reply_that_may_have_gone_out_is_uncertain(c):
    tid = fake.add_email("One", "x")
    fake.T[tid].update(contact="Active", requires=False)
    fake.mail_fails.extend(["send", "persist"])
    for _ in range(2):
        with pytest.raises(T2Error) as e:
            c.reply(tid, subject="s", body="b")
        assert e.value.email_uncertain and not e.value.email_not_tried
    assert len(fake.sent_mail) == 1                       # the second one did go out
    assert [a for a in c.get(tid).articles if a.visible and not a.from_customer] == []
