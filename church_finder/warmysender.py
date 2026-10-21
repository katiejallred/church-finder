"""Push newly found church contacts to WarmySender, segmented, listed and tagged.

After each night's merge, every new row in ``church_contacts.csv`` with a
usable email becomes a WarmySender prospect:

    list           "<State> <Segment>", e.g. "Illinois Pastors". Lists must
                   already exist in the WarmySender workspace (the public API
                   cannot create them); contacts whose list is missing are
                   held back and STATUS.md names the lists to create.
    segment        Comms, Admin, Pastors or Other, from contact_type
    tags           church-finder, IL, Illinois, Pastors, the city, and
                   "review" when the email's domain is not the church's own
    custom fields  greeting, segment, contact_type, state, state_name, city,
                   found_on_page, source — so campaign emails can use
                   {{greeting}} and {{company}} like the Alabama sequence

A ledger, ``warmysender_sync.csv`` in the data folder, remembers every
address already pushed so nothing is sent twice. Prospects whose email domain
differs from the church website (a diocese, a school, a vendor) are still
pushed but tagged ``review`` and not auto-enrolled into any campaign linked
to the list, so a person can check them first.

The API: https://warmysender.com/api/v1 with ``Authorization: Bearer ws_...``
(create a key under workspace settings with the ``prospects:read`` and
``prospects:write`` scopes). Rate limits are per workspace (60/min on Pro),
so requests are paced and a 429 is waited out.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .extract import clean_email
from .discover import US_STATE_NAMES

log = logging.getLogger(__name__)

BASE_URL = "https://warmysender.com/api/v1"
API_KEY_ENV = "WARMYSENDER_API_KEY"
LEDGER = "warmysender_sync.csv"
LEDGER_FIELDS = ["email", "church_name", "state", "segment", "list_name", "prospect_id", "status", "synced_at", "note"]

# contact_type in the scrape output -> segment name (also the list suffix)
SEGMENTS = {
    "Communications": "Comms",
    "Admin / Office": "Admin",
    "Pastor": "Pastors",
    "Other": "Other",
}

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "aol.com", "msn.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "mac.com", "comcast.net", "bellsouth.net", "att.net",
    "sbcglobal.net", "verizon.net", "charter.net", "ymail.com", "cox.net", "earthlink.net",
}

# Refusals that are about the workspace, not the address: a bad or revoked key
# (401), a missing scope or a full plan ("Prospect limit reached", 403), and
# rate limits (429). They stop the run and leave the rest for a later night,
# instead of being recorded in the ledger as if the address were refused.
WORKSPACE_REFUSALS = {401, 403, 429}


class WarmySenderError(Exception):
    """A request WarmySender refused (HTTP 4xx) or kept failing (5xx, network)."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Building a prospect from a scrape row


def state_name(state: str) -> str:
    return US_STATE_NAMES.get(state.upper(), state)


def segment_for(contact_type: str) -> str:
    return SEGMENTS.get(contact_type, "Other")


def list_name_for(state: str, segment: str) -> str:
    return f"{state_name(state)} {segment}"


def _base_domain(host: str) -> str:
    host = host.lower().removeprefix("www.")
    return ".".join(host.split(".")[-2:])


def review_note(email: str, website: str) -> str:
    """Flag addresses that may belong to another organisation (a diocese, school, vendor...)."""
    domain = email.split("@")[1].lower()
    site = urlparse(website if "://" in website else "https://" + website).hostname or ""
    if domain in FREE_MAIL or not site or _base_domain(domain) == _base_domain(site):
        return ""
    return f"email domain {domain} differs from website {site.removeprefix('www.')}"


def _clean_company(name: str) -> str:
    return re.sub(r"\bChuch\b", "Church", name).strip()


def _role(row: dict[str, str]) -> str:
    title = row.get("role_title", "")
    if not title or "mailbox" in title:
        return row.get("contact_type", "")
    return title.title() if title.isupper() or title.islower() else title


@dataclass
class Prospect:
    email: str
    church_name: str
    state: str
    segment: str
    list_name: str
    body: dict
    tags: list[str]
    review: str = ""
    update: dict = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        return "church-finder-" + hashlib.sha1(self.email.encode("utf-8")).hexdigest()[:20]


def build_prospect(row: dict[str, str]) -> Prospect | None:
    """Turn one church_contacts.csv row into a prospect, or None if it has no usable email."""
    email = clean_email(row.get("best_email") or "")  # the scraper's own validity rules
    if not email:
        return None
    state = (row.get("state") or "").strip().upper()
    if state not in US_STATE_NAMES:
        # The scraper sometimes records the full name ("Illinois"); map it back.
        state = next((code for code, name in US_STATE_NAMES.items() if name.lower() == state.lower()), state)
    city = (row.get("city") or "").strip()
    segment = segment_for(row.get("contact_type", ""))
    church = _clean_company(row.get("church_name", ""))
    website = (row.get("website") or "").strip()
    review = review_note(email, website)

    tags = ["church-finder", state, state_name(state), segment]
    if city:
        tags.append(city)
    if review:
        tags.append("review")

    body = {
        "email": email,
        "company": church,
        "role": _role(row),
        "customFields": {
            "greeting": row.get("contact_greeting") or "there",
            "segment": f"{state_name(state)} {segment}",
            "contact_type": row.get("contact_type", ""),
            "state": state,
            "state_name": state_name(state),
            "city": city,
            "church_website": website,
            "found_on_page": row.get("found_on_page", ""),
            "source": "church-finder",
        },
    }
    if row.get("contact_name"):
        parts = row["contact_name"].split()
        body["firstName"] = parts[0]
        if len(parts) > 1:
            body["lastName"] = parts[-1]
    phone = (row.get("phone") or "").strip()
    if phone:
        body["phone"] = phone
    # A flagged address still goes on the list, but is not auto-enrolled into
    # a campaign linked to it until someone has looked at it.
    body["enroll"] = not review

    update = {"tags": tags, "country": "United States"}
    if website:
        update["website"] = website
    if city:
        update["city"] = city

    return Prospect(
        email=email, church_name=church, state=state, segment=segment,
        list_name=list_name_for(state, segment), body=body, tags=tags, review=review, update=update,
    )


# ---------------------------------------------------------------------------
# The API client

RETRY_ATTEMPTS = 6
RETRY_BASE_DELAY = 5.0
RETRY_MAX_DELAY = 120.0


class WarmySender:
    """A small client for the WarmySender public REST API."""

    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        rpm: int = 50,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not api_key:
            raise ValueError(f"WarmySender needs an API key: pass --api-key or set {API_KEY_ENV}")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests_made = 0

    def _pace(self) -> None:
        """Keep at most ``rpm`` requests a minute, spread evenly."""
        now = self._clock()
        if self.min_interval and self._last_request is not None:
            wait = self.min_interval - (now - self._last_request)
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_request = now

    def request(self, method: str, path: str, json: dict | None = None, params: dict | None = None,
                idempotency_key: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = self.base_url + path
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            self._pace()
            self.requests_made += 1
            try:
                resp = self.session.request(method, url, headers=headers, json=json, params=params, timeout=60)
            except requests.RequestException as exc:
                if attempt == RETRY_ATTEMPTS:
                    raise WarmySenderError(f"WarmySender request failed: {exc}") from exc
                self._sleep(min(RETRY_BASE_DELAY * 2 ** (attempt - 1), RETRY_MAX_DELAY))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == RETRY_ATTEMPTS:
                    raise WarmySenderError(f"WarmySender busy (HTTP {resp.status_code})", resp.status_code)
                delay = _retry_delay(resp, attempt)
                log.warning("WarmySender HTTP %d; retrying in %.0fs (%d/%d)", resp.status_code, delay, attempt, RETRY_ATTEMPTS - 1)
                self._sleep(delay)
                continue
            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {}
            if resp.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
                err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
                message = err.get("message") or resp.text[:200] or f"HTTP {resp.status_code}"
                raise WarmySenderError(f"WarmySender HTTP {resp.status_code}: {message}", resp.status_code, err.get("code", ""))
            return payload
        return {}

    def lists(self) -> dict[str, str]:
        """Every prospect list in the workspace: lower-cased name -> id."""
        found: dict[str, str] = {}
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.request("GET", "/lists", params=params)
            for item in page.get("data", []):
                found[str(item.get("name", "")).strip().lower()] = item["id"]
            pagination = page.get("pagination") or {}
            cursor = pagination.get("next_cursor")
            if not pagination.get("has_more") or not cursor:
                return found

    def create_prospect(self, prospect: Prospect, list_id: str) -> tuple[str, bool]:
        """Create (or merge into) a prospect and put it on a list. Returns (id, already_existed)."""
        body = dict(prospect.body, listId=list_id)
        payload = self.request("POST", "/prospects", json=body, idempotency_key=prospect.idempotency_key)
        data = payload.get("data") or {}
        attached = payload.get("list") or {}
        if attached and attached.get("attached") is False:
            raise WarmySenderError(f"list not attached: {attached.get('message') or attached.get('error')}", 200,
                                   str(attached.get("error", "")))
        return str(data.get("id", "")), bool(payload.get("alreadyExisted"))

    def update_prospect(self, prospect_id: str, fields: dict) -> None:
        self.request("PATCH", f"/prospects/{prospect_id}", json=fields)


def _retry_delay(resp, attempt: int) -> float:
    header = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if header:
        try:
            return min(max(float(header), 1.0), RETRY_MAX_DELAY)
        except ValueError:
            pass
    return min(RETRY_BASE_DELAY * 2 ** (attempt - 1), RETRY_MAX_DELAY)


# ---------------------------------------------------------------------------
# The nightly sync


def read_ledger(data_dir: str) -> dict[str, dict[str, str]]:
    path = os.path.join(data_dir, LEDGER)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["email"]: row for row in csv.DictReader(fh)}


def pending_prospects(data_dir: str, master_csv: str, ledger: dict[str, dict[str, str]]) -> list[Prospect]:
    """Prospects in the master file not yet in the ledger, one per address."""
    pending: list[Prospect] = []
    seen: set[str] = set(ledger)
    if not os.path.exists(master_csv):
        return pending
    with open(master_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            prospect = build_prospect(row)
            if prospect is None or prospect.email in seen:
                continue  # shared inboxes appear once, even across churches
            seen.add(prospect.email)
            pending.append(prospect)
    return pending


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(
    data_dir: str,
    master_csv: str,
    client: WarmySender | None,
    max_per_night: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Push pending prospects to WarmySender. Returns a report for progress.json/STATUS.md.

    Every pushed (or refused) address is appended to the ledger straight away,
    so a run that dies half-way never sends anyone twice.
    """
    ledger = read_ledger(data_dir)
    pending = pending_prospects(data_dir, master_csv, ledger)
    report = {
        "last_sync": _now(), "pending": len(pending), "synced": 0, "existed": 0, "errors": 0,
        "held": 0, "missing_lists": [], "remaining": 0, "dry_run": dry_run, "error": "",
    }
    if not pending:
        log.info("WarmySender: nothing new to push")
        return report
    if client is None:
        raise ValueError(f"WarmySender needs an API key: pass --api-key or set {API_KEY_ENV}")

    lists = client.lists()
    missing: dict[str, int] = {}
    todo: list[tuple[Prospect, str]] = []
    for prospect in pending:
        list_id = lists.get(prospect.list_name.lower())
        if list_id:
            todo.append((prospect, list_id))
        else:
            missing[prospect.list_name] = missing.get(prospect.list_name, 0) + 1
    report["held"] = sum(missing.values())
    report["missing_lists"] = sorted(missing)
    if missing:
        log.warning("WarmySender: %d contacts held back; create these lists in WarmySender: %s",
                    report["held"], ", ".join(f"{name} ({n})" for name, n in sorted(missing.items())))

    batch, rest = todo[:max_per_night], todo[max_per_night:]
    report["remaining"] = len(rest)
    if dry_run:
        for prospect, _ in batch:
            log.info("would push %-40s -> %-28s tags=%s", prospect.email, prospect.list_name, ",".join(prospect.tags))
        log.info("WarmySender dry run: %d would be pushed, %d held back, %d left for later nights",
                 len(batch), report["held"], len(rest))
        return report

    ledger_path = os.path.join(data_dir, LEDGER)
    write_header = not (os.path.exists(ledger_path) and os.path.getsize(ledger_path) > 0)
    with open(ledger_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_FIELDS)
        if write_header:
            writer.writeheader()
        for prospect, list_id in batch:
            entry = {
                "email": prospect.email, "church_name": prospect.church_name, "state": prospect.state,
                "segment": prospect.segment, "list_name": prospect.list_name, "prospect_id": "",
                "status": "", "synced_at": _now(), "note": prospect.review,
            }
            try:
                prospect_id, existed = client.create_prospect(prospect, list_id)
                entry["prospect_id"] = prospect_id
                if existed:
                    # Already in the workspace (an earlier manual import, say):
                    # it is now on the list too, but its tags are left alone.
                    entry["status"] = "existed"
                    report["existed"] += 1
                else:
                    if prospect_id:
                        client.update_prospect(prospect_id, prospect.update)
                    entry["status"] = "synced"
                    report["synced"] += 1
            except WarmySenderError as exc:
                if 400 <= exc.status < 500 and exc.status not in WORKSPACE_REFUSALS:
                    # Refused for this address (bad email, say): record it so it is not retried nightly.
                    entry["status"] = "error"
                    entry["note"] = str(exc)[:200]
                    report["errors"] += 1
                    log.warning("WarmySender refused %s: %s", prospect.email, exc)
                else:
                    report["error"] = str(exc)[:300]
                    report["remaining"] += len(batch) - (report["synced"] + report["existed"] + report["errors"])
                    log.error("WarmySender sync stopped: %s", exc)
                    break
            writer.writerow(entry)
            fh.flush()
    log.info("WarmySender: %d pushed, %d already existed, %d refused, %d held back (lists missing), %d left for later nights",
             report["synced"], report["existed"], report["errors"], report["held"], report["remaining"])
    return report


def status_lines(report: dict | None) -> list[str]:
    """Markdown lines for STATUS.md."""
    if not report:
        return []
    lines = ["", "## WarmySender", ""]
    when = report.get("last_sync", "")[:16].replace("T", " ")
    what = "dry run" if report.get("dry_run") else "sync"
    lines.append(f"Last {what} {when} UTC: {report.get('synced', 0)} pushed, {report.get('existed', 0)} already there, "
                 f"{report.get('errors', 0)} refused, {report.get('held', 0)} held back, "
                 f"{report.get('remaining', 0)} waiting for a later night.")
    if report.get("missing_lists"):
        lines += ["", "Create these lists in WarmySender (Prospects → Lists) so the held-back contacts can be pushed:", ""]
        lines += [f"- {name}" for name in report["missing_lists"]]
    if report.get("error"):
        lines += ["", f"Last run stopped early: {report['error']}"]
    return lines
