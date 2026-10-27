"""Step 1: which product the email is about, or whether it is spam.

The model sees the email, the products with their descriptions and queues, and
what this sender and their company wrote about before. No knowledge base: the
product's own is searched in step 2, once there is a product.
"""

from __future__ import annotations

from functools import lru_cache

from support_agent import llm, options
from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.intake.context import TicketContext
from support_agent.orchestrator import validate
from support_agent.orchestrator.contracts import ProductChoice

FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.sa", "outlook.com",
    "outlook.sa", "live.com", "msn.com", "yahoo.com", "ymail.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "mail.com", "gmx.com",
})
"""Domains anyone can have an address at: the company of a sender there says
nothing, so only the address itself counts."""


@lru_cache(maxsize=1)
def _prompt() -> str:
    return (get_settings().prompts_dir / "orchestrator" / "product.md").read_text(
        encoding="utf-8")


def _products_block() -> str:
    lines = ["", "## The products", ""]
    for o in options.products():
        queues = ", ".join(q.value for q in options.queues(o.product or "")) or "none yet"
        lines += [f"### {o.product} — {o.value}", "",
                  (o.description or "(no description yet)").strip(), "",
                  f"Queues: {queues}", ""]
    how = options.guidance().get("product")
    if how:
        lines += ["## How to choose", "", how.strip(), ""]
    return "\n".join(lines)


def sender_history(context: TicketContext) -> str | None:
    """What this address, and others at the same company, wrote about before:
    approved drafts of ours, and closed tickets of the history."""
    email = (context.requester_email or "").strip().lower()
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1]
    by_domain = domain not in FREE_MAIL
    intake = get_settings().intake_product.upper()

    rows = postgres.query(
        """
        SELECT lower(t.requester_email) = %(email)s AS same,
               CASE WHEN d.action = 'spam' AND coalesce(r.final->>'action', 'spam') = 'spam'
                    THEN 'spam'
                    ELSE upper(coalesce(r.final->>'product', d.product)) END AS product
        FROM drafts d
        JOIN reviews r ON r.draft_id = d.id
        JOIN tickets t ON t.id = d.ticket_id
        WHERE d.status = 'approved' AND t.id <> %(ticket)s
          AND (lower(t.requester_email) = %(email)s
               OR (%(by_domain)s AND split_part(lower(t.requester_email), '@', 2) = %(domain)s))
        UNION ALL
        SELECT lower(c.requester_email) = %(email)s,
               CASE WHEN c.contact_status = 'Blocked' THEN 'spam'
                    ELSE upper(c.product_code) END
        FROM closed_tickets c
        WHERE (c.contact_status = 'Blocked'
               OR upper(coalesce(c.product_code, '')) NOT IN ('', %(intake)s))
          AND (lower(c.requester_email) = %(email)s
               OR (%(by_domain)s AND split_part(lower(c.requester_email), '@', 2) = %(domain)s))
        """,
        {"email": email, "domain": domain, "by_domain": by_domain,
         "ticket": context.ticket_id, "intake": intake})
    if not rows:
        return None

    def counts(same: bool) -> str:
        seen: dict[str, int] = {}
        for r in rows:
            if r["same"] == same and r["product"]:
                seen[r["product"]] = seen.get(r["product"], 0) + 1
        return ", ".join(f"{p} {n}" for p, n in sorted(seen.items(), key=lambda x: -x[1]))

    lines = ["SENDER HISTORY"]
    if mine := counts(True):
        lines.append(f"  This address before:  {mine}")
    if by_domain and (theirs := counts(False)):
        lines.append(f"  Others at {domain}:  {theirs}")
    return "\n".join(lines) if len(lines) > 1 else None


def run(context: TicketContext, trace: list, *, spam_check: str | None = None
        ) -> tuple[ProductChoice, validate.ProductResult]:
    """The product, checked. One retry when the model names a product that is
    not offered; raises llm.LLMError when the second is no better.
    spam_check: what history/spam.py found, when it has something to say."""
    user = context.render()
    history = sender_history(context)
    if history:
        user += "\n\n" + history
    if spam_check:
        user += "\n\n" + spam_check
    trace.append({"step": "product.input", "sender_history": history,
                  "spam_check": spam_check,
                  "products": [o.product for o in options.products()]})

    messages = [{"role": "system", "content": _prompt() + "\n" + _products_block()},
                {"role": "user", "content": user}]
    for attempt in (1, 2):
        choice = llm.ask_json(messages, ProductChoice, trace=trace, step="product")
        result = validate.product(choice, context)
        trace.append({"step": "product", "attempt": attempt,
                      "proposed": choice.model_dump(mode="json"),
                      "product": result.code, "sure": result.sure, "spam": result.spam,
                      "corrections": result.corrections})
        if result.code:
            return choice, result
        codes = ", ".join(o.product or "" for o in options.products())
        messages = messages + [
            {"role": "assistant", "content": choice.model_dump_json()},
            {"role": "user", "content": f"{choice.product!r} is not one of the products. "
                                        f"Choose one of: {codes}."}]
    raise llm.LLMError(f"product: no listed product chosen ({choice.product!r})")
