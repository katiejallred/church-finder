import csv

from church_finder.cli import scrape
from church_finder.crawler import SiteCrawler
from church_finder.discover import Church, read_churches


class FakeResponse:
    def __init__(self, url, text="", status=200, ctype="text/html; charset=utf-8"):
        self.url = url
        self.text = text
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def iter_content(self, chunk_size=1024):
        data = self.text.encode("utf-8")
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, **_):
        self.requested.append(url)
        key = url.rstrip("/")
        if key in self.pages:
            return FakeResponse(url, self.pages[key])
        return FakeResponse(url, "", status=404)


SITE = {
    "https://grace.org/robots.txt": "User-agent: *\nDisallow: /private",
    "https://grace.org": """<a href="/about">About</a> <a href="/our-staff">Our Staff</a>
                            <a href="/private/staff">Secret</a>
                            <footer><a href="mailto:info@grace.org">info@grace.org</a></footer>""",
    "https://grace.org/our-staff": """<div><h4>Kim Ray</h4><p>Communications Director</p>
                                      <a href="mailto:kim@grace.org">Email</a></div>""",
    "https://grace.org/about": "<p>About us</p>",
    "https://grace.org/private/staff": "<a href='mailto:hidden@grace.org'>x</a>",
}


def crawler(pages):
    session = FakeSession(pages)
    return SiteCrawler(session=session, delay=0), session


def test_crawl_finds_comms_on_staff_page_and_stops():
    c, session = crawler(SITE)
    result = c.crawl("grace.org")
    assert result.status == "ok"
    assert result.candidates[0].email == "kim@grace.org"
    # Staff page is visited first and is good enough, so /about is never fetched.
    assert "https://grace.org/about" not in session.requested
    assert all("/private" not in u for u in session.requested)


def test_crawl_unreachable():
    c, _ = crawler({})
    assert c.crawl("https://nowhere.org").status == "fetch_error"


def test_crawl_invalid_url():
    c, _ = crawler({})
    assert c.crawl("not a url").status == "invalid_url"


def test_read_churches_loose_headers(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("Church Name,URL,City,State,E-mail\nGrace,grace.org,Mobile,AL,office@grace.org\nNo Site,,X,AL,\n")
    churches = read_churches(str(p))
    assert len(churches) == 1
    assert churches[0].name == "Grace" and churches[0].website == "grace.org"
    assert churches[0].email == "office@grace.org"


def test_listed_email_is_the_fallback_when_the_site_yields_nothing(tmp_path, monkeypatch):
    import church_finder.crawler as crawler_mod

    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    churches = [
        Church(name="Grace", website="grace.org", email="pastor@grace.org"),       # site has better contacts
        Church(name="Hope", website="https://hope.org", email="Pastor@hope.org"),   # site unreachable
        Church(name="Peace", website="https://peace.org", email="fictional-peacechurch@gmail.com"),
        Church(name="Quiet", website="https://quiet.org"),                           # nothing listed either
    ]
    out = tmp_path / "out.csv"
    stats = scrape(churches, str(out), workers=1, delay=0)
    rows = {r["church_name"]: r for r in csv.DictReader(out.open())}

    grace = rows["Grace"]  # the crawl's comms director still wins; the listed pastor rides along
    assert grace["best_email"] == "kim@grace.org" and grace["status"] == "ok"
    assert "pastor@grace.org" in grace["all_emails"]

    hope = rows["Hope"]
    assert hope["best_email"] == "pastor@hope.org" and hope["contact_type"] == "Pastor"
    assert hope["status"] == "listed" and hope["found_on_page"] == "listed in Overture Maps"
    assert hope["score"] == "45"  # pastor@ mailbox (40) on the church's own domain (+5)

    peace = rows["Peace"]
    assert peace["best_email"] == "fictional-peacechurch@gmail.com" and peace["contact_type"] == "Other"
    assert peace["status"] == "listed" and peace["score"] == "10"

    assert rows["Quiet"]["best_email"] == "" and rows["Quiet"]["status"] == "fetch_error"
    assert stats == {"Communications": 1, "Pastor": 1, "Other": 1, "fetch_error": 1}


def test_scrape_writes_and_resumes(tmp_path, monkeypatch):
    out = tmp_path / "out.csv"
    import church_finder.crawler as crawler_mod

    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    churches = [Church(name="Grace", website="grace.org")]
    scrape(churches, str(out), workers=1, delay=0)
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["best_email"] == "kim@grace.org"
    assert rows[0]["contact_type"] == "Communications"
    assert rows[0]["backup_email"] == "info@grace.org"

    stats = scrape(churches, str(out), workers=1, delay=0)  # already done -> skipped
    assert stats == {}
    assert len(list(csv.DictReader(out.open()))) == 1


def test_bio_pages_for_comms_staff_are_visited_first():
    pages = {
        "https://bio.org": '<a href="/team">Meet Our Team</a>',
        "https://bio.org/team": "".join(
            f'<div class="card"><a href="/team/p{i}">Person {i}</a><p>{title}</p></div>'
            for i, title in enumerate(["Youth Pastor", "Worship Leader", "Kids Director",
                                       "Facilities", "Media & Communications Director"])
        ),
        "https://bio.org/team/p4": "<p>Email: <a href='mailto:comms@bio.org'>comms@bio.org</a></p>",
    }
    c, session = crawler(pages)
    result = c.crawl("https://bio.org")
    assert result.candidates[0].email == "comms@bio.org"
    assert not any(u.endswith(("/p0", "/p1", "/p2", "/p3")) for u in session.requested)


def test_fetch_caps_response_size():
    big = "<p>start</p>" + "x" * 500_000 + "<a href='mailto:late@grace.org'>late</a>"
    session = FakeSession({"https://grace.org": big, "https://grace.org/robots.txt": ""})
    crawler = SiteCrawler(session=session, delay=0, max_html_bytes=100_000, max_pages=1)
    result = crawler.crawl("https://grace.org")
    # The email sat past the size cap, so it was never read.
    assert result.status == "no_email"
    assert result.pages_crawled == 1


def test_crawl_stops_at_site_time_limit(monkeypatch):
    import church_finder.crawler as c

    clock = {"t": 1000.0}
    monkeypatch.setattr(c.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(c.time, "sleep", lambda s: None)

    class SlowSession(FakeSession):
        def get(self, url, **kw):
            clock["t"] += 40  # every page takes 40 "seconds"
            return super().get(url, **kw)

    pages = {
        "https://grace.org/robots.txt": "",
        "https://grace.org": "<a href='/about'>About</a><a href='/staff'>Staff</a><a href='/contact'>Contact</a>",
        "https://grace.org/staff": "<p>nothing here</p>",
        "https://grace.org/about": "<p>nothing here</p>",
        "https://grace.org/contact": "<a href='mailto:office@grace.org'>x</a>",
    }
    session = SlowSession(pages)
    crawler = SiteCrawler(session=session, delay=0, max_pages=8, max_site_seconds=60)
    result = crawler.crawl("https://grace.org")
    assert "time limit" in (result.error or "")
    assert result.pages_crawled < 4
    assert len(session.requested) < len(pages)
