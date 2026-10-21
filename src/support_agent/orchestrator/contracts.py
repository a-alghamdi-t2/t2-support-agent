"""What the model returns at each of the three steps.

  1. ProductChoice  which product the email is about, or that it is spam
  2. Decision       within that product: answer or route, queue, type,
                    status, priority, and the reply
  3. SubtypeChoice  the subtype, among the chosen type's subtypes

Code checks each against the values in agent_options (validate.py); what comes
out of that is what gets stored as the draft.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class Action(str, Enum):
    ANSWER = "answer"
    ROUTE = "route"
    SPAM = "spam"


class ReplyKind(str, Enum):
    ANSWER = "answer"
    ASK_MORE = "ask_more"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ProductChoice(BaseModel):
    """Step 1."""

    model_config = ConfigDict(extra="ignore")

    product: str = Field(description="The product's code or name, exactly as listed")
    sure: bool = Field(description="False when another product is nearly as likely")
    reason: str = Field(description="One line: why this product and not the others")
    other: str | None = Field(
        default=None, description="When not sure: the product it might be instead")
    summary: str = Field(description="What the sender is asking, in one or two sentences")
    spam: bool = Field(
        default=False,
        description="True only for mail no customer of any product sent for support: "
                    "marketing, cold sales, newsletters, scams")
    spam_reason: str | None = Field(default=None, description="One line, when spam is true")


class Decision(BaseModel):
    """Step 2. Every field, both actions.

    Only reply_body and reply_kind depend on the action; everything else is
    produced either way, because the ticket leaves the intake regardless and
    someone has to classify it.
    """

    model_config = ConfigDict(extra="ignore")

    action: Action
    reply_kind: ReplyKind | None = None
    reply_body: str | None = None

    queue: str
    queue_reason: str = Field(description="One line: why this queue and not the others")
    type: str
    next_state: str
    priority: str

    issue_type: str = Field(description="Our own label, free text, for analytics")
    summary: str = Field(description="What the customer is asking, in one or two sentences")
    collected: dict[str, str] = Field(
        default_factory=dict,
        description="What the ticket already tells us: error text, sizes, names, dates")
    missing: list[str] = Field(
        default_factory=list,
        description="What is still unknown. What ask_more asks for, or what the "
                    "receiving team will have to find out.")
    note: str | None = Field(
        default=None,
        description="Anything worth telling the reviewer that no other field carries")

    urgent: bool = Field(
        default=False,
        description="True when the ticket needs someone now: judged from impact, not tone")
    urgent_reason: str | None = Field(
        default=None, description="One line saying why it is urgent, when urgent is true")

    confidence: Confidence
    reasoning: str = Field(description="Why this decision, in two or three sentences")

    # Set by the validator, never by the model (a private attribute is not in
    # the schema the model is shown). A reply on a draft that routes -- the
    # model's own choice, or an answer turned into a route for want of
    # evidence: kept for the reviewer, who can make it an answer, check the
    # reply and send it, instead of it being thrown away.
    _held_reply: dict | None = PrivateAttr(default=None)


class SubtypeChoice(BaseModel):
    """Step 3."""

    model_config = ConfigDict(extra="ignore")

    subtype: str = Field(description="One of the listed subtypes, exactly as written")
    why: str = Field(description="One line: why this subtype and not the others")
    conclusions: str | None = Field(
        default=None,
        description="Only when the subtypes listed have no reason: a short phrase for "
                    "the outcome, a category, a dash, then a description")
