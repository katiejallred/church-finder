"""Find US churches with a website in Overture Maps' free places dataset.

Overture (overturemaps.org) publishes a monthly, openly licensed snapshot of
tens of millions of places, built mostly from Meta's and Microsoft's business
listings. Because nearly every church has a Facebook Page, its coverage of
churches rivals Google Places, and there is no API key and nothing to pay:
the data is a set of Parquet files on a public S3 bucket, and DuckDB reads
just the rows inside the state's bounding box straight from there. A big
state takes about half a minute; a small one a few seconds.

Every row carries the place's category (``taxonomy``), name, website, phone
and address, which fill the Church record directly. Many rows also list an
email address; it is kept as the fallback contact for churches whose website
turns up nothing.
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

from .discover import US_STATES, Church, _site_key
from .extract import clean_email

log = logging.getLogger(__name__)

BUCKET_URL = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
PLACES_PREFIX = "theme=places/type=place/"
RELEASE_ENV = "OVERTURE_RELEASE"  # pin a release, e.g. 2026-09-23.1 (default: the newest)

# Overture's taxonomy: churches sit under christian_place_of_worship (with the
# denomination as the leaf, e.g. baptist_place_of_worship); ministries,
# dioceses and the like are religious_organization. The non-Christian
# place_of_worship branches (muslim_, jewish_, ...) are left out, like the
# religion=christian filter on the OpenStreetMap source.
CHRISTIAN_BRANCH = "christian_place_of_worship"
EXTRA_CATEGORIES = ("place_of_worship", "religious_organization")

# Overture's confidence that the place still exists, 0..1. Open churches
# score about 0.9; a listing nobody has confirmed in years drifts down.
MIN_CONFIDENCE = 0.4

# Listed "websites" that are not the church's own site, so another listed
# website (if any) is used instead: social media and link pages the scraper
# cannot read, business directories, giving platforms, and denominational
# headquarters sites that hundreds of congregations list as theirs (crawling
# those would hand one church the denomination's national staff).
SKIP_HOSTS = (
    # social / link pages / video
    "facebook.com", "instagram.com", "youtube.com", "twitter.com", "x.com", "tiktok.com",
    "linktr.ee", "zoom.us", "business.site", "google.com", "goo.gl", "vimeo.com",
    # directories
    "yellowpages.com", "yelp.com", "mapquest.com", "manta.com", "dandb.com", "bbb.org",
    "chamberofcommerce.com", "businessfinder.al.com", "churchfinder.com", "churchangel.com",
    "netministries.org", "usachurches.org", "hub.biz", "cylex.us.com",
    # giving
    "tithe.ly", "giv.li", "givelify.com", "pushpay.com", "paypal.com", "onrealm.org", "subsplash.com",
    # denominational headquarters
    "umc.org", "umcna.org", "sbc.net", "ag.org", "churchofgod.cc", "churchofgod.org", "cogop.org",
    "church-of-christ.org", "watchtower.org", "jw.org", "lds.org", "churchofjesuschrist.org",
    "adventist.org", "nazarene.org", "upci.org", "pcusa.org", "disciples.org", "ame-church.com",
    "elca.org", "lcms.org", "episcopalchurch.org", "usccb.org", "cogic.org", "pcanet.org",
)

# (south, west, north, east) in degrees, padded a little past each state line.
# Address region (the state code on each place) is checked too, so the padding
# only costs a few extra rows to read. Alaska is clipped at the antimeridian.
STATE_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.09, -88.53, 35.06, -84.83),
    "AK": (50.95, -180.00, 71.65, -128.94),
    "AZ": (31.28, -114.87, 37.06, -108.99),
    "AR": (32.95, -94.67, 36.55, -89.59),
    "CA": (32.47, -124.54, 42.06, -114.08),
    "CO": (36.94, -109.12, 41.06, -101.99),
    "CT": (40.90, -73.78, 42.11, -71.73),
    "DE": (38.40, -75.84, 39.89, -74.93),
    "DC": (38.74, -77.17, 39.05, -76.85),
    "FL": (24.34, -87.69, 31.06, -79.92),
    "GA": (30.30, -85.66, 35.06, -80.70),
    "HI": (18.81, -178.50, 28.57, -154.70),
    "ID": (41.93, -117.30, 49.06, -110.99),
    "IL": (36.92, -91.57, 42.56, -86.96),
    "IN": (37.72, -88.15, 41.82, -84.73),
    "IA": (40.32, -96.69, 43.56, -90.09),
    "KS": (36.94, -102.11, 40.06, -94.53),
    "KY": (36.44, -89.63, 39.20, -81.91),
    "LA": (28.80, -94.10, 33.07, -88.70),
    "ME": (42.87, -71.14, 47.51, -66.83),
    "MD": (37.83, -79.54, 39.78, -74.93),
    "MA": (41.13, -73.56, 42.94, -69.81),
    "MI": (41.64, -90.47, 48.36, -82.07),
    "MN": (43.44, -97.29, 49.44, -89.43),
    "MS": (30.09, -91.71, 35.05, -88.04),
    "MO": (35.94, -95.83, 40.67, -89.04),
    "MT": (44.30, -116.10, 49.06, -103.98),
    "NE": (39.94, -104.11, 43.06, -95.25),
    "NV": (34.95, -120.06, 42.06, -113.99),
    "NH": (42.64, -72.61, 45.36, -70.51),
    "NJ": (38.74, -75.62, 41.41, -73.84),
    "NM": (31.28, -109.11, 37.06, -102.95),
    "NY": (40.42, -79.82, 45.07, -71.74),
    "NC": (33.70, -84.38, 36.64, -75.35),
    "ND": (45.88, -104.10, 49.06, -96.50),
    "OH": (38.35, -84.88, 42.38, -80.46),
    "OK": (33.56, -103.06, 37.06, -94.38),
    "OR": (41.94, -124.76, 46.35, -116.41),
    "PA": (39.66, -80.58, 42.57, -74.63),
    "RI": (41.04, -71.96, 42.07, -71.03),
    "SC": (31.98, -83.41, 35.27, -78.49),
    "SD": (42.42, -104.11, 46.00, -96.38),
    "TN": (34.93, -90.37, 36.73, -81.59),
    "TX": (25.78, -106.70, 36.56, -93.45),
    "UT": (36.94, -114.11, 42.06, -108.99),
    "VT": (42.67, -73.49, 45.07, -71.41),
    "VA": (36.49, -83.73, 39.52, -75.11),
    "WA": (45.49, -124.89, 49.06, -116.86),
    "WV": (37.15, -82.70, 40.69, -77.66),
    "WI": (42.44, -92.94, 47.36, -86.19),
    "WY": (40.94, -111.11, 45.06, -104.00),
}
assert set(STATE_BBOXES) == set(US_STATES)

_RELEASE_RE = re.compile(r"^release/(\d{4}-\d{2}-\d{2}\.\d+)/$")


def _list_bucket(session: requests.Session, prefix: str, delimiter: str | None = None) -> tuple[list[str], list[str]]:
    """List an S3 prefix anonymously. Returns (keys, common prefixes)."""
    keys: list[str] = []
    prefixes: list[str] = []
    params: dict[str, str] = {"list-type": "2", "prefix": prefix}
    if delimiter:
        params["delimiter"] = delimiter
    while True:
        resp = session.get(BUCKET_URL, params=params, timeout=60)
        resp.raise_for_status()
        text = resp.text
        keys.extend(re.findall(r"<Key>([^<]+)</Key>", text))
        prefixes.extend(re.findall(r"<Prefix>([^<]+)</Prefix>", text))
        token = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
        if not token:
            return keys, prefixes
        params["continuation-token"] = token.group(1)


def latest_release(session: requests.Session | None = None) -> str:
    """The newest release on the bucket, e.g. '2026-09-23.1'. Honours OVERTURE_RELEASE."""
    pinned = os.environ.get(RELEASE_ENV, "").strip()
    if pinned:
        return pinned
    session = session or requests.Session()
    try:
        _, prefixes = _list_bucket(session, "release/", delimiter="/")
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not list Overture releases at {BUCKET_URL}: {exc}") from exc
    releases = sorted(m.group(1) for p in prefixes if (m := _RELEASE_RE.match(p)))
    if not releases:
        raise RuntimeError(f"No Overture releases found at {BUCKET_URL}")
    return releases[-1]


def places_files(release: str, session: requests.Session | None = None) -> list[str]:
    """HTTPS URLs of the places Parquet files in a release."""
    session = session or requests.Session()
    try:
        keys, _ = _list_bucket(session, f"release/{release}/{PLACES_PREFIX}")
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not list Overture release {release}: {exc}") from exc
    files = [BUCKET_URL + k for k in keys if k.endswith(".parquet")]
    if not files:
        raise RuntimeError(f"Overture release {release} has no places files (is the release name right?)")
    return files


def connect():
    """A DuckDB connection able to read Parquet over HTTPS, honouring the usual
    proxy and CA-bundle environment variables."""
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - requirements.txt installs it
        raise RuntimeError("The Overture source needs DuckDB: pip install duckdb") from exc
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        con.execute("SET http_proxy = ?", [proxy])
    ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE")
    if ca_bundle and os.path.exists(ca_bundle):
        con.execute("SET ca_cert_file = ?", [ca_bundle])
    return con


# One row per place: the name, every website, the first phone, the address
# parts, the category and the confidence. Filtering on bbox first lets DuckDB
# skip most of the files' row groups without downloading them.
_QUERY = """
SELECT
    names.primary                      AS name,
    websites                           AS websites,
    phones[1]                          AS phone,
    emails                             AS emails,
    addresses[1].freeform              AS street,
    addresses[1].locality              AS city,
    addresses[1].region                AS state,
    addresses[1].postcode              AS zip,
    taxonomy.primary                   AS category,
    confidence                         AS confidence
FROM read_parquet([{files}])
WHERE bbox.xmin >= ? AND bbox.xmax <= ? AND bbox.ymin >= ? AND bbox.ymax <= ?
  AND websites IS NOT NULL AND len(websites) > 0
  AND names.primary IS NOT NULL
  AND coalesce(operating_status, '') <> 'permanently_closed'
  AND (list_contains(taxonomy.hierarchy, ?) OR taxonomy.primary IN (?, ?))
  AND upper(coalesce(addresses[1].region, '')) = ?
"""


def pick_website(websites: list[str] | None) -> str:
    """The first listed website the scraper can crawl, or ''."""
    for url in websites or []:
        url = (url or "").strip()
        if not url:
            continue
        probe = url if "://" in url else "https://" + url
        host = (urlparse(probe).hostname or "").lower().removeprefix("www.")
        if host and not any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            return url
    return ""


def pick_email(emails: list[str] | None) -> str:
    """The first listed address that passes the scraper's own junk filter, or ''."""
    for raw in emails or []:
        email = clean_email(raw or "")
        if email:
            return email
    return ""


def denomination(category: str | None) -> str:
    """'baptist_place_of_worship' -> 'baptist'; the generic categories give ''."""
    if not category or not category.endswith("_place_of_worship"):
        return ""
    name = category.removesuffix("_place_of_worship")
    return "" if name in ("christian", "protestant") else name


def _row_to_church(row: tuple, state: str) -> Church | None:
    name, websites, phone, emails, street, city, region, zip_code, category, confidence = row
    if confidence is not None and confidence < MIN_CONFIDENCE:
        return None
    website = pick_website(websites)
    name = (name or "").strip()
    if not website or not name:
        return None
    return Church(
        name=name,
        website=website,
        street=(street or "").strip(),
        city=(city or "").strip(),
        state=(region or state).upper(),
        zip=(zip_code or "").strip(),
        phone=(phone or "").strip(),
        email=pick_email(emails),
        denomination=denomination(category),
        source="overture",
    )


def discover_overture(
    states: list[str],
    city: str | None = None,
    release: str | None = None,
    files: list[str] | None = None,
    session: requests.Session | None = None,
    con=None,
) -> list[Church]:
    """Churches with a website in each state (or one city), from Overture Maps.

    ``release`` defaults to the newest on the bucket (or OVERTURE_RELEASE).
    ``files`` overrides the Parquet files to read (local paths work too),
    which the tests use.
    """
    for state in states:
        if state.upper() not in US_STATES:
            raise ValueError(f"Unknown state code: {state}")
    if files is None:
        release = release or latest_release(session)
        files = places_files(release, session)
        log.info("Reading Overture Maps release %s (%d files)", release, len(files))
    con = con or connect()
    query = _QUERY.format(files=",".join("'" + f.replace("'", "''") + "'" for f in files))

    churches: list[Church] = []
    seen: set[str] = set()
    for state in states:
        state = state.upper()
        south, west, north, east = STATE_BBOXES[state]
        where = f"{city}, {state}" if city else state
        log.info("Querying Overture Maps for churches in %s...", where)
        started = time.monotonic()
        try:
            rows = con.execute(query, [west, east, south, north, CHRISTIAN_BRANCH, *EXTRA_CATEGORIES, state]).fetchall()
        except Exception as exc:  # DuckDB raises its own hierarchy; surface one clear error
            raise RuntimeError(f"Overture query for {where} failed: {exc}") from exc
        found = 0
        for row in rows:
            church = _row_to_church(row, state)
            if church is None:
                continue
            if city and church.city.lower() != city.lower():
                continue
            key = _site_key(church.website)
            if key in seen:
                continue
            seen.add(key)
            churches.append(church)
            found += 1
        log.info("  %d churches with websites in %s (%d rows read, %.0fs)", found, where, len(rows),
                 time.monotonic() - started)
    return churches
