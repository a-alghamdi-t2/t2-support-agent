"""Step 3: the subtype, among the chosen type's subtypes.

Its own call, because a type can have dozens of subtypes and each comes with
a reason to read: given alongside every queue and type in step 2 they would
crowd out the ticket. Here the model sees only the type it chose and what it
concluded about the ticket. A type with one subtype that has its reason needs
no call; where the product's subtypes have no reasons, the model writes the
Conclusions itself.
"""

from __future__ import annotations

from functools import lru_cache

from support_agent import llm, options
from support_agent.config import get_settings
from support_agent.intake.context import TicketContext
from support_agent.orchestrator import validate
from support_agent.orchestrator.contracts import Decision, SubtypeChoice


@lru_cache(maxsize=1)
def _prompt() -> str:
    return (get_settings().prompts_dir / "orchestrator" / "subtype.md").read_text(
        encoding="utf-8")


def run(context: TicketContext, code: str, decision: Decision, trace: list
        ) -> tuple[str | None, str | None, list[str]]:
    """(subtype, conclusions, corrections). Both None when the type has no
    subtypes, or none was chosen."""
    if not decision.type:
        return None, None, []
    offered = options.subtypes(code, decision.type)
    if not offered:
        trace.append({"step": "subtype", "skipped": f"{decision.type} has no subtypes"})
        return None, None, []
    with_reasons = any(o.conclusion for o in offered)
    if len(offered) == 1 and offered[0].conclusion:
        o = offered[0]
        trace.append({"step": "subtype", "subtype": o.value, "skipped": "the only one"})
        return o.value, o.conclusion, []

    lines = ["", "## The subtypes of " + decision.type, ""]
    for o in offered:
        lines.append(f"- {o.value}")
        if o.conclusion:
            lines.append(f"  Reason: {o.conclusion.strip()}")
        if o.description:
            lines.append(f"  When: {o.description.strip()}")
    how = options.guidance().get("subtype")
    if how:
        lines += ["", "## How to choose", "", how.strip()]

    product = options.product(code)
    decided = "\n".join([
        "DECIDED",
        f"  Product:   {product.value} ({code})" if product else f"  Product:   {code}",
        f"  Queue:     {decision.queue}" + (f"  ({decision.queue_reason})"
                                           if decision.queue_reason else ""),
        f"  Type:      {decision.type}",
        f"  Issue:     {decision.issue_type}",
        f"  Summary:   {decision.summary}",
        f"  Reasoning: {decision.reasoning}",
        *(f"  {k}: {v}" for k, v in decision.collected.items()),
    ])
    if not with_reasons:
        decided += ("\n\nNone of these subtypes has a reason in the ticket system, so "
                    "write `conclusions` yourself.")
    messages = [{"role": "system", "content": _prompt() + "\n" + "\n".join(lines)},
                {"role": "user", "content": context.render() + "\n\n" + decided}]

    choice = llm.ask_json(messages, SubtypeChoice, trace=trace, step="subtype")
    picked, conclusions, corrections = validate.subtype(choice, offered)
    trace.append({"step": "subtype", "proposed": choice.model_dump(mode="json"),
                  "subtype": picked.value if picked else None, "corrections": corrections})
    return (picked.value if picked else None), conclusions, corrections
