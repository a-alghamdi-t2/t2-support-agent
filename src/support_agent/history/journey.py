"""What happened on a closed ticket from start to finish, and why it moved.

Two kinds of output, from two kinds of source:

    path     the exact queues, from the ticket's history. Copied, never
             generated.
    steps, moves, summary
             written by a model after reading the stored timeline, with the
             path given to it as fact to explain, not to work out. The reason
             a person wrote when moving the ticket is in the timeline, so the
             model mostly reports it rather than infers it.

The model is JOURNEY_MODEL; when it fails (not served, an error, no usable JSON
after a second try), JOURNEY_FALLBACK_MODEL answers instead, and the row
records which model wrote it and why the fallback was needed.

Prompt: prompts/history/journey.md, shared by every product.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache

from pydantic import BaseModel, Field

from support_agent import llm
from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import timeline
from support_agent.logging import get_logger

log = get_logger(__name__)

PROMPT_VER = "journey-t2-v1"

TIMELINE_CHARS = 60000
"""A very long ticket keeps its beginning and its end -- the problem and the
resolution -- and every move, and says how much was left out."""
KEEP_FIRST, KEEP_LAST = 10, 20


class JourneyFailed(RuntimeError):
    pass


class Step(BaseModel):
    article: int | None = None
    what: str


class Move(BaseModel):
    from_: str = Field(alias="from")
    to: str
    why: str = "not stated"

    model_config = {"populate_by_name": True}


class Journey(BaseModel):
    steps: list[Step] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    summary: str = ""


def load(closed_ticket_id: int) -> tuple[dict, dict]:
    ticket = postgres.query_one("SELECT * FROM closed_tickets WHERE id = %s",
                                (closed_ticket_id,))
    tl = postgres.query_one("SELECT entries, queue_path FROM ticket_timelines "
                            "WHERE closed_ticket_id = %s", (closed_ticket_id,))
    if ticket is None or tl is None:
        raise JourneyFailed(f"no timeline for closed ticket {closed_ticket_id}")
    return ticket, tl


def render(ticket: dict, tl: dict) -> str:
    """The stored timeline as the model reads it, trimmed if very long."""
    entries = tl["entries"] or []
    text = timeline.render(ticket, entries, tl["queue_path"] or [], names=False)
    if len(text) <= TIMELINE_CHARS or len(entries) <= KEEP_FIRST + KEEP_LAST:
        return text
    head, middle, tail = entries[:KEEP_FIRST], entries[KEEP_FIRST:-KEEP_LAST], entries[-KEEP_LAST:]
    moves = [e for e in middle if e.get("kind") == "queue"]
    gap = {"no": None, "at": "", "kind": "note", "from": None, "subject": "",
           "text": f"… {len(middle) - len(moves)} messages in the middle left out …"}
    return timeline.render(ticket, head + [gap] + moves + tail, tl["queue_path"] or [],
                           names=False)


@lru_cache(maxsize=1)
def _prompt() -> str:
    s = get_settings()
    return (s.prompts_dir / "history" / "journey.md").read_text(encoding="utf-8").replace(
        "{language}", s.journey_language)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _json_of(text: str) -> dict:
    """The JSON object in a reply. A reasoning model may put its thinking in
    <think> tags or wrap the object in a code fence."""
    text = _THINK.sub("", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def _with(model: str, text: str) -> Journey:
    """One model, two tries. Raises JourneyFailed when it cannot produce a
    usable journey."""
    messages = [{"role": "system", "content": _prompt()},
                {"role": "user", "content": text}]
    last = None
    for attempt in (1, 2):
        try:
            reply = llm.complete(messages, model=model, temperature=0.2, max_tokens=8192)
            journey = Journey.model_validate(_json_of(reply.content))
            if not journey.steps or not journey.summary.strip():
                raise ValueError("steps and summary must not be empty")
            return journey
        except llm.LLMError as exc:
            # Not served, or an API error: asking again about JSON won't help.
            raise JourneyFailed(f"{model}: {exc}") from exc
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("journey attempt failed",
                        extra={"model": model, "attempt": attempt, "error": last[:200]})
            messages.append({"role": "user", "content":
                             f"That was not usable ({last[:200]}). "
                             "Reply with the JSON object only."})
    raise JourneyFailed(last or "no usable reply")


def _in_order(journey: Journey) -> Journey:
    """Steps in message order; a step without a number stays after the one
    before it."""
    keyed, last = [], 0
    for i, step in enumerate(journey.steps):
        last = step.article if step.article is not None else last
        keyed.append((last, i, step))
    journey.steps = [s for _, _, s in sorted(keyed, key=lambda k: (k[0], k[1]))]
    return journey


def _align(journey: Journey, route: list[str]) -> Journey:
    """Hold the moves to the recorded path: one per step of the route, in
    order, with its queues exactly as recorded. A reason the model gave is kept
    when its queues match; anything else becomes "not stated"."""
    given = {(m.from_, m.to): m for m in journey.moves}
    journey.moves = [
        Move(**{"from": frm, "to": to,
                "why": ((given[(frm, to)].why or "").strip() or "not stated")
                       if (frm, to) in given else "not stated"})
        for frm, to in zip(route, route[1:])
    ]
    return journey


def extract(ticket: dict, tl: dict) -> tuple[Journey, dict]:
    """One ticket's journey, from the main model or, failing that, the
    fallback."""
    s = get_settings()
    text = render(ticket, tl)
    started = time.perf_counter()
    fallback_reason = None
    try:
        journey, model = _with(s.journey_model, text), s.journey_model
    except JourneyFailed as exc:
        fallback_reason = str(exc)[:500]
        if not s.journey_fallback_model or s.journey_fallback_model == s.journey_model:
            raise
        journey, model = _with(s.journey_fallback_model, text), s.journey_fallback_model
    meta = {"model": model, "fallback_reason": fallback_reason, "prompt_ver": PROMPT_VER,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_chars": len(text)}
    return _align(_in_order(journey), tl["queue_path"] or []), meta


def save(closed_ticket_id: int, route: list[str], journey: Journey, meta: dict) -> int:
    row = postgres.query_one(
        """
        INSERT INTO ticket_journeys (closed_ticket_id, path, steps, moves, summary, model,
                                     fallback_reason, prompt_ver, input_chars, latency_ms)
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (closed_ticket_id) DO UPDATE SET
            path = EXCLUDED.path, steps = EXCLUDED.steps, moves = EXCLUDED.moves,
            summary = EXCLUDED.summary, model = EXCLUDED.model,
            fallback_reason = EXCLUDED.fallback_reason, prompt_ver = EXCLUDED.prompt_ver,
            input_chars = EXCLUDED.input_chars, latency_ms = EXCLUDED.latency_ms,
            created_at = now()
        RETURNING id
        """,
        (closed_ticket_id, route,
         json.dumps([x.model_dump() for x in journey.steps], ensure_ascii=False),
         json.dumps([m.model_dump(by_alias=True) for m in journey.moves], ensure_ascii=False),
         journey.summary, meta["model"], meta["fallback_reason"], meta["prompt_ver"],
         meta["input_chars"], meta["latency_ms"]))
    return row["id"]


def for_ticket(closed_ticket_id: int) -> str:
    """Extract and store one ticket's journey. Returns its summary."""
    ticket, tl = load(closed_ticket_id)
    result, meta = extract(ticket, tl)
    save(closed_ticket_id, tl["queue_path"] or [], result, meta)
    return result.summary
