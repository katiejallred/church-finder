"""Polite, per-site crawler that visits a church's staff/about/contact pages.

It honours robots.txt, waits between requests to the same site, caps the
number of pages per church, and stops early once it finds a communications
contact.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from .extract import EmailCandidate, classify_text, extract_emails, merge_hits, rank_candidates

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; ChurchFinderBot/1.0; "
    "+https://github.com/katiejallred/church-finder)"
)

# Link keywords that usually lead to staff listings, highest priority first.
LINK_KEYWORDS: list[tuple[int, re.Pattern[str]]] = [
    (100, re.compile(r"staff|our[\s\-_]?team|meet[\s\-_]?the[\s\-_]?team|team|people|directory", re.I)),
    (90, re.compile(r"leadership|leaders|pastors|clergy|elders|who[\s\-_]?we[\s\-_]?are", re.I)),
    (80, re.compile(r"contact|connect|get[\s\-_]?in[\s\-_]?touch|office", re.I)),
    (60, re.compile(r"about", re.I)),
]

# Tried when the homepage doesn't link to obvious staff/contact pages.
FALLBACK_PATHS = ["/staff", "/our-staff", "/about/staff", "/leadership", "/contact", "/contact-us", "/about"]

SKIP_EXT_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|mp3|mp4|mov|zip|docx?|xlsx?|pptx?|ics)$", re.I
)

GOOD_ENOUGH_SCORE = 90  # a communications contact: stop crawling this site


@dataclass
class SiteResult:
    website: str
    status: str = "ok"  # ok | no_email | fetch_error | robots_blocked | invalid_url
    candidates: list[EmailCandidate] = field(default_factory=list)
    pages_crawled: int = 0
    error: str = ""


def normalize_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    if not parsed.hostname or "." not in parsed.hostname:
        return None
    return url


def _same_site(a: str, b: str) -> bool:
    def host(u: str) -> str:
        return (urlparse(u).hostname or "").lower().removeprefix("www.")

    return host(a) == host(b)


def _card_role_score(a) -> int:
    """Role score of the text around a link, e.g. a staff card's title line."""
    node = a
    for _ in range(3):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) > 200:
            return 0
        # Stop at containers holding other links (a nav menu, a whole staff grid).
        if node is not a and len({x.get("href") for x in node.find_all("a", href=True)}) > 1:
            return 0
        score, category, _ = classify_text(text)
        if score and category != "other":
            return score
        if node.parent is None:
            return 0
        node = node.parent
    return 0


class SiteCrawler:
    def __init__(
        self,
        session: requests.Session | None = None,
        max_pages: int = 8,
        delay: float = 1.0,
        timeout: float = 15.0,
        respect_robots: bool = True,
        max_site_seconds: float = 90.0,
        max_html_bytes: int = 2_000_000,
    ):
        self.session = session or make_session()
        self.max_pages = max_pages
        self.delay = delay
        self.timeout = timeout
        self.respect_robots = respect_robots
        # One slow site must not hold up a long run: a server that drips bytes
        # never trips the per-request timeout, so cap wall time and size too.
        self.max_site_seconds = max_site_seconds
        self.max_html_bytes = max_html_bytes
        self._last_request = 0.0
        self._deadline = float("inf")

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def _robots(self, base: str) -> robotparser.RobotFileParser | None:
        if not self.respect_robots:
            return None
        rp = robotparser.RobotFileParser()
        robots_url = urljoin(base, "/robots.txt")
        try:
            self._wait()
            resp = self.session.get(robots_url, timeout=self.timeout)
        except requests.RequestException:
            return None  # unreachable robots.txt => no restrictions
        if resp.status_code in (401, 403):
            rp.disallow_all = True
        elif resp.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(resp.text.splitlines())
        return rp

    def _fetch(self, url: str) -> tuple[str, str] | None:
        """Return (final_url, html) or None."""
        self._wait()
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            log.debug("fetch failed %s: %s", url, exc)
            return None
        try:
            if resp.status_code >= 400:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype.lower() and ctype:
                return None
            chunks: list[bytes] = []
            size = 0
            try:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.max_html_bytes or time.monotonic() > self._deadline:
                        log.debug("truncated %s after %d bytes", url, size)
                        break
            except requests.RequestException as exc:
                log.debug("read failed %s: %s", url, exc)
                if not chunks:
                    return None
            return resp.url, _decode(b"".join(chunks), resp.encoding)
        finally:
            resp.close()

    @staticmethod
    def _rank_links(html: str, page_url: str, root: str) -> list[tuple[int, str]]:
        soup = BeautifulSoup(html, "html.parser")
        ranked: dict[str, int] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url, _ = urldefrag(urljoin(page_url, href))
            if not url.startswith("http") or not _same_site(url, root) or SKIP_EXT_RE.search(url):
                continue
            label = f"{a.get_text(' ', strip=True)} {urlparse(url).path}"
            for score, pattern in LINK_KEYWORDS:
                if pattern.search(label):
                    ranked[url] = max(ranked.get(url, 0), score)
                    break
            # Staff grids often link each person to a bio page holding their email.
            # Visit the ones whose card mentions a role we want before anything else.
            role_score = _card_role_score(a)
            if role_score:
                ranked[url] = max(ranked.get(url, 0), 100 + role_score)
        return sorted(((s, u) for u, s in ranked.items()), reverse=True)

    def crawl(self, website: str) -> SiteResult:
        start = normalize_url(website)
        if not start:
            return SiteResult(website, status="invalid_url")

        self._deadline = time.monotonic() + self.max_site_seconds
        result = SiteResult(start)
        robots = self._robots(start)

        def allowed(url: str) -> bool:
            return robots is None or robots.can_fetch(USER_AGENT, url)

        if not allowed(start):
            result.status = "robots_blocked"
            return result

        fetched = self._fetch(start)
        if fetched is None and start.startswith("https://"):
            fetched = self._fetch("http://" + start[len("https://"):])
        if fetched is None:
            result.status = "fetch_error"
            result.error = "homepage unreachable"
            return result

        home_url, home_html = fetched
        site_domain = urlparse(home_url).hostname
        visited = {home_url.rstrip("/"), start.rstrip("/")}
        hits = extract_emails(home_html, home_url, site_domain)
        result.pages_crawled = 1

        queue = self._rank_links(home_html, home_url, home_url)
        queued = {u.rstrip("/") for _, u in queue}
        for path in FALLBACK_PATHS:
            url = urljoin(home_url, path)
            if url.rstrip("/") not in queued:
                queue.append((10, url))
                queued.add(url.rstrip("/"))

        while queue and result.pages_crawled < self.max_pages:
            if any(h.score >= GOOD_ENOUGH_SCORE for h in hits):
                break
            if time.monotonic() > self._deadline:
                result.error = f"stopped after {self.max_site_seconds:.0f}s site time limit"
                log.debug("%s: %s", start, result.error)
                break
            _, url = queue.pop(0)
            if url.rstrip("/") in visited or not allowed(url):
                continue
            visited.add(url.rstrip("/"))
            fetched = self._fetch(url)
            if fetched is None:
                continue
            final_url, html = fetched
            visited.add(final_url.rstrip("/"))
            if not _same_site(final_url, home_url):
                continue
            result.pages_crawled += 1
            hits.extend(extract_emails(html, final_url, site_domain))

            # Staff pages often link to individual bio pages; follow strong leads.
            for score, link in self._rank_links(html, final_url, home_url):
                if score >= 90 and link.rstrip("/") not in queued:
                    queued.add(link.rstrip("/"))
                    queue.append((score, link))
            queue.sort(key=lambda t: t[0], reverse=True)

        result.candidates = rank_candidates(merge_hits(hits))
        if not result.candidates:
            result.status = "no_email"
        return result


def _decode(raw: bytes, declared: str | None) -> str:
    for encoding in (declared, "utf-8", "cp1252"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    })
    adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
