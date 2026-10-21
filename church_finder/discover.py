"""Find US churches that list a website, using OpenStreetMap's free Overpass API.

Overture Maps is the second (also free, much bigger) source; see overture.py.
Results from several sources are combined with merge_churches(), which drops
duplicates by website. You can also skip discovery and feed the scraper your
own CSV of churches (any export from Google Maps, a denomination directory, a
purchased list...).
"""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass, asdict
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Public Overpass servers, tried in order when one is busy or down.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
OVERPASS_URL = OVERPASS_URLS[0]

# overpass-api.de answers HTTP 406 to anonymous clients (e.g. the default
# "python-requests" User-Agent), so identify the tool like a browser would.
OVERPASS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ChurchFinderBot/1.0; +https://github.com/katiejallred/church-finder)",
    "Referer": "https://github.com/katiejallred/church-finder",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

FIELDS = ["name", "website", "street", "city", "state", "zip", "phone", "email", "denomination", "source"]


@dataclass
class Church:
    name: str
    website: str
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    email: str = ""  # an address listed by the source itself (Overture), used when the website yields none
    denomination: str = ""
    source: str = ""


def _overpass_query(state: str, city: str | None) -> str:
    target = "nwr" + '["amenity"="place_of_worship"]["religion"="christian"]'
    website_filter = '[~"^(website|contact:website|url)$"~"."]'
    if city:
        city_esc = city.replace('"', '\\"')
        return f"""
[out:json][timeout:300];
area["ISO3166-2"="US-{state}"][admin_level=4]->.state;
rel["boundary"="administrative"]["name"="{city_esc}"](area.state);
map_to_area->.city;
{target}{website_filter}(area.city);
out center tags;
"""
    return f"""
[out:json][timeout:300];
area["ISO3166-2"="US-{state}"][admin_level=4]->.state;
{target}{website_filter}(area.state);
out center tags;
"""


def _site_key(url: str) -> str:
    if "://" not in url:
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    path = urlparse(url).path.rstrip("/")
    # Big platforms host many churches under one domain, so keep the path for those.
    if host.endswith(("facebook.com", "wixsite.com", "weebly.com", "blogspot.com", "sites.google.com", "org.uk")):
        return host + path
    return host


def discover_osm(
    states: list[str],
    city: str | None = None,
    session: requests.Session | None = None,
    pause: float = 5.0,
) -> list[Church]:
    session = session or requests.Session()
    churches: list[Church] = []
    seen: set[str] = set()
    for i, state in enumerate(states):
        state = state.upper()
        if state not in US_STATES:
            raise ValueError(f"Unknown state code: {state}")
        if i:
            time.sleep(pause)  # Overpass is a shared free service; be gentle.
        log.info("Querying OpenStreetMap for churches in %s%s...", f"{city}, " if city else "", state)
        data = _post_overpass(session, _overpass_query(state, city))
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            website = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
            name = tags.get("name", "").strip()
            if not website or not name:
                continue
            if "facebook.com" in website.lower():
                continue  # Facebook pages can't be crawled for staff emails.
            key = _site_key(website)
            if key in seen:
                continue
            seen.add(key)
            churches.append(Church(
                name=name,
                website=website.strip(),
                street=" ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")])),
                city=tags.get("addr:city", city or ""),
                state=tags.get("addr:state", state),
                zip=tags.get("addr:postcode", ""),
                phone=tags.get("phone") or tags.get("contact:phone", ""),
                denomination=tags.get("denomination", ""),
                source="openstreetmap",
            ))
        log.info("  %d churches with websites so far", len(churches))
    return churches


def _post_overpass(session: requests.Session, query: str, attempts: int = 4) -> dict:
    """POST a query, rotating through the public Overpass servers when one is busy."""
    delay = 15.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        url = OVERPASS_URLS[(attempt - 1) % len(OVERPASS_URLS)]
        try:
            resp = session.post(url, data={"data": query}, headers=OVERPASS_HEADERS, timeout=360)
            if resp.status_code == 406:
                raise RuntimeError(
                    f"{url} refused the request (HTTP 406). The server wants a descriptive "
                    "User-Agent; this tool sends one, so the server may have blocked this network."
                )
            if resp.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"Overpass busy (HTTP {resp.status_code})")
            resp.raise_for_status()
            return resp.json()
        except RuntimeError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            log.warning("Overpass error at %s (%s); retrying in %.0fs", url, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Overpass query failed after {attempts} attempts: {last_error}")


def merge_churches(*lists: list[Church]) -> list[Church]:
    """Combine church lists from several sources, keeping the first entry per website."""
    merged: list[Church] = []
    seen: set[str] = set()
    for churches in lists:
        for church in churches:
            key = _site_key(church.website)
            if key in seen:
                continue
            seen.add(key)
            merged.append(church)
    return merged


def write_churches(churches: list[Church], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for church in churches:
            writer.writerow(asdict(church))


def read_churches(path: str) -> list[Church]:
    """Read a CSV of churches. Needs a website column; other columns are optional.

    Header names are matched loosely, so "Church Name", "URL", "Website",
    "City", "State" etc. all work.
    """
    aliases = {
        "name": ("name", "church", "church name", "church_name", "organization", "title"),
        "website": ("website", "url", "site", "web", "homepage", "domain", "website url"),
        "street": ("street", "address", "street address"),
        "city": ("city", "town"),
        "state": ("state", "st", "region"),
        "zip": ("zip", "zipcode", "zip code", "postal code", "postcode"),
        "phone": ("phone", "telephone", "phone number"),
        "email": ("email", "e-mail", "email address", "contact email"),
        "denomination": ("denomination", "affiliation"),
    }
    churches: list[Church] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header_map: dict[str, str] = {}
        for col in reader.fieldnames or []:
            norm = col.strip().lower()
            for field, names in aliases.items():
                if norm in names and field not in header_map:
                    header_map[field] = col
        if "website" not in header_map:
            raise ValueError(f"{path}: no website/url column found (columns: {reader.fieldnames})")
        for row in reader:
            values = {f: (row.get(c) or "").strip() for f, c in header_map.items()}
            if not values.get("website"):
                continue
            values.setdefault("name", "")
            churches.append(Church(**values, source="csv"))
    return churches
