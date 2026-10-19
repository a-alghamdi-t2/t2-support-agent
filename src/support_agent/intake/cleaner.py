"""Separate what the customer just wrote from everything else in the body.

Three parts come out: the new text, the signature block, and the quoted history.
Nothing is discarded -- concatenating the three reproduces the input exactly.
That invariant is tested, and it is the reason this file separates rather than
strips: if a split is wrong, nothing is lost, and body_raw keeps the original
regardless.

Makes no decisions. Whether a message is a thank-you, whether it belongs to a
case, whether to reply -- none of that is here.

Every pattern below was taken from real messages, not from a specification.
Arabic mail in particular does things no RFC mentions.

Takes plain text. OTRS may return HTML for some articles; converting that to
text happens before this file sees it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Invisible characters that mail clients insert around mixed Arabic and Latin
# text so it displays in the right order. They are real characters in the
# string, and a line beginning with one does not start with the letter you can
# see. Gmail puts them around the Subject: line inside a forwarded block, which
# is enough to defeat any match anchored at the start of a line.
#
# Stripped for matching only. The output keeps the original text.
BIDI = "\u200e\u200f\u061c\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_BIDI_RE = re.compile(f"[{BIDI}]")

# A signature block begins here. RFC 3676 specifies "-- " with a trailing
# space; plenty of clients send "--" without it, including Gmail. Both count.
_SIGNATURE_MARKERS = {"--", "-- ", "—", "___"}

# Forwarded and original-message separators, from Gmail and Outlook.
_SEPARATOR_RE = re.compile(
    r"^\s*(?:"
    r"-{2,}\s*Forwarded message\s*-{2,}"
    r"|-{3,}\s*Original Message\s*-{3,}"
    r"|-{5,}\s*رسالة (?:أصلية|معاد توجيهها)\s*-{2,}"
    r"|_{10,}"
    r")\s*$",
    re.IGNORECASE,
)

# Gmail's attribution line, English and Arabic. It wraps: in a real fixture the
# line broke after the address and "wrote:" landed on the next line, so a match
# anchored to a single line misses it. These are applied to a window of up to
# three joined lines.
_ATTRIBUTION_RES = [
    # On Tue, Aug 4, 2026 at 4:48 PM Someone <a@b.com> wrote:
    re.compile(r"^\s*On\s+.{10,400}?\bwrote:\s*$", re.DOTALL),
    # في الأربعاء، 29 يوليو 2026، كتب فريق دعم <a@b.com>:
    re.compile(r"^\s*في\s+.{10,400}?كتب.{0,200}:\s*$", re.DOTALL),
    # Outlook header block that follows a separator
    re.compile(r"^\s*From:\s+.{5,400}?\bSent:\s", re.DOTALL),
    re.compile(r"^\s*من:\s+.{5,400}?تم الإرسال:\s", re.DOTALL),
]

_QUOTE_PREFIX_RE = re.compile(r"^\s{0,3}>")

# How many lines an attribution is allowed to span.
_ATTRIBUTION_WINDOW = 3


@dataclass
class CleanedBody:
    """The three parts, plus why the split happened where it did."""

    clean: str
    quoted: str | None
    signature: str | None
    quote_reason: str | None = None
    signature_marker: str | None = None

    def reassemble(self) -> str:
        """Everything, in order. Must equal the input the cleaner was given."""
        return "".join(p for p in (self.clean, self.signature, self.quoted) if p)


def _for_matching(line: str) -> str:
    """A line with directional marks removed, for pattern matching only."""
    return _BIDI_RE.sub("", line)


def _find_quote_start(lines: list[str]) -> tuple[int | None, str | None]:
    """Index of the first line belonging to quoted history, and why.

    Whichever marker appears earliest wins. A reply can carry several -- a quote
    inside a forward inside a reply -- and only the first matters, because
    everything after it is history by definition.
    """
    matchable = [_for_matching(line) for line in lines]
    candidates: list[tuple[int, str]] = []

    # 1. A forwarded or original-message separator.
    for i, line in enumerate(matchable):
        if _SEPARATOR_RE.match(line):
            candidates.append((i, "separator"))
            break

    # 2. A block of '>' quoted lines, together with the attribution line above
    #    it. Gmail wraps that attribution over two lines, so the search looks
    #    back over a small window and joins them before matching.
    for i, line in enumerate(matchable):
        if not _QUOTE_PREFIX_RE.match(line):
            continue
        start = i
        for j in range(max(0, i - _ATTRIBUTION_WINDOW - 1), i):
            window = "\n".join(matchable[j:i]).strip()
            if window and any(rx.match(window) for rx in _ATTRIBUTION_RES):
                # Do not drag preceding blank lines into the quote.
                while j < i and not matchable[j].strip():
                    j += 1
                start = j
                break
        candidates.append((start, "quote_prefix"))
        break

    # 3. An attribution with no '>' beneath it, which happens when a client
    #    quotes by indentation instead of prefixing.
    for i in range(len(matchable)):
        if not matchable[i].strip():
            continue  # an attribution never begins on a blank line
        for span in range(1, _ATTRIBUTION_WINDOW + 1):
            window = "\n".join(matchable[i:i + span]).strip()
            if window and any(rx.match(window) for rx in _ATTRIBUTION_RES):
                candidates.append((i, "attribution"))
                break
        if candidates and candidates[-1][1] == "attribution":
            break

    if not candidates:
        return None, None
    index, reason = min(candidates, key=lambda c: c[0])
    return index, reason


def _find_signature_start(lines: list[str], limit: int) -> tuple[int | None, str | None]:
    """Index of the signature marker, searching only before `limit`.

    The last marker before the quote wins, not the first: a customer who writes
    above a quote that itself contains a signature would otherwise have their
    own text cut at the wrong point.
    """
    found = None
    marker = None
    for i in range(min(limit, len(lines))):
        stripped = _for_matching(lines[i]).strip()
        if stripped in _SIGNATURE_MARKERS:
            found = i
            marker = stripped
    return found, marker


def clean(body: str) -> CleanedBody:
    """Split a message body into new text, signature, and quoted history."""
    if not body:
        return CleanedBody(clean="", quoted=None, signature=None)

    # keepends so the pieces reassemble byte for byte.
    lines = body.splitlines(keepends=True)

    quote_start, quote_reason = _find_quote_start(lines)
    boundary = quote_start if quote_start is not None else len(lines)

    signature_start, signature_marker = _find_signature_start(lines, boundary)

    clean_end = signature_start if signature_start is not None else boundary

    clean_text = "".join(lines[:clean_end])
    signature_text = (
        "".join(lines[signature_start:boundary]) if signature_start is not None else None
    )
    quoted_text = "".join(lines[quote_start:]) if quote_start is not None else None

    return CleanedBody(
        clean=clean_text,
        quoted=quoted_text,
        signature=signature_text,
        quote_reason=quote_reason,
        signature_marker=signature_marker,
    )