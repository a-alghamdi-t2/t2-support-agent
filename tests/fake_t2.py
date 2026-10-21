"""A stand-in for the T2 Ticketing System's TicketAPI, for tests.

Follows what the API does (T2 Agent Ticket API, 20 October 2026): HTTP 200 with
an Error object on refusal; times in Riyadh time; queue, type and subtype
looked up under the target product; the product applied first; Conclusions
at most 500 characters; history entries with "Queue changed from 'X' to 'Y'";
the description listed as the first article when no message holds it;
search needing at least one filter; a reply (IsVisibleForCustomer 1) emailed
during the call, refused for a pending contact or an unclassified ticket, and
nothing of the call saved when the email fails. Plugged into httpx with MockTransport.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import httpx

USER, PASSWORD = "support-agent", "secret"

PRODUCTS = {
    "EMAIL": {"name": "Unclassified Email", "queues": [], "types": {}},
    "RICH": {"name": "RiCH", "queues": ["RiCH Support", "RiCH Billing", "RiCH Technical"],
             "types": {"Inquiry": ["DLR - Call Barring", "User Permissions"],
                       "Request": ["Sender Name - Renewal Contract", "User - Transfer Balance"],
                       "Service Failure": ["Operator - Outage - OTP"]}},
    "OLE5": {"name": "Ole5", "queues": ["Ole5 Support", "Ole5 Technical"],
             "types": {"Inquiry": ["Login"]}},
}
REASONS = {"DLR - Call Barring": "Issue is between the End user and his service provider",
           "User Permissions": "A user reports they cannot perform an action allowed by their role.",
           "Sender Name - Renewal Contract": "The client requests the renewal of an existing contract",
           "User - Transfer Balance": "An User Admin requests to transfer credit between users",
           "Operator - Outage - OTP": "Some operators are down, affecting OTP SMS for all clients"}
STATUSES = [("New", False), ("Open", False), ("Waiting For Customer Response", False),
            ("Waiting for Concerned Department", False), ("Pending Reminder", False),
            ("pending auto close+", False), ("closed successful", True), ("merged", True)]
PRIORITIES = ["Low", "Medium", "High", "Critical"]

T: dict[str, dict] = {}
calls: list[tuple[str, dict]] = []
now = datetime(2026, 10, 19, 9, 0, 0)          # Riyadh time, naive like theirs
fail_next: list[int] = []                      # HTTP status codes to answer with next
subtypes_with_reasons = False                  # RiCH's catalog reasons on its subtypes
email_codes = False                            # their separate refusal codes (A6), once live
mail_fails: list[str] = []                     # "send" (not confirmed) or "persist", next reply
sent_mail: list[dict] = []
_seq = 0


def reset() -> None:
    global _seq, subtypes_with_reasons, email_codes
    T.clear()
    calls.clear()
    fail_next.clear()
    mail_fails.clear()
    sent_mail.clear()
    subtypes_with_reasons = email_codes = False
    _seq = 0


def fmt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def add_email(title: str, body: str, email: str = "client@example.com",
              minutes_ago: int = 30) -> str:
    """An email ticket as their Gmail sync makes it."""
    global _seq
    _seq += 1
    tid = str(uuid.uuid4()).upper()
    at = now - timedelta(minutes=minutes_ago)
    T[tid] = {"id": tid, "number": f"T-{at:%Y%m%d%H%M%S}000-{_seq:04d}", "title": title,
              "product": "EMAIL", "queue": None, "status": "Open", "priority": "Low",
              "type": "Incident", "subtype": None, "reason": None, "requires": True,
              "assigned_staff": None, "email": email, "contact": "Pending",
              "created": at, "resolved": None,
              "description": body, "history": [],
              "threads": [{"id": str(uuid.uuid4()), "customer": True, "channel": "Email",
                           "internal": False, "from": email, "subject": title,
                           "body": body, "at": at}]}
    return tid


def err(code: str, msg: str) -> dict:
    return {"Error": {"ErrorCode": code, "ErrorMessage": msg}}


def _find(ident: str) -> dict | None:
    ident = (ident or "").upper()
    return T.get(ident) or next((t for t in T.values() if t["number"] == ident), None)


def _product(value: str) -> str | None:
    v = (value or "").split("::")[-1].strip().lower()
    return next((c for c, p in PRODUCTS.items() if v in (c.lower(), p["name"].lower())), None)


def _closed(status: str) -> bool:
    return dict(STATUSES)[status]


def _state_type(status: str) -> str:
    if _closed(status):
        return "merged" if "merged" in status.lower() else "closed"
    return "new" if status.lower() == "new" else "open"


def _log(t: dict, action: str, details: str) -> None:
    t["history"].append({"at": now, "action": action, "details": details, "by": USER})


def handle(request: httpx.Request) -> httpx.Response:
    op = request.url.path.rsplit("/", 1)[-1]
    body = json.loads(request.content)
    calls.append((op, body))
    if fail_next:
        return httpx.Response(fail_next.pop(0), text="busy")
    if body.get("UserLogin") != USER or body.get("Password") != PASSWORD:
        return httpx.Response(200, json=err(f"{op}.AccessDenied",
                                            "Invalid integration credentials or the integration is disabled."))
    fn = OPS.get(op)
    if fn is None:
        return httpx.Response(404, text="no such operation")
    return httpx.Response(200, json=fn(body))


def search(b: dict) -> dict:
    if not any([b.get("TicketNumber"), b.get("Queues"), b.get("Products"),
                b.get("RequiresClassification") is not None, b.get("Unassigned") == 1]):
        return err("TicketSearch.FilterRequired",
                   "TicketNumber, queue, product, RequiresClassification or Unassigned is required.")
    rows = list(T.values())
    if b.get("TicketNumber"):
        rows = [t for t in rows if t["number"] == b["TicketNumber"]]
    if b.get("Products"):
        codes = {_product(p) for p in b["Products"]}
        rows = [t for t in rows if t["product"] in codes]
    if b.get("Queues"):
        rows = [t for t in rows if t["queue"] in b["Queues"]]
    if b.get("RequiresClassification") is not None:
        rows = [t for t in rows if t["requires"] == (b["RequiresClassification"] == 1)]
    if b.get("Unassigned") == 1:
        rows = [t for t in rows if not t["queue"] and not t["assigned_staff"]]
    if b.get("States"):
        rows = [t for t in rows if t["status"].lower() in [s.lower() for s in b["States"]]]
    if b.get("StateType"):
        rows = [t for t in rows if _state_type(t["status"]) in b["StateType"]]
    if b.get("TicketCloseTimeNewerDate"):
        lo = datetime.strptime(b["TicketCloseTimeNewerDate"], "%Y-%m-%d %H:%M:%S")
        rows = [t for t in rows if t["resolved"] and t["resolved"] >= lo]
    if b.get("TicketCloseTimeOlderDate"):
        hi = datetime.strptime(b["TicketCloseTimeOlderDate"], "%Y-%m-%d %H:%M:%S")
        rows = [t for t in rows if t["resolved"] and t["resolved"] <= hi]
    ids = [t["id"] for t in sorted(rows, key=lambda t: t["created"])][: b.get("Limit") or 10000]
    return {"TicketID": ids} if ids else {}


def get(b: dict) -> dict:
    t = _find(b.get("TicketID"))
    if not t:
        return err("TicketGet.NotFound", "Ticket was not found.")
    articles = []
    if b.get("AllArticles") == 1:
        threads = sorted(t["threads"], key=lambda a: a["at"])
        articles = [{"ArticleID": a["id"], "SenderType": "customer" if a["customer"] else "agent",
                     "CommunicationChannelID": "1" if a["channel"] == "Email" else "2",
                     "IsVisibleForCustomer": "0" if a["internal"] else "1",
                     "From": a["from"] or "", "To": "", "Cc": "",
                     "Subject": a["subject"] or t["title"], "Body": a["body"].replace("\n", "\r\n"),
                     "ContentType": "text/plain; charset=utf-8", "MessageID": "",
                     "InReplyTo": "", "CreateTime": fmt(a["at"]), "Attachment": []}
                    for a in threads]
        if t["description"] and not any(a["body"].strip() == t["description"].strip()
                                        for a in threads):
            articles.insert(0, {"ArticleID": f"description-{t['id'].replace('-', '').lower()}",
                                "SenderType": "customer", "CommunicationChannelID": "1",
                                "IsVisibleForCustomer": "1", "From": t["email"],
                                "Subject": t["title"], "Body": t["description"],
                                "CreateTime": fmt(t["created"])})
        for i, a in enumerate(articles):
            a["ArticleNumber"] = i + 1
    history = []
    for h in t["history"]:
        e = {"CreateTime": fmt(h["at"]), "Action": h["action"], "Details": h["details"],
             "FromQueue": "", "ToQueue": "", "FromState": "", "ToState": "", "Reason": "",
             "ResponsibleTeam": "", "By": h["by"]}
        import re
        if m := re.search(r"Queue changed from '([^']*)' to '([^']*)'", h["details"]):
            e["FromQueue"], e["ToQueue"] = m.groups()
        if m := re.search(r"Status changed from '([^']*)' to '([^']*)'", h["details"]):
            e["FromState"], e["ToState"] = m.groups()
        e.update(h.get("extra", {}))
        history.append(e)
    fields = [{"Name": "SubType"} | ({"Value": t["subtype"]} if t["subtype"] else {}),
              {"Name": "Conclusions"} | ({"Value": t["reason"]} if t["reason"] else {})]
    p = PRODUCTS[t["product"]]
    return {"Ticket": [{
        "TicketID": t["id"].lower(), "TicketNumber": t["number"], "Title": t["title"],
        "Queue": t["queue"] or "", "State": t["status"], "StateType": _state_type(t["status"]),
        "Type": t["type"], "Priority": t["priority"], "Service": p["name"],
        "ProductCode": t["product"], "SLA": "", "RequiresClassification": int(t["requires"]),
        "CustomerID": ("EMAIL-97D648B5D334 - Pending Email Contacts" if t["contact"] == "Pending"
                       else "CUST-0042"), "CustomerUserID": t["email"],
        "CustomerContactStatus": t["contact"],
        "OrganizationName": ("Pending Email Contacts" if t["contact"] == "Pending"
                             else "Alwaha Clinics"),
        "CustomerTier": "", "IsVipUser": 0, "Created": fmt(t["created"]), "Changed": fmt(now),
        "EscalationResponseTime": "0", "EscalationUpdateTime": "0", "EscalationSolutionTime": "0",
        "DynamicField": fields, "Article": articles, "History": history}]}


def update(b: dict) -> dict:
    t = _find(b.get("TicketID"))
    if not t:
        return err("TicketGet.NotFound", "Ticket was not found.")
    ch = b.get("Ticket") or {}
    target = t["product"]
    if ch.get("Service"):
        target = _product(ch["Service"])
        if not target:
            return err("TicketUpdate.InvalidService", f"Unknown Service: {ch['Service']}")
    p = PRODUCTS[target]
    queue = None
    if ch.get("Queue"):
        queue = next((q for q in p["queues"] if q.lower() == ch["Queue"].strip().lower()), None)
        if not queue:
            return err("TicketUpdate.InvalidQueue", f"Unknown Queue: {ch['Queue']}")
    status = None
    if ch.get("State"):
        status = next((s for s, _ in STATUSES if s.lower() == ch["State"].strip().lower()), None)
        if not status:
            return err("TicketUpdate.InvalidState", f"Unknown State: {ch['State']}")
    priority = None
    if ch.get("Priority"):
        priority = next((x for x in PRIORITIES if x.lower() == ch["Priority"].strip().lower()), None)
        if not priority:
            return err("TicketUpdate.InvalidPriority", f"Unknown Priority: {ch['Priority']}")
    ttype = None
    if ch.get("Type"):
        ttype = next((x for x in p["types"] if x.lower() == ch["Type"].strip().lower()), None)
        if not ttype:
            return err("TicketUpdate.InvalidType", f"Unknown Type: {ch['Type']}")
    fields = {f["Name"]: f.get("Value") for f in b.get("DynamicField") or []}
    subtype = None
    if fields.get("SubType"):
        subtype = next((s for subs in p["types"].values() for s in subs
                        if s.lower() == fields["SubType"].strip().lower()), None)
        if not subtype:
            return err("TicketUpdate.InvalidSubType", f"Unknown SubType: {fields['SubType']}")
    conclusions = fields.get("Conclusions")
    if conclusions is not None and len(conclusions) > 500:
        return err("TicketUpdate.InvalidConclusions", "Conclusions cannot exceed 500 characters.")
    art = b.get("Article")
    if art is not None and not (art.get("Body") or "").strip():
        return err("TicketUpdate.InvalidArticleBody", "Article.Body is required.")
    send = bool(art) and art.get("IsVisibleForCustomer") == 1
    message_id = None
    if send:
        if not (art.get("Subject") or "").strip():
            return err("TicketUpdate.InvalidArticle", "Article.Subject is required.")
        classified = not t["requires"] or bool(ttype or subtype or conclusions is not None)
        if t["contact"] == "Pending":
            return err("TicketUpdate.ContactPending" if email_codes else "TicketUpdate.EmailFailed",
                       "Approve or link this pending contact before replying.")
        if not classified:
            return err("TicketUpdate.NotClassified" if email_codes else "TicketUpdate.EmailFailed",
                       "Classify this customer ticket before replying.")
        failure = mail_fails.pop(0) if mail_fails else None
        if failure == "send":
            return err("TicketUpdate.EmailFailed",
                       "Customer email could not be confirmed. No reply was recorded as sent.")
        message_id = f"<{uuid.uuid4()}@mail.t2.test>"
        sent_mail.append({"to": t["email"], "subject": f"[{t['number']}] {art['Subject']}",
                          "body": art["Body"], "message_id": message_id})
        if failure == "persist":
            return err("TicketUpdate.EmailPersistenceFailed",
                       f"The email was accepted (MessageID: {message_id}), but saving the "
                       "ticket failed. Do not resend automatically; reconcile the ticket first.")

    if target != t["product"]:
        _log(t, "Product Changed",
             f"Product changed from '{PRODUCTS[t['product']]['name']}' to '{p['name']}'.")
        t.update(product=target, subtype=None, reason=None)
        if not queue:
            t["queue"] = None
    if queue and queue != t["queue"]:
        _log(t, "Reassigned", f"Queue changed from '{t['queue'] or ''}' to '{queue}'.")
        t["queue"] = queue
    if status and status != t["status"]:
        _log(t, "Status Changed", f"Status changed from '{t['status']}' to '{status}'.")
        t["status"] = status
        t["resolved"] = now if _closed(status) else None
    if priority and priority != t["priority"]:
        _log(t, "Priority Changed", f"Priority changed from '{t['priority']}' to '{priority}'.")
        t["priority"] = priority
    if ttype:
        t["type"] = ttype
    if subtype:
        t["subtype"] = subtype
    if conclusions is not None:
        t["reason"] = conclusions
    if ttype or subtype or conclusions is not None:
        t["requires"] = False
    art_id = None
    if art:
        art_id = str(uuid.uuid4())
        internal = art.get("IsVisibleForCustomer") != 1
        t["threads"].append({"id": art_id, "customer": False,
                             "channel": "Email" if send else "T2AgentApi",
                             "internal": internal, "from": art.get("From"),
                             "subject": art.get("Subject"), "body": art["Body"].strip(),
                             "at": now + timedelta(seconds=len(t["threads"]))})
        _log(t, "Internal Note Added" if internal else "Reply Added", art["Body"][:500])
    out = {"TicketID": t["id"].lower(), "TicketNumber": t["number"]}
    if art_id:
        out["ArticleID"] = art_id
        out["EmailSent"] = 1 if send else 0
        if message_id:
            out["MessageID"] = message_id
    return out


def create(b: dict) -> dict:
    tk, a = b.get("Ticket") or {}, b.get("Article") or {}
    code = _product(tk.get("Service") or "")
    if not code:
        return err("TicketCreate.InvalidService", f"Unknown Service: {tk.get('Service')}")
    queue = next((q for q in PRODUCTS[code]["queues"]
                  if q.lower() == (tk.get("Queue") or "").strip().lower()), None)
    if not queue:
        return err("TicketCreate.InvalidQueue", f"Unknown Queue: {tk.get('Queue')}")
    tid = add_email(tk["Title"], a["Body"], tk["CustomerUser"], minutes_ago=0)
    T[tid].update(product=code, queue=queue, requires=not tk.get("Type"))
    return {"TicketID": tid.lower(), "TicketNumber": T[tid]["number"],
            "ArticleID": T[tid]["threads"][0]["id"]}


def lookups(b: dict) -> dict:
    codes = list(PRODUCTS)
    if b.get("Product"):
        codes = [c for c in codes if c == _product(b["Product"])]
    out = []
    for c in codes:
        p = PRODUCTS[c]
        types = [{"Name": n, "SubTypes": [
            {"Name": s} | ({"Reason": REASONS[s]} if subtypes_with_reasons and s in REASONS
                           else {}) for s in subs]}
                 for n, subs in p["types"].items()]
        out.append({"Name": p["name"], "Code": c, "Queues": p["queues"], "Types": types,
                    "SLAs": [f"{p['name']} SLA"] if p["queues"] else []})
    return {"Products": out,
            "Statuses": [{"Name": n, "IsClosedState": int(c)} for n, c in STATUSES],
            "Priorities": PRIORITIES}


OPS = {"TicketSearch": search, "TicketGet": get, "TicketUpdate": update,
       "TicketCreate": create, "TicketLookupsGet": lookups}
TRANSPORT = httpx.MockTransport(handle)
