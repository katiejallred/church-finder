import csv
import json

import pytest

import church_finder.warmysender as ws
from church_finder.cli import OUTPUT_FIELDS
from church_finder.nightly import load_progress, sync_warmysender
from church_finder.warmysender import (
    LEDGER,
    Prospect,
    WarmySender,
    WarmySenderError,
    build_prospect,
    list_name_for,
    pending_prospects,
    read_ledger,
    review_note,
    sync,
)


def row(**over):
    base = {f: "" for f in OUTPUT_FIELDS}
    base.update({
        "church_name": "First Baptist Chuch", "website": "https://www.firstbaptist.org/", "city": "Springfield",
        "state": "IL", "phone": "+1 217 555 0100", "best_email": "Office@FirstBaptist.org",
        "contact_type": "Admin / Office", "role_title": "office mailbox", "score": "55",
        "found_on_page": "https://www.firstbaptist.org/contact", "status": "ok",
    })
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Building prospects


def test_build_prospect_segments_lists_and_tags():
    p = build_prospect(row())
    assert p.email == "office@firstbaptist.org"
    assert p.segment == "Admin"
    assert p.list_name == "Illinois Admin"
    assert p.tags == ["church-finder", "IL", "Illinois", "Admin", "Springfield"]
    assert p.body["company"] == "First Baptist Church"  # OSM typo fixed
    assert p.body["role"] == "Admin / Office"  # a shared mailbox has no real title
    assert p.body["enroll"] is True
    cf = p.body["customFields"]
    assert cf["greeting"] == "there"
    assert cf["segment"] == "Illinois Admin"
    assert cf["state"] == "IL" and cf["state_name"] == "Illinois" and cf["city"] == "Springfield"
    assert cf["source"] == "church-finder"
    assert p.update == {
        "tags": p.tags, "country": "United States",
        "website": "https://www.firstbaptist.org/", "city": "Springfield",
    }
    assert p.idempotency_key.startswith("church-finder-")


def test_build_prospect_pastor_and_comms_segments():
    assert build_prospect(row(contact_type="Pastor", role_title="Senior Pastor")).list_name == "Illinois Pastors"
    comms = build_prospect(row(contact_type="Communications", role_title="DIRECTOR OF COMMUNICATIONS"))
    assert comms.segment == "Comms"
    assert comms.body["role"] == "Director Of Communications"
    assert build_prospect(row(contact_type="Other")).segment == "Other"
    assert build_prospect(row(contact_type="")).segment == "Other"


def test_build_prospect_flags_foreign_domains_for_review():
    p = build_prospect(row(best_email="chancery@diocese-springfield.org"))
    assert p.review
    assert "review" in p.tags
    assert p.body["enroll"] is False  # on the list, but not auto-enrolled
    # Free mail and the church's own domain are fine.
    assert build_prospect(row(best_email="pastor@gmail.com")).review == ""
    assert build_prospect(row(best_email="pastor@firstbaptist.org")).review == ""


def test_build_prospect_skips_bad_emails_and_maps_state_names():
    assert build_prospect(row(best_email="")) is None
    assert build_prospect(row(best_email="pastor@st.luke")) is None
    p = build_prospect(row(state="Illinois"))
    assert p.state == "IL" and p.list_name == "Illinois Admin"
    assert build_prospect(row(contact_name="Shelia Ward")).body["firstName"] == "Shelia"


def test_review_note_and_list_names():
    assert review_note("a@b.org", "https://www.b.org/") == ""
    assert "differs" in review_note("a@diocese.org", "https://b.org")
    assert list_name_for("al", "Pastors") == "Alabama Pastors"


# ---------------------------------------------------------------------------
# The API client


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = b"x" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    """Answers canned responses and records every call."""

    def __init__(self, lists=("Illinois Admin", "Illinois Pastors"), existing=()):
        self.calls = []
        self.lists = list(lists)
        self.existing = set(existing)
        self.queued = []  # responses to return first, for error cases

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append((method, url.split("/api/v1")[1], json, params, headers))
        if self.queued:
            return self.queued.pop(0)
        path = url.split("/api/v1")[1]
        if method == "GET" and path == "/lists":
            return self.lists_response()
        if method == "POST" and path == "/prospects":
            email = json["email"]
            existed = email in self.existing
            return FakeResponse(200 if existed else 201, {
                "data": {"id": "p-" + email.split("@")[0], "email": email},
                "alreadyExisted": existed, "listId": json.get("listId"),
                "list": {"attached": True, "enrolledCampaigns": 0},
            })
        if method == "PATCH" and path.startswith("/prospects/"):
            return FakeResponse(200, {"data": {"id": path.rsplit("/", 1)[1]}})
        raise AssertionError(f"unexpected call {method} {path}")

    def lists_response(self):
        data = [{"id": f"list-{i}", "name": name, "prospectCount": 0} for i, name in enumerate(self.lists)]
        return FakeResponse(200, {"data": data, "pagination": {"has_more": False, "next_cursor": None}})


def client_for(session, **kw):
    sleeps = []
    c = WarmySender("ws_test", session=session, rpm=0, sleep=sleeps.append, **kw)
    c.sleeps = sleeps
    return c


def test_client_sends_bearer_and_idempotency_key():
    session = FakeSession()
    c = client_for(session)
    assert c.lists() == {"illinois admin": "list-0", "illinois pastors": "list-1"}
    p = build_prospect(row())
    pid, existed = c.create_prospect(p, "list-0")
    assert (pid, existed) == ("p-office", False)
    method, path, body, _, headers = session.calls[-1]
    assert (method, path) == ("POST", "/prospects")
    assert headers["Authorization"] == "Bearer ws_test"
    assert headers["Idempotency-Key"] == p.idempotency_key
    assert body["listId"] == "list-0" and body["email"] == "office@firstbaptist.org"


def test_client_waits_out_rate_limits_and_honours_retry_after():
    session = FakeSession()
    session.queued = [FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(503)]
    c = client_for(session)
    assert c.lists()["illinois admin"] == "list-0"
    assert c.sleeps == [7.0, 10.0]  # header first, then 5s * 2^(attempt-1)
    assert len(session.calls) == 3


def test_client_raises_on_refusal_and_on_missing_list():
    session = FakeSession()
    session.queued = [FakeResponse(400, {"error": {"code": "validation_error", "message": "email is invalid"}})]
    c = client_for(session)
    with pytest.raises(WarmySenderError) as exc:
        c.create_prospect(build_prospect(row()), "list-0")
    assert exc.value.status == 400 and "invalid" in str(exc.value)
    session.queued = [FakeResponse(200, {"data": {"id": "p-1"}, "alreadyExisted": False,
                                         "list": {"attached": False, "error": "list_not_found", "message": "no such list"}})]
    with pytest.raises(WarmySenderError):
        c.create_prospect(build_prospect(row()), "list-gone")


def test_client_paces_requests():
    session = FakeSession()
    clock = iter([0.0, 0.5, 1.2, 100.0])
    sleeps = []
    c = WarmySender("ws_test", session=session, rpm=60, sleep=sleeps.append, clock=lambda: next(clock))
    for _ in range(4):
        c.lists()
    # One a second: the second call waits 0.5s, the third 0.8s (it is due at 2.0), the fourth not at all.
    assert sleeps == [pytest.approx(0.5), pytest.approx(0.8)]


# ---------------------------------------------------------------------------
# The nightly sync


def write_master(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    write_master(d / "church_contacts.csv", [
        row(),  # Illinois Admin
        row(church_name="Grace Church", website="https://grace.org", best_email="pastor@grace.org",
            contact_type="Pastor", role_title="Senior Pastor"),  # Illinois Pastors
        row(church_name="Hope Church", website="https://hope.org", best_email="news@hope.org",
            contact_type="Communications"),  # Illinois Comms: list missing
        row(church_name="Second Baptist", website="https://second.org", best_email="office@firstbaptist.org"),  # shared inbox
        row(church_name="No Email Church", best_email="", contact_type="", status="no_email"),
    ])
    return d


def test_sync_pushes_to_lists_holds_back_missing_and_keeps_a_ledger(data_dir):
    session = FakeSession()
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))
    assert report["synced"] == 2 and report["existed"] == 0 and report["errors"] == 0
    assert report["held"] == 1 and report["missing_lists"] == ["Illinois Comms"]
    assert report["pending"] == 3 and report["remaining"] == 0

    posts = [c for c in session.calls if c[0] == "POST"]
    assert [c[2]["email"] for c in posts] == ["office@firstbaptist.org", "pastor@grace.org"]
    assert [c[2]["listId"] for c in posts] == ["list-0", "list-1"]
    patches = [c for c in session.calls if c[0] == "PATCH"]
    assert [c[1] for c in patches] == ["/prospects/p-office", "/prospects/p-pastor"]
    assert patches[1][2]["tags"] == ["church-finder", "IL", "Illinois", "Pastors", "Springfield"]

    ledger = read_ledger(str(data_dir))
    assert set(ledger) == {"office@firstbaptist.org", "pastor@grace.org"}
    assert ledger["pastor@grace.org"]["status"] == "synced"
    assert ledger["pastor@grace.org"]["list_name"] == "Illinois Pastors"
    assert ledger["pastor@grace.org"]["prospect_id"] == "p-pastor"

    # The next night: only the held-back comms contact is still pending.
    assert [p.email for p in pending_prospects(str(data_dir), str(data_dir / "church_contacts.csv"), ledger)] == ["news@hope.org"]
    session2 = FakeSession(lists=("Illinois Admin", "Illinois Pastors", "Illinois Comms"))
    report2 = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session2))
    assert report2["synced"] == 1 and report2["held"] == 0
    assert len(read_ledger(str(data_dir))) == 3


def test_sync_existing_prospects_keep_their_tags(data_dir):
    session = FakeSession(existing={"pastor@grace.org"})
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))
    assert report["synced"] == 1 and report["existed"] == 1
    assert [c[1] for c in session.calls if c[0] == "PATCH"] == ["/prospects/p-office"]
    assert read_ledger(str(data_dir))["pastor@grace.org"]["status"] == "existed"


def test_sync_respects_the_nightly_cap_and_records_refusals(data_dir):
    session = FakeSession()
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session), max_per_night=1)
    assert report["synced"] == 1 and report["remaining"] == 1
    assert len(read_ledger(str(data_dir))) == 1

    session.queued = [session.lists_response(),
                      FakeResponse(422, {"error": {"code": "validation_error", "message": "bad address"}})]
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))
    assert report["errors"] == 1 and report["synced"] == 0
    assert read_ledger(str(data_dir))["pastor@grace.org"]["status"] == "error"
    # Refused addresses are not retried night after night.
    assert sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))["pending"] == 1


def test_sync_stops_on_outage_without_losing_the_ledger(data_dir):
    session = FakeSession()
    session.queued = [session.lists_response()] + [FakeResponse(503)] * ws.RETRY_ATTEMPTS
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))
    assert report["synced"] == 0 and report["error"].startswith("WarmySender busy")
    assert report["remaining"] == 2  # both retried next night
    assert read_ledger(str(data_dir)) == {}


def test_sync_stops_when_the_plan_is_full_and_retries_later(data_dir):
    session = FakeSession()
    session.queued = [session.lists_response(),
                      FakeResponse(403, {"error": {"code": "plan_limit",
                                                   "message": "Prospect limit reached (25000). Upgrade to add more."}})]
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session))
    assert report["errors"] == 0 and report["synced"] == 0
    assert "Prospect limit reached" in report["error"]
    assert report["remaining"] == 2
    assert [c[0] for c in session.calls] == ["GET", "POST"]  # stopped at the first refusal
    assert read_ledger(str(data_dir)) == {}
    # Once there is room, both are pushed.
    assert sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(FakeSession()))["synced"] == 2


def test_sync_dry_run_writes_nothing(data_dir):
    session = FakeSession()
    report = sync(str(data_dir), str(data_dir / "church_contacts.csv"), client_for(session), dry_run=True)
    assert report["dry_run"] and report["missing_lists"] == ["Illinois Comms"]
    assert [c[0] for c in session.calls] == ["GET"]
    assert not (data_dir / LEDGER).exists()


def test_sync_without_a_key_only_fails_when_there_is_work(data_dir):
    with pytest.raises(ValueError):
        sync(str(data_dir), str(data_dir / "church_contacts.csv"), None)
    write_master(data_dir / "church_contacts.csv", [])
    assert sync(str(data_dir), str(data_dir / "church_contacts.csv"), None)["pending"] == 0


def test_nightly_sync_records_report_and_status(data_dir):
    session = FakeSession()
    report = sync_warmysender(str(data_dir), None, 1000, client=client_for(session))
    assert report["synced"] == 2
    assert load_progress(str(data_dir))["warmysender"]["missing_lists"] == ["Illinois Comms"]
    status = (data_dir / "STATUS.md").read_text()
    assert "## WarmySender" in status
    assert "2 pushed" in status and "1 held back" in status
    assert "- Illinois Comms" in status

    # An exception (no key, say) is recorded rather than failing the night.
    write_master(data_dir / "church_contacts.csv", [row(best_email="new@firstbaptist.org", contact_type="Pastor")])
    report = sync_warmysender(str(data_dir), None, 1000)
    assert "API key" in report["error"]
    assert "stopped early" in (data_dir / "STATUS.md").read_text()
