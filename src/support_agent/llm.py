"""The language model, through Groq.

Everything the agent asks the model goes through complete(). It takes the
conversation as a list of chat messages and returns the reply: text, or the
tool calls the model wants made. Tests replace complete() with a script, so
nothing above it needs Groq to be tested.

ask_json() is the common case on top of it: a reply that must be one JSON
object matching a pydantic model, with one retry that tells the model what was
wrong.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from support_agent.config import get_settings
from support_agent.logging import get_logger

log = get_logger(__name__)

TIMEOUT = 120
"""Seconds for one call. A decision with four searches behind it is a long
conversation."""

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not be reached, or refused the request."""


class ToolCallFailed(LLMError):
    """The model wrote a tool call Groq could not parse. Repeats identically
    at temperature 0; a little temperature is what breaks it out."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_message(self) -> dict[str, Any]:
        """The reply as the assistant message to put back in the conversation."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name,
                              "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in self.tool_calls]
        return msg


_client = None


def _groq():
    global _client
    s = get_settings()
    if not s.groq_api_key:
        raise LLMError("GROQ_API_KEY is not set")
    key = s.groq_api_key.get_secret_value()
    if _client is None or _client[0] != key:
        from groq import Groq

        _client = (key, Groq(api_key=key, timeout=TIMEOUT, max_retries=2))
    return _client[1]


def complete(messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
             json_mode: bool = False, temperature: float = 0.0) -> Reply:
    """One call to the model."""
    from groq import APIError, BadRequestError

    kwargs: dict[str, Any] = {"model": get_settings().groq_model, "messages": messages,
                              "temperature": temperature}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = _groq().chat.completions.create(**kwargs)
    except BadRequestError as exc:
        text = str(exc)
        if "tool" in text.lower():
            raise ToolCallFailed(text[:500]) from exc
        raise LLMError(text[:500]) from exc
    except APIError as exc:
        raise LLMError(str(exc)[:500]) from exc

    msg = resp.choices[0].message
    calls = []
    for c in msg.tool_calls or []:
        try:
            args = json.loads(c.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(id=c.id, name=c.function.name,
                              arguments=args if isinstance(args, dict) else {}))
    usage = {}
    if resp.usage:
        usage = {"prompt": resp.usage.prompt_tokens or 0,
                 "completion": resp.usage.completion_tokens or 0}
    return Reply(content=msg.content or "", tool_calls=calls, usage=usage)


def parse(text: str, model: type[M]) -> M:
    """A JSON reply, forgiving markdown fences around it."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return model.model_validate_json(text.strip())


def ask_json(messages: list[dict[str, Any]], model: type[M], *,
             trace: list | None = None, step: str = "") -> M:
    """A reply that must be one JSON object of the given shape. One retry,
    told what was wrong. Raises LLMError when the second is no better."""
    convo = list(messages)
    schema = json.dumps(model.model_json_schema(), ensure_ascii=False)
    convo.append({"role": "user", "content": (
        "Reply with a single JSON object and nothing else: no explanation, no "
        "markdown fences. It must match this schema:\n\n" + schema)})
    problem = ""
    for attempt in (1, 2):
        started = time.perf_counter()
        reply = complete(convo, json_mode=True)
        if trace is not None:
            trace.append({"step": f"{step}.call", "attempt": attempt,
                          "ms": int((time.perf_counter() - started) * 1000),
                          "tokens": reply.usage})
        try:
            return parse(reply.content, model)
        except Exception as exc:
            problem = str(exc)[:400]
            log.warning("reply not usable", extra={"step": step, "attempt": attempt,
                                                   "error": problem})
            if trace is not None:
                trace.append({"step": f"{step}.retry", "reason": problem})
            convo += [reply.as_message(),
                      {"role": "user", "content": (
                          f"That was not valid. The problem was: {problem}. "
                          "Return JSON only, with every required field.")}]
    raise LLMError(f"{step}: no usable reply ({problem})")


def ping() -> dict[str, Any]:
    """Is the key valid, and does the model we name exist?"""
    s = get_settings()
    ids = sorted(m.id for m in _groq().models.list().data)
    out: dict[str, Any] = {"models_available": len(ids), "model": s.groq_model,
                           "model_found": s.groq_model in ids}
    if not out["model_found"]:
        out["close_matches"] = [m for m in ids if "gpt-oss" in m or "120b" in m] or ids[:10]
    return out
