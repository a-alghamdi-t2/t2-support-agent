"""The text a ticket is compared on when looking for similar tickets.

The customer's first article, with the mail around the question removed.
Deterministic: the same body and the same boilerplate table always give the
same text. Nothing here calls a model.

Separate from cleaner.py, which answers a different question. cleaner.py
separates what the customer just wrote from the quoted history, for a person to
read, and cuts at the first forward. Here the forward is often the substance --
a cover note saying "please see below" above the actual problem -- so it is
kept, and only its headers go.

What goes, in order:

  1. image tags, mailto wrappers, links, email addresses, phone numbers,
     zero-width characters
  2. forwarding headers and separators (the Subject line is kept: it is often
     the best one-line description of the problem)
  3. lines listed in boilerplate_lines for this sender, their domain, or our
     own staff -- signatures and footers, learned from history
  4. disclaimer paragraphs, recognised by their wording
  5. greetings and sign-offs, when they are the whole line
  6. lines garbled at export into runs of question marks
  7. repeats: a forward chain quotes the same paragraph several times

What stays is never rewritten. Usernames like rsbuqami@GSTAT have no dot after
the @ and are not email addresses, so they survive step 1 -- they are exactly
what makes two tickets about the same account similar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)

SEARCH_VER = "search-v17"

# Our own domains. A line our staff repeat in their replies is a signature,
# and customers quote those replies back inside their forwards.
INTERNAL_DOMAINS: tuple[str, ...] = ("t2.sa", "t2saas.sa")

# A line is boilerplate for a sender or a domain when it appears in at least
# MIN_SEEN of their tickets and in at least MIN_SHARE of them. The share is what
# keeps real text: a disclaimer is in nearly every ticket from its mail server,
# an error message the same organisation reports is in a handful.
MIN_SEEN = 3
MIN_SHARE = 0.3

# Lines shorter than this are never learned as boilerplate. Too little to tell
# a signature from a word the customer happened to repeat.
MIN_LINE = 4


# ---------------------------------------------------------------------------
# 1. patterns inside a line
# ---------------------------------------------------------------------------

_INLINE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\[cid:[^\]]*\]", re.I), " "),
    # [Ticket#2026081782000109] in a quoted subject: a unique number, which
    # can only make two tickets look different, never alike.
    (re.compile(r"\[Ticket#\d+\]", re.I), " "),
    (re.compile(r"<mailto:[^>]*>", re.I), ""),
    (re.compile(r"\bmailto:\S*", re.I), " "),
    (re.compile(r"\[\d{1,2}\](?=\s*(?:mailto:|https?:|\[\d|$))"), " "),
    (re.compile(r"(?<=:)\s*\[\d{1,2}\]"), " "),
    (re.compile(r"[\[<][^\]>\n]{0,80}\.(?:png|jpe?g|gif|bmp|webp)[\]>]", re.I), " "),
    (re.compile(r"<(?:tel|callto|sip):[^>]*>", re.I), ""),
    (re.compile(r"<https?://[^>]*>", re.I), " "),
    (re.compile(r"https?://\S+|www\.\S+", re.I), " "),
    # A dot after the @ is what makes it an address. rsbuqami@GSTAT has none.
    (re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"), " "),
    # [Company Logo], [Facebook], [Green Turtle Icon]. Not [error 500]: a
    # bracket with a digit in it may be something the customer wrote.
    (re.compile(r"\[[^\]\d\n]{1,40}\]"), " "),
    (re.compile(r"\[[^\]\n]{0,120}description automatically generated[^\]\n]*\]", re.I), " "),
    # Saudi phone numbers: 05 mobile or 01 landline, after +966, 00966 or a
    # leading 0. Anchored on that 5 or 1 so a document number such as
    # 001-24-003143 -- exactly what makes two tickets alike -- is left alone.
    (re.compile(r"(?<![\w+])\+?(?:00966|966|0)[\s-]?[15]\d(?:[\s-]?\d){6,8}(?!\w)"), " "),
    (re.compile(r"\bEx(?:t)?\.?\s*:?\s*\d{3,6}\b", re.I), " "),
]

_INVISIBLE = re.compile("[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
                        "\u061c\u202a-\u202e\u2066-\u2069\u0640]")
_SPACES = re.compile(r"[ \t\u00a0]+")

# What is left of a contact line once its number or address has gone.
_EMPTY_LABEL = re.compile(
    r"^(?:tel|telephone|mobile|mob|phone|fax|email|e-mail|web|website|ext|"
    r"[empatw]|هاتف|جوال|الجوال|فاكس|البريد|البريد الإلكتروني|تحويلة)\.?\s*:?[\s,.:،]*$",
    re.I,
)


# *From:* and *Subject:* -- a bold header, as some clients render one when an
# HTML mail is turned into text. The asterisks go so the header is recognised.
_BOLD_LABEL = re.compile(r"^\*([^*:\n]{1,20}:)\*")


def _inline(line: str) -> str:
    line = _INVISIBLE.sub("", line)
    for rx, repl in _INLINE:
        line = rx.sub(repl, line)
    line = re.sub(r"^\s*(?:>\s?)+", "", line)          # quote prefixes
    line = re.sub(r"[<(\[]\s*[>)\]]", " ", line)          # < > and ( ) left empty
    # Footnote numbers left alone on a line once the links after them are
    # gone: "[6]<https://...>" becomes "[6]" only after the link rule runs.
    line = re.sub(r"^(?:\s*\[\d{1,2}\])+\s*$", "", line)
    line = _BOLD_LABEL.sub(r"\1", line.lstrip())
    return _SPACES.sub(" ", line).strip()


def line_key(line: str) -> str:
    """The form a line is compared in: case, spacing and trailing punctuation
    forgiven. Used both to learn boilerplate and to remove it, so the two
    always agree."""
    return re.sub(r"[\s,.:;،؛!]+$", "", line.casefold()).strip()


# ---------------------------------------------------------------------------
# 2. forwarding headers
# ---------------------------------------------------------------------------

_HEADER_START = re.compile(r"^(?:from|من)\s*:", re.I)
_HEADER_FIELD = re.compile(
    r"^(?:from|sent|to|cc|bcc|date|subject|من|تم الإرسال|التاريخ|إلى|الى|"
    r"نسخة|نسخة إلى|الموضوع)\s*:", re.I)
_SUBJECT = re.compile(r"^(?:subject|الموضوع)\s*:\s*(.*)$", re.I)
_SUBJECT_PREFIX = re.compile(r"^(?:(?:re|fw|fwd|رد|إعادة توجيه|تحويل)\s*:\s*)+", re.I)
_SEPARATOR = re.compile(
    r"^(?:[-_=*]{5,}|-+\s*(?:forwarded message|original message)\s*-+|"
    r"-+\s*رسالة (?:أصلية|معاد توجيهها)\s*-+)$", re.I)
# "On ... wrote:", and Arabic Gmail in both its forms: "في ... كتب:" and
# "في ... تمت كتابة ما يلي بواسطة ...:".
_ATTRIBUTION = re.compile(
    r"^(?:on\s.{5,300}\swrote:|في\s.{5,300}(?:كتب|تمت كتابة ما يلي بواسطة).{0,200}:)[.\s]*$", re.I)


_VIA_GROUP = re.compile(r"\svia\s[^<]{1,60}<", re.I)
# "++ @T2 Support" -- a staff member adding a recipient, not a message.
# Read on the raw line, where the address is still there to recognise.
_ADD_RECIPIENT = re.compile(
    r"^\s*\+{1,2}\s*(?:@\S.{0,80}|[^<\n]{1,40}<[^>\n]*@[^>\n]*>.{0,40})\s*$")
_SIGNATURE_MARK = re.compile(r"^(?:--|-- |—|___)$")
_INTERNAL = re.compile(
    r"@(?:" + "|".join(re.escape(d) for d in INTERNAL_DOMAINS) + r")\b", re.I)


def candidate_lines(body: str, trace: list | None = None,
                    skip_staff: bool = True) -> list[str]:
    """Steps 1 and 2: the lines boilerplate is learned from and removed from.

    Headers are recognised on the cleaned line, but whether a quoted message
    came from us is read from the raw one, before its address was removed.

    Three things are skipped whole rather than line by line:

      - a header block, From: to the first blank line. Outlook wraps a long
        To: list and the continuation has no label to recognise it by. The
        Subject's text is kept, once: it is often the best one-line summary.
      - a signature, from a "--" line to the next quoted message.
      - a message one of our own staff wrote, quoted back in a forward. That is
        our answer, not the customer's question.

    Public because the script that builds boilerplate_lines must see exactly
    the lines the cleaner will later compare against.
    """
    raw_lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    in_header = False
    skipping = False          # signature, or a message from us
    skip_why = ""             # which, for the trace

    lines = [_inline(r) for r in raw_lines]
    i = -1
    while i + 1 < len(raw_lines):
        i += 1
        raw, line = raw_lines[i], lines[i]
        # A line with no letters left, such as "," once the links around it are
        # gone -- but not "--" or a separator, which mean something below.
        if not re.search(r"\w", line) and not (_SIGNATURE_MARK.match(line) or _SEPARATOR.match(line)):
            line = ""
        if _EMPTY_LABEL.match(line) or _LABEL_LINE.match(line) or _ADD_RECIPIENT.match(raw):
            line = ""

        if _SEPARATOR.match(line):
            in_header = False
            continue

        # An attribution can wrap: "On ... Ahmed Bin Yahya <" then
        # "Ahmed.BinYahya@...> wrote:". Try it alone, then joined with the
        # next one and two lines, the way cleaner.py does.
        span = 0
        for n in (1, 2, 3):
            joined = " ".join(l for l in lines[i:i + n] if l)
            if joined and _ATTRIBUTION.match(joined):
                span = n
                break
        if span:
            in_header = False
            # "'hassan izzeldin' via Operation <operation@t2.sa> wrote:" -- a
            # customer writing to one of our mailing groups. The address is
            # ours, the author is not, so the message is kept.
            attribution = " ".join(raw_lines[i:i + span])
            skipping = (skip_staff and bool(_INTERNAL.search(attribution))
                        and not _VIA_GROUP.search(attribution))
            skip_why = "staff quote"
            i += span - 1
            continue

        if _HEADER_START.match(line):
            in_header = True
            skipping = skip_staff and bool(_INTERNAL.search(raw))
            skip_why = "staff quote"
            continue

        if in_header:
            if not line:
                in_header = False
                if not skipping:
                    out.append("")
                continue
            m = _SUBJECT.match(line)
            if m and not skipping:
                subject = _SUBJECT_PREFIX.sub("", m.group(1)).strip()
                if subject:
                    out.append(subject)
            continue

        if skipping:
            if trace is not None and line:
                trace.append((skip_why, line))
            continue
        if _SIGNATURE_MARK.match(line):
            skipping = True
            skip_why = "after --"
            continue
        m = _SUBJECT.match(line)
        if m:
            # A subject outside a header block: keep its text, not its label.
            subject = _SUBJECT_PREFIX.sub("", m.group(1)).strip()
            if subject:
                out.append(subject)
            continue
        if _HEADER_FIELD.match(line):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# 4-6. disclaimers, greetings, garbled lines
# ---------------------------------------------------------------------------

# One of these alone marks a paragraph as a disclaimer.
_STRONG = [re.compile(p, re.I) for p in (
    r"\bdisclaimer\b", r"[إا]خلاء\s*(?:ال)?مسؤولي[ةه]",
    r"intended recipient", r"المتلقي المقصود", r"المرسل إليه",
)]
# Two of these together do. Alone, each is something a customer could write
# about their own documents -- "سرية" is a real classification in Tarasol.
_WEAK = [re.compile(p, re.I) for p in (
    r"confidential", r"privileged", r"سري[ةه]", r"unauthori[sz]ed",
    r"virus", r"ف[اي]?يروس", r"recipient", r"liability", r"مسؤولية",
    r"تعاقدية", r"legally", r"قانون",
)]


# Lines that are a label, not a message: information classification, the
# print-the-environment footer. Removed as single lines, never as a paragraph --
# a label directly above the customer's text, with no blank line between, would
# otherwise take the text with it.
_LABEL_LINE = re.compile(
    r"^(?:.{0,40}\b)?(?:information\s+)?classification\s*:.*$"
    r"|^التصنيف\s*:.*$"
    r"|^classified as\b.*$"
    r"|^internal use(?: only)?$|^this content is (?:restricted|internal|public|confidential)\b.*$"
    r"|^this (?:e-?mail|document|message)(?:\s*/\s*(?:e-?mail|document|message))?"
    r" (?:has been|is) classified\b.*$"
    r"|^(?:confidential|restricted|internal|public|sensitive)"
    r"(?:\s*[–—-]?\s*(?:مقيد|داخلي|عام|سري|حساس|\?+))?$"
    r"|^(?:مقيد|سري|داخلي)\s*[–—-]?\s*(?:confidential|restricted|internal)?$"
    r"|^sent from (?:my )?(?:outlook|iphone|ipad|android|samsung|mail for windows|"
    r"yahoo mail|mobile|huawei)\b.*$"
    r"|^get outlook for (?:ios|android).*$"
    r"|^you don't often get email from.*$|^learn why this is important.*$"
    r"|^(?:أُ?رسلت|ارسلت|مرسل) من (?:ال ?)?(?:iphone|ipad|android|جوالي|هاتفي|الآيفون|الايفون).*$"
    r"|^(?:restricted|internal|public|confidential|sensitive)\s*[–—-]\s*"
    r"(?:مقيد|داخلي|عام|سري|حساس)$"
    r"|^.{0,40}consider the environment before printing.*$"
    # Mail gateway banners, added by the customer's own mail system.
    r"|^.{0,30}\b(?:caution|warning|external)\b.{0,40}originated from outside.*$"
    r"|^\[?external(?: email| sender)?\]?\s*:?$"
    r"|^this (?:message|email) is (?:marked|classified as)\s+"
    r"(?:public|internal|restricted|confidential|sensitive).*$"
    # A Teams invitation pasted into the mail.
    r"|^_*\s*microsoft teams(?: meeting)?\s*_*$"
    r"|^\*?join on your computer.*$"
    r"|^click here to join the meeting.*$"
    r"|^meeting id\s*:.*$|^passcode\s*:.*$"
    r"|^download teams.*$|^\|?\s*join on(?: the web)?$|^the web$"
    r"|^learn more\s*\|\s*meeting options$"
    r"|^join with a video conferencing device.*$|^need help\?.*$"
    r"|^for organizers\s*:.*$|^microsoft teams need help\?.*$|^join the meeting now$"
    r"|^ticket information$|^معلومات التذكرة$"
    r"|^view (?:ticket|request)\s*/\s*عرض (?:التذكرة|الطلب)$"
    r"|^you can view your (?:ticket|request) to track updates.*$"
    r"|^يمكنك متابعة التحديثات والقيام بتغي(?:ي)?رات على (?:الطلب|التذكرة).*$"
    r"|^انطلاقا[ًا]? من قيمنا بالحفاظ على البيئة.*$",
    re.I,
)


_ABOUT_THIS_MAIL = re.compile(
    r"this (?:e-?mail|message|communication|transmission)|e-?mail (?:message|and any)|"
    r"هذ[اه] (?:البريد|الرسالة|الإيميل)|البريد الإلكتروني|بريد إلكتروني|الرسالة ومرفقاتها",
    re.I)
_DISCLAIMER_START = re.compile(r"^\W*(?:disclaimer|[إا]خلاء\s*(?:ال)?مسؤولي)", re.I)


def _is_disclaimer(paragraph: str) -> bool:
    weak = sum(1 for rx in _WEAK if rx.search(paragraph))
    # A short paragraph is a disclaimer only when it says so, or is dense with
    # the wording. "Intended recipient field is empty so we cannot send the
    # transaction" is a customer describing a field in Tarasol, not a footer.
    if len(paragraph) < 150:
        return bool(_DISCLAIMER_START.match(paragraph)) or weak >= 3
    if any(rx.search(paragraph) for rx in _STRONG):
        return True
    # Without a strong marker, the words alone are not enough: an audit
    # finding about "unauthorized access" and "legal" obligations has them too.
    # A disclaimer is always about the mail it is attached to.
    return weak >= 2 and bool(_ABOUT_THIS_MAIL.search(paragraph))


_GREETING = re.compile(
    r"^(?:"
    r"تحي[ةه] طيب[ةه](?: وبعد)?|السلام عليكم(?: ورحمة الله(?: وبركاته)?)?|"
    r"مع (?:خالص |فائق )?التحي[ةه](?: والتقدير)?|(?:و ?لكم |مع )?تحياتي|"
    r"و?تفضلوا بقبول .{0,30}|و?شاكرين (?:لكم )?.{0,20}تعاونكم|"
    r"و?شكر[اًا]?(?: لكم)?(?: جزيلا)?|السادة الكرام|السادة|الزملاء الكرام|"
    r"hi|hello|dears?|dear (?:all|team|sir|sirs|support|support team)|"
    r"(?:best|kind|warm)? ?regards|br|thanks?(?: you)?(?: in advance)?|"
    r"many thanks|sincerely|yours sincerely|respectfully|"
    r"thanks? ?(?:&|and) ?(?:best |kind |warm )?regards|"
    r"good (?:morning|afternoon|evening|day)|صباح الخير|مساء الخير|"
    r"(?:i )?hope (?:this|that) (?:e-?mail|message) finds you (?:well|in good health)|"
    r"(?:i )?hope you(?:'re|’re| are) (?:doing )?well|hope all is well"
    r")$", re.I)

# "Dear Mohammed," or "الأستاذ فيصل المحترم" -- an address with nothing else.
_SALUTATION = re.compile(r"^(?:dear|hi|hello|مرحبا|مرحباً|أهلا|اهلا|المهندس|المهندسة|الدكتور|الدكتورة|د\.|م\.|الأستاذ|الاستاذ|الأستاذة|الاستاذة|"
                         r"الزميل|الزميلة|سعادة|أ\.|ا\.)(?:\s|/|\.)"
                         r"|(?:المحترم|المحترمة|المحترمين)$", re.I)


_BLESSING_END = re.compile(r"(?:سلمه|سلمها|حفظه|حفظها|وفقه|وفقها|وفقهم|حفظهم) الله$"
                           r"|(?:المحترم|المحترمة|المحترمين)$")


_SIGNOFF_WORDS = frozenset("""
    و ولكم لكم مع مني منا كل
    تقبلوا وتقبلوا تقبلو وتقبلو تقلوا وتقلوا تقبل وتقبل
    اطيب أطيب خالص فائق وافر جزيل
    تحياتي وتحياتي تحيات التحية والتحية التحيه تحية
    وتقديري تقديري وتقدري والتقدير التقدير والاحترام الاحترام احترامي واحترامي
    تفضلوا وتفضلوا بقبول
    شاكرين وشاكرين شاكر لكم جهودكم تعاونكم حسن وشكرا شكرا شكراً وشكراً
    شاكر وشاكر شاكرة وشاكرة شاكره وشاكره مقدر ومقدر ومقدرة ومقدره مقدرة مقدره ومقدرين لتعاونكم التحيات والتحيات التحايا دمتم ودمتم
    للزملاء والزميلات الزميلات الزملاء الزميل الزميلة الأعزاء الاعزاء العزيز الأفاضل الافاضل
    السادة الكرام فريق الدعم الفني التقني الفريق في سلمهم سلمكم الله حفظكم
    الشكر دعمكم ودعمكم لدعمكم وتعاونكم المستمر والتشغيل التشغيل
    for your assistance help support cooperation الاخوة الإخوة الأخوة مرحبا أهلا اهلا
    dear dears hi hello team support all
    thank thanks you and & best kind warm regards regard with many sincerely
    yours cheers respectfully appreciation
""".split())


def _is_signoff(line: str) -> bool:
    bare = re.sub("[\u064B-\u0652\u0670]", "", line)        # tanween and other marks
    words = re.sub(r"[*_,،.؛;:!…()\-]+", " ", bare).split()
    return 0 < len(words) <= 8 and all(w.casefold() in _SIGNOFF_WORDS for w in words)


_FIELD_LABEL = re.compile(r"^[^:：\d]{1,30}[:：]$")


def _is_field_label(line: str) -> bool:
    bare = line.strip()
    return bool(_FIELD_LABEL.match(bare)) and len(bare.split()) <= 4


def _is_greeting(line: str) -> bool:
    if _is_field_label(line):
        return True
    if _is_signoff(line):
        return True
    bare = re.sub(r"[\s,.:;،؛!]+$", "", line).strip()
    if _GREETING.match(bare):
        return True
    if _BLESSING_END.search(bare) and len(bare.split()) <= 8:
        return True
    if not _SALUTATION.match(bare):
        return False
    # Up to 5 words always; up to 8 when it ends in a comma, the way an address
    # line does ("مرحبا NCP Platform AND Application Group،") and a sentence
    # ("Dear team, the system is down") does not.
    ends_like_address = line.rstrip().endswith((",", "،"))
    return len(bare.split()) <= (8 if ends_like_address else 5)


def _is_garbled(line: str) -> bool:
    visible = [c for c in line if not c.isspace()]
    return len(visible) >= 10 and visible.count("?") / len(visible) > 0.3


# ---------------------------------------------------------------------------
# the whole thing
# ---------------------------------------------------------------------------

@dataclass
class Boilerplate:
    """The line keys to remove for one ticket."""

    keys: frozenset[str] = frozenset()

    def __contains__(self, line: str) -> bool:
        return line_key(line) in self.keys


def build(body: str, boilerplate: Boilerplate | None = None,
          trace: list | None = None, *, sender: str | None = None) -> str:
    """The search text for one first article. Pure: no database, no model.

    With trace, what the three rules that remove more than a line took out is
    appended to it as (rule, text): a disclaimer paragraph, the rest of a
    message after "--", and a quote from our staff. Those are the only places a
    real sentence could be lost in bulk, so they are what an audit checks.
    """
    boilerplate = boilerplate or Boilerplate()
    # Our staff's quoted messages are skipped only when a customer opened the
    # ticket: there they are our answers, forwarded back. When one of our staff
    # opened it -- an internal escalation -- those messages are the description
    # of the customer's problem ("the client is facing issues in adding users",
    # the error text the technical team found), and skipping them loses it.
    skip_staff = domain_of(sender) not in INTERNAL_DOMAINS
    lines = candidate_lines(body, trace, skip_staff)

    # 3. learned boilerplate
    lines = [l if (not l or l not in boilerplate) else "" for l in lines]

    # 4. disclaimers, by paragraph
    paragraphs: list[list[str]] = [[]]
    for l in lines:
        if l:
            paragraphs[-1].append(l)
        elif paragraphs[-1]:
            paragraphs.append([])
    kept = []
    for p in paragraphs:
        if not p:
            continue
        if _is_disclaimer(" ".join(p)):
            if trace is not None:
                trace.append(("disclaimer", " ".join(p)))
            continue
        kept.append(p)

    # 5-7. greetings, garbled lines, repeats
    seen: set[str] = set()
    out: list[str] = []
    for p in kept:
        block = []
        for l in p:
            if _is_greeting(l) or _is_garbled(l):
                continue
            key = line_key(l)
            if len(key) >= 20 and key in seen:
                continue
            seen.add(key)
            block.append(l)
        if block:
            out.append("\n".join(block))
    return "\n\n".join(out).strip()


# ---------------------------------------------------------------------------
# reading the boilerplate table
# ---------------------------------------------------------------------------

def domain_of(address: str | None) -> str | None:
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().lower() or None


def load_boilerplate(sender: str | None) -> Boilerplate:
    """Everything to remove for a ticket from this sender."""
    sender = (sender or "").strip().lower() or None
    rows = postgres.query(
        """
        SELECT line_key FROM boilerplate_lines
        WHERE scope = 'internal'
           OR (scope = 'sender' AND key = %s)
           OR (scope = 'domain' AND key = %s)
        """,
        (sender, domain_of(sender)),
    )
    return Boilerplate(frozenset(r["line_key"] for r in rows))


def fill_ticket(ticket_id: int) -> str | None:
    """Build and store search_text for one live ticket. Never raises: a ticket
    without search text is still a ticket, and intake must not fail over it."""
    try:
        row = postgres.query_one(
            """
            SELECT body_raw, sender_address FROM articles
            WHERE ticket_id = %s AND party = 'request'
            ORDER BY article_no NULLS LAST, id
            LIMIT 1
            """,
            (ticket_id,),
        )
        if row is None:
            return None
        text = build(row["body_raw"], load_boilerplate(row["sender_address"]),
                     sender=row["sender_address"])
        postgres.execute(
            "UPDATE tickets SET search_text = %s, search_ver = %s WHERE id = %s",
            (text, SEARCH_VER, ticket_id),
        )
        return text
    except Exception:
        log.exception("search text failed", extra={"ticket": ticket_id})
        return None
