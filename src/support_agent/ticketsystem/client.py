"""The T2 Ticketing System's TicketAPI: the only way the agent reaches it.

Five operations, each a POST of JSON to <T2_BASE_URL>/<Operation> with the
API user and password in the body:

    TicketSearch     ticket IDs matching filters (the intake, closed tickets)
    TicketGet        one ticket with its articles and history
    TicketUpdate     route a ticket and add a note, in one transaction; or
                     email a reply to the customer
    TicketLookupsGet the products, queues, types, subtypes with their
                     reasons, statuses and priorities the system has now
    TicketCreate     tests only; off in production (AllowTicketCreate)

Their API answers HTTP 200 with an "Error" object when it refuses something;
that becomes T2Error. Times go both ways as "yyyy-MM-dd HH:mm:ss" in Riyadh
time. IDs are GUIDs, returned here in lower case.

It allows 120 calls a minute from one address, so calls from this process are
spaced by T2_MIN_INTERVAL. Reads are retried on a dropped connection, a 429
or a 5xx. Writes are retried only when the request never left (the
connection could not be made): a write that timed out may have been applied,
so it is reported, not repeated; the writer decides.

A reply (an article with IsVisibleForCustomer 1) is emailed during the call,
and if the email fails nothing of that call is saved. Sending is not
idempotent: a reply is never sent twice by this code, whatever the error.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from support_agent.config import Settings, get_settings
from support_agent.logging import get_logger
from support_agent.ticketsystem.models import (
    Article, Attachment, HistoryEntry, Lookups, Note, ProductValues, Subtype, Ticket,
    WriteResult,
)

log = get_logger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONCLUSIONS_MAX = 500
"""Their limit on Conclusions."""

READ_OPS = ("TicketSearch", "TicketGet", "TicketLookupsGet")
READ_ATTEMPTS = 3
RETRY_WAIT = (1.0, 3.0)

# One clock for every client in the process: the limit is per address.
_pace_lock = threading.Lock()
_last_call = 0.0


class T2Error(RuntimeError):
    """The ticket system refused a call, or could not be reached.

    code is theirs ("TicketUpdate.InvalidQueue", "TicketGet.NotFound" ...)
    or ours: "Transport.Unreachable", "Transport.Timeout", "Transport.HTTP<n>",
    "Config.Missing". maybe_applied is True when a write was sent and no
    answer came back: it may or may not have happened.
    """

    def __init__(self, op: str, code: str, message: str, *, maybe_applied: bool = False):
        super().__init__(f"{op}: {code}: {message}")
        self.op = op
        self.code = code
        self.message = message
        self.maybe_applied = maybe_applied

    @property
    def refused(self) -> bool:
        """Their API answered and said no: retrying the same call will not help."""
        return not self.code.startswith(("Transport.", "Config."))

    @property
    def email_not_tried(self) -> str | None:
        """For a reply refused before anything was sent, why: the contact is
        pending, the ticket is not classified, or the customer's email
        notifications are off. A person fixes it and it can be sent again."""
        if self.code in EMAIL_NOT_TRIED:
            return EMAIL_NOT_TRIED[self.code]
        if self.code == "TicketUpdate.EmailFailed":
            # Until their separate codes are live, the message says which.
            text = self.message.lower()
            if "pending contact" in text:
                return "contact pending"
            if "classify this" in text:
                return "not classified"
        return None

    @property
    def email_uncertain(self) -> bool:
        """A reply that may have reached the customer: never send it again
        without a person checking the mailbox's Sent folder."""
        if self.email_not_tried:
            return False
        return self.maybe_applied or self.code in ("TicketUpdate.EmailFailed",
                                                   "TicketUpdate.EmailPersistenceFailed")


EMAIL_NOT_TRIED = {
    "TicketUpdate.ContactPending": "contact pending",
    "TicketUpdate.NotClassified": "not classified",
    "TicketUpdate.NotificationsDisabled": "notifications off",
}
"""Their codes for a reply refused before sending (requested, A6)."""


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:     # no tz database in the image: Riyadh has no DST
        return timezone(timedelta(hours=3))


class T2Client:
    def __init__(self, *, base_url: str, user: str, password: str, timeout: float = 30,
                 verify: bool = True, min_interval: float = 0.6,
                 tz: str = "Asia/Riyadh", intake_product: str = "EMAIL",
                 intake_states: list[str] | None = None, intake_limit: int = 50):
        self.base_url = base_url.rstrip("/")
        self._auth = {"UserLogin": user, "Password": password}
        self._min_interval = min_interval
        self._tz = _zone(tz)
        self.intake_product = intake_product
        self.intake_states = intake_states or ["Open"]
        self.intake_limit = intake_limit
        self._http = httpx.Client(timeout=timeout, verify=verify,
                                  headers={"User-Agent": "t2-support-agent"})

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> "T2Client":
        s = s or get_settings()
        if not s.tickets_configured:
            raise T2Error("Client", "Config.Missing",
                          "T2_BASE_URL, T2_USER and T2_PASSWORD must all be set")
        return cls(base_url=s.t2_base_url, user=s.t2_user,
                   password=s.t2_password.get_secret_value(), timeout=s.t2_timeout,
                   verify=s.t2_verify_tls, min_interval=s.t2_min_interval, tz=s.timezone,
                   intake_product=s.intake_product, intake_states=s.intake_state_list,
                   intake_limit=s.intake_limit)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "T2Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _pace(self) -> None:
        global _last_call
        with _pace_lock:
            wait = _last_call + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_call = time.monotonic()

    def _call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        is_read = op in READ_OPS
        attempts = READ_ATTEMPTS if is_read else 2
        for attempt in range(attempts):
            last = attempt == attempts - 1
            self._pace()
            started = time.perf_counter()
            try:
                resp = self._http.post(f"{self.base_url}/{op}", json={**self._auth, **body})
            except httpx.ConnectError as exc:
                # Nothing reached them: safe to try again, read or write.
                if last:
                    raise T2Error(op, "Transport.Unreachable", str(exc)) from None
                time.sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])
                continue
            except httpx.TimeoutException as exc:
                if not is_read:
                    raise T2Error(op, "Transport.Timeout", f"no answer: {exc}",
                                  maybe_applied=True) from None
                if last:
                    raise T2Error(op, "Transport.Timeout", str(exc)) from None
                time.sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])
                continue
            except httpx.HTTPError as exc:
                if not is_read:
                    raise T2Error(op, "Transport.Failed", str(exc), maybe_applied=True) from None
                if last:
                    raise T2Error(op, "Transport.Failed", str(exc)) from None
                time.sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])
                continue

            ms = round((time.perf_counter() - started) * 1000)
            if resp.status_code == 429 or resp.status_code >= 500:
                if is_read and not last:
                    wait = float(resp.headers.get("Retry-After") or RETRY_WAIT[min(attempt, 1)])
                    time.sleep(min(wait, 30.0))
                    continue
                raise T2Error(op, f"Transport.HTTP{resp.status_code}", resp.text[:300],
                              maybe_applied=not is_read and resp.status_code >= 500)
            if resp.status_code != 200:
                raise T2Error(op, f"Transport.HTTP{resp.status_code}", resp.text[:300])
            try:
                data = resp.json()
            except ValueError:
                raise T2Error(op, "Transport.BadJSON", resp.text[:300]) from None
            err = data.get("Error") if isinstance(data, dict) else None
            if err:
                raise T2Error(op, str(err.get("ErrorCode") or "Unknown"),
                              str(err.get("ErrorMessage") or ""))
            log.debug("t2 call", extra={"op": op, "ms": ms})
            return data if isinstance(data, dict) else {}
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------
    # times
    # ------------------------------------------------------------------

    def _parse_time(self, text: str | None) -> datetime | None:
        if not text or not text.strip():
            return None
        try:
            return datetime.strptime(text.strip(), TIME_FORMAT).replace(tzinfo=self._tz)
        except ValueError:
            log.warning("unreadable time from the ticket system", extra={"value": text})
            return None

    def _format_time(self, when: datetime) -> str:
        if when.tzinfo is None:
            raise ValueError("pass a timezone-aware datetime")
        return when.astimezone(self._tz).strftime(TIME_FORMAT)

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def search(self, *, queues: list[str] | None = None, products: list[str] | None = None,
               states: list[str] | None = None, state_types: list[str] | None = None,
               requires_classification: bool | None = None, unassigned: bool = False,
               ticket_number: str | None = None, closed_after: datetime | None = None,
               closed_before: datetime | None = None, limit: int | None = None) -> list[str]:
        """Ticket IDs, oldest first. Their API needs at least one of
        ticket_number, queues, products, requires_classification, unassigned."""
        body: dict[str, Any] = {}
        if queues:
            body["Queues"] = queues
        if products:
            body["Products"] = products
        if states:
            body["States"] = states
        if state_types:
            body["StateType"] = state_types
        if requires_classification is not None:
            body["RequiresClassification"] = 1 if requires_classification else 0
        if unassigned:
            body["Unassigned"] = 1
        if ticket_number:
            body["TicketNumber"] = ticket_number
        if closed_after:
            body["TicketCloseTimeNewerDate"] = self._format_time(closed_after)
        if closed_before:
            body["TicketCloseTimeOlderDate"] = self._format_time(closed_before)
        if limit:
            body["Limit"] = limit
        ids = self._call("TicketSearch", body).get("TicketID") or []
        if isinstance(ids, str):
            ids = [ids]
        return [str(i).lower() for i in ids]

    def intake(self) -> list[str]:
        """New email tickets waiting to be classified: the intake product,
        no queue and nobody assigned, still marked for classification."""
        return self.search(products=[self.intake_product], requires_classification=True,
                           unassigned=True, states=self.intake_states,
                           limit=self.intake_limit)

    def closed_between(self, start: datetime, end: datetime,
                       products: list[str] | None = None) -> list[str]:
        """Tickets closed in [start, end], for the weekly history update."""
        return self.search(products=products, state_types=["closed"],
                           closed_after=start, closed_before=end)

    def find_number(self, number: str) -> str | None:
        ids = self.search(ticket_number=number, limit=1)
        return ids[0] if ids else None

    def get(self, ticket_id: str, *, articles: bool = True) -> Ticket:
        data = self._call("TicketGet", {"TicketID": ticket_id,
                                        "AllArticles": 1 if articles else 0,
                                        "DynamicFields": 1})
        tickets = data.get("Ticket") or []
        if not tickets:
            raise T2Error("TicketGet", "TicketGet.NotFound", f"no ticket {ticket_id}")
        return self._ticket(tickets[0])

    def lookups(self, product: str | None = None) -> Lookups:
        body = {"Product": product} if product else {}
        return self._lookups(self._call("TicketLookupsGet", body))

    def ping(self) -> None:
        """Raises T2Error when the system cannot be reached or refuses our
        credentials. A search for a number that cannot exist: cheap, and
        needs no ticket to be there."""
        self.search(ticket_number="T-00000000000000000-0000", limit=1)

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------

    def update(self, ticket_id: str, *, product: str | None = None, queue: str | None = None,
               state: str | None = None, type: str | None = None,
               priority: str | None = None, subtype: str | None = None,
               conclusions: str | None = None, note: Note | None = None) -> WriteResult:
        """Change the ticket and add a note, all or nothing.

        The product is applied first; queue, type and subtype are looked up
        under it. Changing the product or priority makes their system apply
        the product's SLA again. Setting a type, subtype or conclusions marks
        the ticket classified, by the API user.
        """
        changes = {k: v for k, v in (("Service", product), ("Queue", queue), ("State", state),
                                     ("Type", type), ("Priority", priority)) if v}
        fields = []
        if subtype:
            fields.append({"Name": "SubType", "Value": subtype})
        if conclusions is not None:
            fields.append({"Name": "Conclusions", "Value": _fit(conclusions, CONCLUSIONS_MAX)})
        body: dict[str, Any] = {"TicketID": ticket_id}
        if changes:
            body["Ticket"] = changes
        if fields:
            body["DynamicField"] = fields
        if note:
            body["Article"] = self._article(note)
        if len(body) == 1:
            raise ValueError("nothing to update")
        return self._write_result(self._call("TicketUpdate", body))

    def reply(self, ticket_id: str, *, subject: str, body: str) -> WriteResult:
        """Email a reply to the ticket's customer, on its own: nothing else
        changes, so a failed email undoes nothing else. Never retried here."""
        return self.update(ticket_id, note=Note(subject, body, visible=True))

    def create(self, *, title: str, customer_email: str, body: str, product: str,
               queue: str | None = None, subject: str | None = None,
               state: str | None = None, priority: str | None = None,
               type: str | None = None) -> WriteResult:
        """Tests only: their production keeps TicketCreate switched off."""
        ticket = {k: v for k, v in (("Title", title), ("CustomerUser", customer_email),
                                    ("Service", product), ("Queue", queue), ("State", state),
                                    ("Priority", priority), ("Type", type)) if v}
        article = {"Subject": subject or title, "Body": body, "IsVisibleForCustomer": 1,
                   "ContentType": "text/plain; charset=utf-8", "SenderType": "customer",
                   "From": customer_email}
        return self._write_result(self._call("TicketCreate",
                                             {"Ticket": ticket, "Article": article}))

    # ------------------------------------------------------------------
    # their JSON -> ours
    # ------------------------------------------------------------------

    @staticmethod
    def _article(note: Note) -> dict[str, Any]:
        article = {"Subject": _fit(note.subject, 500), "Body": note.body,
                   "ContentType": "text/plain; charset=utf-8",
                   "IsVisibleForCustomer": 1 if note.visible else 0}
        if note.sender:
            article["From"] = note.sender
        return article

    @staticmethod
    def _write_result(data: dict[str, Any]) -> WriteResult:
        sent = data.get("EmailSent")
        return WriteResult(ticket_id=str(data.get("TicketID") or "").lower(),
                           number=str(data.get("TicketNumber") or ""),
                           article_id=(str(data["ArticleID"]).lower()
                                       if data.get("ArticleID") else None),
                           email_sent=None if sent is None else _flag(sent),
                           message_id=str(data["MessageID"]) if data.get("MessageID") else None)

    def _ticket(self, t: dict[str, Any]) -> Ticket:
        fields = {str(f.get("Name")): str(f.get("Value") or "")
                  for f in t.get("DynamicField") or [] if f.get("Name")}
        customer_email = _s(t, "CustomerUserID").lower()
        return Ticket(
            id=_s(t, "TicketID").lower(), number=_s(t, "TicketNumber"), title=_s(t, "Title"),
            product=_s(t, "Service"), product_code=_s(t, "ProductCode"), queue=_s(t, "Queue"),
            state=_s(t, "State"), state_type=_s(t, "StateType").lower(), type=_s(t, "Type"),
            priority=_s(t, "Priority"), sla=_s(t, "SLA"),
            subtype=fields.get("SubType", ""), conclusions=fields.get("Conclusions", ""),
            requires_classification=_flag(t.get("RequiresClassification")),
            customer_id=_s(t, "CustomerID"), customer_email=customer_email,
            customer_status=_s(t, "CustomerContactStatus"),
            customer_org=_s(t, "OrganizationName"), customer_tier=_s(t, "CustomerTier"),
            is_vip=_flag(t.get("IsVipUser")),
            created=self._parse_time(t.get("Created")),
            changed=self._parse_time(t.get("Changed")),
            dynamic_fields=fields,
            articles=tuple(self._article_from(a) for a in t.get("Article") or []),
            history=tuple(self._history_from(h) for h in t.get("History") or []),
        )

    def _article_from(self, a: dict[str, Any]) -> Article:
        return Article(
            id=_s(a, "ArticleID").lower(), number=int(a.get("ArticleNumber") or 0),
            from_customer=_s(a, "SenderType").lower() == "customer",
            via_email=_s(a, "CommunicationChannelID") == "1",
            visible=_flag(a.get("IsVisibleForCustomer")),
            sender=_s(a, "From"), to=_s(a, "To"), cc=_s(a, "Cc"), subject=_s(a, "Subject"),
            body=_s(a, "Body").replace("\r\n", "\n"),
            created=self._parse_time(a.get("CreateTime")), message_id=_s(a, "MessageID"),
            attachments=tuple(Attachment(
                id=_s(x, "AttachmentID").lower(), filename=_s(x, "Filename"),
                content_type=_s(x, "ContentType"),
                size=int(x["FilesizeRaw"]) if str(x.get("FilesizeRaw") or "").isdigit() else None,
                url=_s(x, "ContentAlternative")) for x in a.get("Attachment") or []),
        )

    def _history_from(self, h: dict[str, Any]) -> HistoryEntry:
        return HistoryEntry(
            at=self._parse_time(h.get("CreateTime")), action=_s(h, "Action"),
            details=_s(h, "Details"), by=_s(h, "By"),
            from_queue=_s(h, "FromQueue"), to_queue=_s(h, "ToQueue"),
            from_state=_s(h, "FromState"), to_state=_s(h, "ToState"),
            reason=_s(h, "Reason"), team=_s(h, "ResponsibleTeam"))

    @staticmethod
    def _lookups(data: dict[str, Any]) -> Lookups:
        products = []
        for p in data.get("Products") or []:
            types: dict[str, tuple[Subtype, ...]] = {}
            for ty in p.get("Types") or []:
                subs = []
                for s in ty.get("SubTypes") or []:
                    # Today a list of names; once their change lands, objects
                    # with the reason that belongs to each.
                    if isinstance(s, dict):
                        subs.append(Subtype(_s(s, "Name"), _s(s, "Reason") or None))
                    elif s:
                        subs.append(Subtype(str(s)))
                types[_s(ty, "Name")] = tuple(subs)
            products.append(ProductValues(
                name=_s(p, "Name"), code=_s(p, "Code"),
                queues=tuple(str(q) for q in p.get("Queues") or []), types=types,
                slas=tuple(str(x) for x in p.get("SLAs") or [])))
        statuses = tuple((_s(s, "Name"), _flag(s.get("IsClosedState")))
                         for s in data.get("Statuses") or [])
        return Lookups(products=tuple(products), statuses=statuses,
                       priorities=tuple(str(x) for x in data.get("Priorities") or []))


def _s(d: dict[str, Any], key: str) -> str:
    v = d.get(key)
    return "" if v is None else str(v).strip()


def _flag(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true")


def _fit(text: str, limit: int) -> str:
    """Their limits are hard: cut at a word, with an ellipsis, rather than be refused."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.")
    log.warning("text cut to the ticket system's limit", extra={"limit": limit,
                                                               "length": len(text)})
    return cut + "…"


def connect() -> T2Client:
    """A client from the settings in force. Use it as a context manager:

        with ticketsystem.connect() as t2:
            for ticket_id in t2.intake(): ...
    """
    return T2Client.from_settings()
