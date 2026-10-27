"""The rules the model does not get to bend.

The prompts ask for these; this file guarantees them. A model that returns a
product, queue or subtype that is not offered, or a reply with no evidence
behind it, is corrected here rather than trusted.

Corrections are recorded, not silent: every one is returned so the reviewer and
the trace can see what the model actually said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from support_agent import options
from support_agent.intake.context import TicketContext
from support_agent.knowledge.contracts import Retrieval, Verdict, all_evidence, strongest
from support_agent.orchestrator.contracts import (Action, Confidence, Decision,
                                                  ProductChoice, ReplyKind, SubtypeChoice)

ROUTED_STATE = "Waiting for Concerned Department"
"""Where a routed ticket goes when the model leaves it New or Open."""
UNROUTED_STATES = ("new", "open")
"""Statuses that say nobody has the ticket yet: wrong for a ticket just sent
to a team."""


def snap(value: str | None, allowed: list[str]) -> str | None:
    """The allowed value the model meant, or None. Case and spacing are
    forgiven; meaning is not."""
    if not value or not value.strip():
        return None
    # The prompt lists a value with its description as "value — text", and
    # the model sometimes copies the whole line back. Only the long dash is
    # cut: values like "DLR - Call Barring" have a hyphen of their own.
    candidates = [value.strip()]
    head = re.split(r"\s+[—–]\s+", value.strip(), maxsplit=1)[0]
    if head.strip() and head.strip() != candidates[0]:
        candidates.append(head.strip())

    for c in candidates:
        for a in allowed:
            if c.casefold() == a.casefold():
                return a

    # Brackets, punctuation and numbering last ("2. Medium", a missing
    # bracket): letters and digits only, and only when one value matches.
    def loose(text: str) -> str:
        return re.sub(r"[\W_]+", "", re.sub(r"^\s*\d+\s*\.?\s*", "", text).casefold())

    for c in candidates:
        hits = [a for a in allowed if loose(c) and loose(a) == loose(c)]
        if len(hits) == 1:
            return hits[0]
    return None


# ---------------------------------------------------------------------------
# step 1
# ---------------------------------------------------------------------------

@dataclass
class ProductResult:
    code: str | None
    """None when the model named no product we offer."""
    name: str | None
    sure: bool
    spam: bool
    corrections: list[str]


def product(choice: ProductChoice, context: TicketContext) -> ProductResult:
    corrections: list[str] = []
    offered = options.products()
    hit = None
    names = {o.value: o for o in offered}
    codes = {(o.product or ""): o for o in offered}
    picked = snap(choice.product, list(names) + list(codes))
    if picked:
        hit = names.get(picked) or codes.get(picked)
    if hit is None:
        corrections.append(f"product: {choice.product!r} is not one of the products")

    spam = choice.spam
    if spam and context.customer.known:
        # Spam is proposed only for a sender the ticket system does not know:
        # a customer on record writing in is a customer, whatever it reads like.
        corrections.append("spam: the sender is a known customer, not proposed")
        spam = False

    sure = choice.sure
    if sure and choice.other and snap(choice.other, list(names) + list(codes)):
        sure = False
        corrections.append("product: marked sure but named another product, not sure")

    return ProductResult(code=hit.product if hit else None, name=hit.value if hit else None,
                         sure=sure, spam=spam, corrections=corrections)


# ---------------------------------------------------------------------------
# step 2
# ---------------------------------------------------------------------------

def decision(d: Decision, code: str, retrievals: list[Retrieval]) -> tuple[Decision, list[str]]:
    """The decision as it will be stored, and what had to be changed.

    Judged against every search together, not the last one: a ticket asking two
    things gets two searches, and one that found nothing must not veto one that
    did.
    """
    corrections: list[str] = []

    if d.action == Action.SPAM:
        corrections.append("action: spam is decided at the product step, routed instead")
        d.action = Action.ROUTE

    def fallback(kind: str, field: str, value: str | None, *, product: bool) -> str | None:
        """The AI admin's default for a value that is not a real one."""
        o = options.default(kind, code if product else None)
        if o:
            corrections.append(f"{field}: {value!r} is not a {field}, using the default "
                               f"{o.value!r}")
            return o.value
        corrections.append(f"{field}: {value!r} is not a {field} and there is no default, "
                           "left as it is")
        return None

    queues = [o.value for o in options.queues(code)]
    q = snap(d.queue, queues)
    if q is None:
        default_queue = options.default("queue", code)
        q = default_queue.value if default_queue else queues[0]
        corrections.append(f"queue: {d.queue!r} is not one of the product's queues, "
                           f"using {'the default ' if default_queue else ''}{q!r}")
        d.confidence = Confidence.LOW
    d.queue = q

    t = snap(d.type, [t.option.value for t in options.types(code)])
    d.type = t or fallback("type", "type", d.type, product=True)

    states = [o.value for o in options.statuses()]
    d.next_state = snap(d.next_state, states) or fallback(
        "next_state", "status", d.next_state, product=False)

    d.priority = snap(d.priority, [o.value for o in options.priorities()]) or fallback(
        "priority", "priority", d.priority, product=False)

    # No evidence, no assertion -- and inferential evidence is not evidence for
    # this purpose. The rule applies to answering and not to asking: a reply
    # that asks for the error message asserts nothing.
    #
    # On the old system, of 200 replayed tickets, 33 answers rested on
    # inferential evidence alone, and every one examined invented a procedure.
    # Given thin evidence the model reconstructs something plausible rather
    # than declining, and it reads exactly like documentation.
    verdict = strongest(retrievals) if retrievals else Verdict.NOT_IN_KB
    evidence = all_evidence(retrievals)
    why = None
    if d.action == Action.ANSWER and d.reply_kind != ReplyKind.ASK_MORE:
        if not retrievals:
            why = "answer without searching the knowledge base"
        elif all(r.error for r in retrievals):
            why = "answer despite every search failing"
        elif not evidence:
            why = "answer with no evidence"
        elif verdict == Verdict.PARTIAL:
            why = "answer on inferential evidence only"
        if why:
            corrections.append(f"{why}: routed")
            d.action = Action.ROUTE

    if d.action == Action.ROUTE:
        # A reply the model wrote is kept, not sent: if the reviewer changes the
        # draft to an answer, it is there to check and edit. Never sent as it
        # stands, because it is not backed by the knowledge base.
        if d.reply_body and d.reply_body.strip():
            d._held_reply = {
                "kind": (d.reply_kind or ReplyKind.ANSWER).value,
                "body": d.reply_body.strip(),
                "next_state": d.next_state,       # the state the agent chose for it
                "documents": list(dict.fromkeys(e.document for e in evidence)),
                "why": why or "the agent chose to route",
            }
            corrections.append("reply kept for the reviewer, not sent: the action is route")
        d.reply_body = None
        d.reply_kind = None
        if (d.next_state or "").lower() in UNROUTED_STATES and ROUTED_STATE in states:
            corrections.append(f"next_state: {d.next_state} on a routing, "
                               f"using {ROUTED_STATE!r}")
            d.next_state = ROUTED_STATE
    else:
        if not d.reply_body or not d.reply_body.strip():
            corrections.append("answer with an empty reply: routed")
            d.action, d.reply_kind, d.reply_body = Action.ROUTE, None, None
            if ROUTED_STATE in states:
                d.next_state = ROUTED_STATE
        elif d.reply_kind is None:
            d.reply_kind = ReplyKind.ANSWER
            corrections.append("reply_kind was missing, assumed answer")

    if d.reply_kind == ReplyKind.ASK_MORE and not d.missing:
        corrections.append("ask_more with nothing listed as missing")

    if d.urgent and not (d.urgent_reason or "").strip():
        d.urgent_reason = None
    if not d.urgent:
        d.urgent_reason = None

    return d, corrections


# ---------------------------------------------------------------------------
# step 3
# ---------------------------------------------------------------------------

def subtype(choice: SubtypeChoice, offered: list[options.Option]
            ) -> tuple[options.Option | None, str | None, list[str]]:
    """The subtype, and Conclusions: the subtype's reason word for word when
    it has one (their screen expects the catalog's), otherwise the model's
    phrase."""
    corrections: list[str] = []
    by_name = {o.value: o for o in offered}
    picked = snap(choice.subtype, list(by_name))
    if picked is None:
        corrections.append(f"subtype: {choice.subtype!r} is not one of the type's subtypes, "
                           "left empty")
        return None, None, corrections
    o = by_name[picked]
    conclusions = o.conclusion or ((choice.conclusions or "").strip()[:500] or None)
    if conclusions is None:
        corrections.append("conclusions: the subtype has no reason and none was written")
    return o, conclusions, corrections
