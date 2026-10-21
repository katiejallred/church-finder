"""Pull email addresses out of church web pages and classify who they belong to.

Each email is scored by the role it appears next to on the page (e.g. a staff
card reading "Jane Doe - Director of Communications") and by its mailbox name
(e.g. ``communications@``). Higher scores are better contacts:

    communications  >  admin / office  >  pastor  >  anything else
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# "jane [at] church [dot] org", "jane(at)church(dot)org", "jane at church dot org"
OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*"
    r"([A-Za-z0-9\-]+(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+|\.)\s*[A-Za-z0-9\-]+)+)",
    re.IGNORECASE,
)
DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+|\.)\s*", re.IGNORECASE)

# Addresses that are never a real person/office contact: template placeholders,
# and the companies that build or host church websites (their support address
# ends up in page footers and theme code).
JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com",
    "yourdomain.com", "mydomain.com", "mysite.com", "yoursite.com", "test.com",
    "sentry.io", "wixpress.com", "sentry.wixpress.com", "godaddy.com", "wix.com",
    "squarespace.com", "wordpress.com", "wordpress.org", "weebly.com", "webflow.com",
    "netministries.org", "ekklesia360.com", "clover.com", "cloversites.com",
    "sharefaith.com", "subsplash.com", "pushpay.com", "tithe.ly", "nucleus.church",
    "faithlife.com", "monkdev.com", "churchtrac.com", "breezechms.com",
    "planningcenteronline.com", "ccbchurch.com", "elexio.com", "e-zekiel.com",
}
JUNK_LOCAL_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|mailer-daemon|postmaster|abuse|"
    r"privacy|webmaster|wordpress|user|username|name|email|your-?email|you|"
    r"test|testing|sample|demo|placeholder|someone|somebody|firstname|lastname|"
    r"first\.?last|john\.?doe|jane\.?doe)$",
    re.IGNORECASE,
)

# Text like "pastor@St.Luke's" or a line break after "church.or" produces an
# address whose last label is not a real top-level domain. Reject those.
GENERIC_TLDS = set("""
com org net edu gov mil int arpa info biz name pro mobi tel asia cat jobs museum travel
coop aero post xxx
church faith bible charity foundation ngo ong community catholic christmas
life live love family world global online site website one today news media email
app dev cloud digital tech io ai co me tv fm cc us ws nu ly fyi xyz link page top click
click wiki guide directory help hope kids earth land fun club press social network
center city events group team care health house place space zone camp gives giving
services solutions systems agency company inc llc ltd partners works
academy school university college education training courses study
radio video music art design studio store shop blog
nyc la vegas miami boston chicago
""".split())
COUNTRY_TLDS = set("""
ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br
bs bt bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do
dz ec ee eg er es et eu fi fj fk fm fo fr ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs
gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp ke kg kh ki km
kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp
mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk
pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss
st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug uk us uy uz va vc
ve vg vi vn vu wf ws ye yt za zm zw
""".split())
VALID_TLDS = GENERIC_TLDS | COUNTRY_TLDS
FILE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|ico|bmp|tiff?)$", re.IGNORECASE)

# (score, category, pattern) — checked in order, first match wins per context.
ROLE_PATTERNS: list[tuple[int, str, re.Pattern[str]]] = [
    (100, "communications", re.compile(
        r"\b(director|dir\.|head|pastor|minister|manager|vp|vice president)\s+(of\s+)?"
        r"(communications?|comms|marketing|media\s*(?:&|and)\s*communications?)\b"
        r"|\b(communications?|comms|marketing)\s+(director|pastor|manager|lead|leader|minister)\b",
        re.IGNORECASE)),
    (90, "communications", re.compile(
        r"\b(communications?|comms|marketing)\s+(coordinator|specialist|assistant|associate|"
        r"strategist|team|admin(istrator)?)\b|\bcommunications\b|\bmarketing\b",
        re.IGNORECASE)),
    (80, "communications", re.compile(
        r"\b(creative|media|digital media|social media|design|content|brand(ing)?)\s+"
        r"(director|pastor|manager|coordinator|specialist|lead)\b"
        r"|\bdirector\s+of\s+(creative|media|digital)",
        re.IGNORECASE)),
    (65, "admin", re.compile(
        r"\b(church|business|executive|parish|office|operations?)\s+(administrator|manager)\b"
        r"|\bexecutive\s+pastor\b|\bdirector\s+of\s+(operations|administration)\b"
        r"|\b(operations|administrative)\s+(director|pastor)\b|\badministrator\b",
        re.IGNORECASE)),
    (55, "admin", re.compile(
        r"\b(administrative|office|ministry|executive)\s+(assistant|coordinator|secretary)\b"
        r"|\b(church\s+)?secretary\b|\breceptionist\b|\bfront\s+desk\b|\bchurch\s+office\b"
        r"|\boffice\b",
        re.IGNORECASE)),
    (45, "pastor", re.compile(
        r"\b(senior|lead|head)\s+pastor\b|\bpastor\s*/\s*teacher\b|\brector\b"
        r"|\bsenior\s+minister\b|\bparish\s+priest\b",
        re.IGNORECASE)),
    (20, "other", re.compile(
        r"\b(youth|student|children'?s?|kids|worship|music|nursery|preschool|college|"
        r"young\s+adults?|women'?s|men'?s|missions?|recovery)\s+(pastor|minister|director)\b",
        re.IGNORECASE)),
    (35, "pastor", re.compile(
        r"\bpastor\b|\bminister\b|\breverend\b|\brev\.|\bpriest\b|\bfr\.\s|\bvicar\b|\bclergy\b",
        re.IGNORECASE)),
]

SPECIALTY_MINISTRY_RE = ROLE_PATTERNS[-2][2]
GENERIC_PASTOR_RE = ROLE_PATTERNS[-1][2]

# Mailbox-name hints, used when the page text gives nothing better.
LOCAL_PART_HINTS: list[tuple[int, str, re.Pattern[str]]] = [
    (85, "communications", re.compile(
        r"^(communications?|comms?|marketing|media|press|news|creative)$", re.IGNORECASE)),
    (50, "admin", re.compile(
        r"^(office|admin|administrator|administration|churchoffice|church\.?office|"
        r"info|contact|hello|church|mail|frontdesk|reception|secretary|connect)$",
        re.IGNORECASE)),
    (40, "pastor", re.compile(r"^(senior)?pastor$|^rector$|^revd?$", re.IGNORECASE)),
]

CATEGORY_LABELS = {
    "communications": "Communications",
    "admin": "Admin / Office",
    "pastor": "Pastor",
    "other": "Other",
}

MAX_CONTEXT_CHARS = 300


@dataclass
class EmailHit:
    email: str
    score: int
    category: str
    title: str  # the role text that matched, e.g. "Director of Communications"
    context: str
    source_url: str


@dataclass
class EmailCandidate:
    """All sightings of one address across a site, collapsed to its best one."""

    email: str
    best: EmailHit
    sightings: int = 1
    pages: set[str] = field(default_factory=set)


def clean_email(raw: str) -> str | None:
    email = unquote(raw).strip().strip(".,;:()[]<>\"'").lower()
    email = email.split("?")[0]
    if not EMAIL_RE.fullmatch(email):
        return None
    local, _, domain = email.rpartition("@")
    if domain in JUNK_DOMAINS or JUNK_LOCAL_RE.match(local) or FILE_EXT_RE.search(email):
        return None
    if domain.rpartition(".")[2] not in VALID_TLDS or ".." in domain or domain.startswith("."):
        return None
    # Long hex/uuid-like local parts are tracking ids, not mailboxes.
    if re.fullmatch(r"[0-9a-f]{16,}", local):
        return None
    return email


def decode_cfemail(encoded: str) -> str | None:
    """Decode Cloudflare's email obfuscation (very common on church sites)."""
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    try:
        return bytes(b ^ key for b in data[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def classify_text(text: str) -> tuple[int, str, str]:
    """Return (score, category, matched title) for a chunk of page text."""
    best = (0, "", "")
    for score, category, pattern in ROLE_PATTERNS:
        haystack = text
        if pattern is GENERIC_PASTOR_RE:
            # "Youth Pastor" shouldn't count as the church's pastor contact.
            haystack = SPECIALTY_MINISTRY_RE.sub(" ", text)
        m = pattern.search(haystack)
        if m and score > best[0]:
            best = (score, category, m.group(0).strip())
    return best


def classify_local_part(email: str) -> tuple[int, str, str]:
    local = email.split("@")[0]
    for score, category, pattern in LOCAL_PART_HINTS:
        if pattern.match(local):
            return score, category, f"{local}@ mailbox"
    return 0, "", ""


def _text(el: Tag) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def _emails_in(el: Tag) -> set[str]:
    found = {e.lower() for e in EMAIL_RE.findall(el.get_text(" "))}
    for a in el.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            cleaned = clean_email(href[7:])
            if cleaned:
                found.add(cleaned)
    for node in el.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cfemail(node["data-cfemail"])
        if decoded:
            found.add(decoded.lower())
    return found


def context_for(el: Tag, email: str, max_depth: int = 6) -> str:
    """Find the text block describing the person who owns this address.

    Walks up from the element holding the email to the smallest ancestor that
    mentions a role, stopping before it reaches a block that also contains
    someone else's email (so one person's title isn't applied to a neighbour).
    """
    node: Tag | None = el
    fallback = ""
    for _ in range(max_depth):
        if node is None or node.name in ("body", "html", "[document]"):
            break
        text = _text(node)
        if len(text) > MAX_CONTEXT_CHARS * 2:
            break
        others = _emails_in(node) - {email}
        if others and node is not el:
            break
        if not fallback and len(text) >= 20:
            fallback = text
        if classify_text(text)[0]:
            return text[:MAX_CONTEXT_CHARS]
        node = node.parent
    return (fallback or _text(el))[:MAX_CONTEXT_CHARS]


def _score(email: str, context: str, site_domain: str | None) -> tuple[int, str, str]:
    score, category, title = classify_text(context)
    lp_score, lp_cat, lp_title = classify_local_part(email)
    if lp_score > score:
        score, category, title = lp_score, lp_cat, lp_title
    if not category:
        score, category, title = 10, "other", ""
    if site_domain and _same_org_domain(email.split("@")[1], site_domain):
        score += 5
    return score, category, title


def _same_org_domain(email_domain: str, site_domain: str) -> bool:
    def base(d: str) -> str:
        d = d.lower().removeprefix("www.")
        return ".".join(d.split(".")[-2:])

    return base(email_domain) == base(site_domain)


def extract_emails(html: str, page_url: str, site_domain: str | None = None) -> list[EmailHit]:
    """Find every email on a page and score it by the role it is listed under."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "template"]):
        # Cloudflare-protected addresses live in plain markup, so scripts are safe to drop.
        junk.decompose()

    if site_domain is None:
        site_domain = urlparse(page_url).hostname

    raw: list[tuple[str, Tag]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            for part in href[7:].split(","):
                raw.append((part, a))
        elif "/cdn-cgi/l/email-protection#" in href:
            decoded = decode_cfemail(href.split("#", 1)[1])
            if decoded:
                raw.append((decoded, a))

    for node in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cfemail(node["data-cfemail"])
        if decoded:
            raw.append((decoded, node))

    for string in soup.find_all(string=True):
        if not isinstance(string, NavigableString) or not string.parent:
            continue
        text = str(string)
        if "@" in text:
            for m in EMAIL_RE.finditer(text):
                raw.append((m.group(0), string.parent))
        lowered = text.lower()
        if "at" in lowered and ("dot" in lowered or "." in lowered):
            for m in OBFUSCATED_RE.finditer(text):
                domain = DOT_RE.sub(".", m.group(2))
                raw.append((f"{m.group(1)}@{domain}", string.parent))

    hits: list[EmailHit] = []
    for candidate, el in raw:
        email = clean_email(candidate)
        if not email:
            continue
        context = context_for(el, email)
        score, category, title = _score(email, context, site_domain)
        hits.append(EmailHit(email, score, category, title, context, page_url))
    return hits


def merge_hits(hits: list[EmailHit]) -> dict[str, EmailCandidate]:
    merged: dict[str, EmailCandidate] = {}
    for hit in hits:
        existing = merged.get(hit.email)
        if existing is None:
            merged[hit.email] = EmailCandidate(hit.email, hit, 1, {hit.source_url})
            continue
        existing.sightings += 1
        existing.pages.add(hit.source_url)
        if hit.score > existing.best.score:
            existing.best = hit
    return merged


def rank_candidates(candidates: dict[str, EmailCandidate]) -> list[EmailCandidate]:
    """Best contact first. Ties go to the address seen on more pages."""
    return sorted(
        candidates.values(),
        key=lambda c: (c.best.score, c.sightings, -len(c.email)),
        reverse=True,
    )
