"""Command line entry point.

    python -m church_finder discover --state AL -o churches.csv
    python -m church_finder scrape -i churches.csv -o contacts.csv
    python -m church_finder run --state AL --city Birmingham -o contacts.csv
    python -m church_finder scrape --url https://www.somechurch.org
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .crawler import SiteCrawler, SiteResult, normalize_url
from .discover import US_STATES, Church, discover_osm, merge_churches, read_churches, write_churches
from .extract import CATEGORY_LABELS, EmailCandidate, EmailHit, _same_org_domain, classify_local_part
from .overture import discover_overture

log = logging.getLogger("church_finder")

OUTPUT_FIELDS = [
    "church_name", "website", "city", "state", "phone",
    "best_email", "contact_type", "role_title", "score", "found_on_page",
    "backup_email", "backup_contact_type", "all_emails", "status", "pages_crawled",
]


def result_row(church: Church, result: SiteResult) -> dict[str, str]:
    row = {
        "church_name": church.name,
        "website": result.website or church.website,
        "city": church.city,
        "state": church.state,
        "phone": church.phone,
        "status": result.status,
        "pages_crawled": str(result.pages_crawled),
    }
    cands = result.candidates
    if cands:
        best = cands[0].best
        row.update({
            "best_email": best.email,
            "contact_type": CATEGORY_LABELS[best.category],
            "role_title": best.title,
            "score": str(best.score),
            "found_on_page": best.source_url,
            "all_emails": "; ".join(c.email for c in cands[:10]),
        })
        # A backup from a different tier, e.g. the office address behind a comms director.
        backup = next((c for c in cands[1:] if c.best.category != best.category), None)
        if backup is None and len(cands) > 1:
            backup = cands[1]
        if backup:
            row["backup_email"] = backup.email
            row["backup_contact_type"] = CATEGORY_LABELS[backup.best.category]
    return {f: row.get(f, "") for f in OUTPUT_FIELDS}


LISTED_SOURCE = "listed in Overture Maps"  # found_on_page value for an address from the church listing


def add_listed_email(church: Church, result: SiteResult) -> SiteResult:
    """Fold the address the discovery source listed for the church into the
    crawl result, scored like any other sighting (by its mailbox name, plus the
    own-domain bonus) so it only wins when the website offered nothing better.
    When the website gave no usable address at all, the row's status becomes
    ``listed`` so it is clear where the contact came from."""
    email = (church.email or "").lower().strip()
    if not email or any(c.email == email for c in result.candidates):
        return result
    score, category, title = classify_local_part(email)
    if not category:
        score, category, title = 10, "other", ""
    site = urlparse(normalize_url(church.website) or "").hostname or ""
    if site and _same_org_domain(email.split("@")[1], site):
        score += 5
    hit = EmailHit(email, score, category, title, "", LISTED_SOURCE)
    if not result.candidates:
        result.status = "listed"
    result.candidates.append(EmailCandidate(email, hit, 1, {LISTED_SOURCE}))
    result.candidates.sort(key=lambda c: (c.best.score, c.sightings, -len(c.email)), reverse=True)
    return result


def _done_websites(path: str) -> set[str]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {_key(r.get("website", "")) for r in csv.DictReader(fh)}


def _key(url: str) -> str:
    return (normalize_url(url) or url).lower().rstrip("/").replace("://www.", "://").replace("http://", "https://")


def scrape(
    churches: list[Church],
    output: str,
    workers: int = 8,
    max_pages: int = 8,
    delay: float = 1.0,
    respect_robots: bool = True,
    resume: bool = True,
    site_timeout: float = 90.0,
    already_done: set[str] | None = None,
    deadline: float | None = None,
) -> dict[str, int]:
    """Crawl every church and append one row per church to ``output``.

    ``already_done`` holds website keys (see ``_key``) to skip on top of those
    already in ``output``, e.g. a master file kept elsewhere. ``deadline`` is a
    ``time.monotonic()`` value; once it passes, no more churches are started and
    the number left over is reported under the ``unfinished`` key of the stats.
    """
    done = _done_websites(output) if resume else set()
    if already_done:
        done |= already_done
    todo = [c for c in churches if _key(c.website) not in done]
    if done:
        log.info("Resuming: %d already done, %d to go", len(churches) - len(todo), len(todo))

    write_header = not (resume and os.path.exists(output) and os.path.getsize(output) > 0)
    stats: dict[str, int] = {}

    def work(church: Church) -> tuple[Church, SiteResult]:
        crawler = SiteCrawler(max_pages=max_pages, delay=delay, respect_robots=respect_robots,
                              max_site_seconds=site_timeout)
        try:
            result = crawler.crawl(church.website)
        except Exception as exc:  # one broken site must not kill a long run
            log.debug("crawl crashed for %s", church.website, exc_info=True)
            result = SiteResult(church.website, status="fetch_error", error=str(exc))
        return church, add_listed_email(church, result)

    with open(output, "a" if not write_header else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        if write_header:
            writer.writeheader()
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(work, c) for c in todo]
        n = 0
        try:
            for fut in as_completed(futures):
                church, result = fut.result()
                n += 1
                row = result_row(church, result)
                writer.writerow(row)
                fh.flush()
                kind = row["contact_type"] or result.status
                stats[kind] = stats.get(kind, 0) + 1
                log.info("[%d/%d] %-40.40s %-16s %s", n, len(todo), church.name or church.website,
                         kind, row["best_email"])
                if deadline is not None and time.monotonic() >= deadline:
                    log.warning("Time limit reached; stopping with %d churches left.", len(todo) - n)
                    break
        finally:
            # cancel_futures drops the churches not yet started; those already
            # running finish (each is capped by site_timeout) but are not written.
            pool.shutdown(wait=True, cancel_futures=True)
    unfinished = len(todo) - n
    if unfinished:
        stats["unfinished"] = unfinished
    return stats


def _parse_states(value: str) -> list[str]:
    if value.lower() in ("all", "usa", "us"):
        return list(US_STATES)
    return [s.strip().upper() for s in value.split(",") if s.strip()]


SOURCES = ("all", "overture", "osm")


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into the environment, without overriding
    variables that are already set. Lets you keep API keys (WarmySender) out of the shell."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if key:
                os.environ.setdefault(key, value)


def discover(states: list[str], city: str | None, source: str) -> list[Church]:
    """Find churches from the chosen source(s): Overture Maps, OpenStreetMap or
    both merged. With ``all``, one source failing is only a warning as long as
    the other delivers; both failing raises."""
    sources = {
        "overture": lambda: discover_overture(states, city),
        "osm": lambda: discover_osm(states, city),
    }
    wanted = list(sources) if source == "all" else [source]
    lists: list[list[Church]] = []
    errors: list[str] = []
    for name in wanted:
        try:
            lists.append(sources[name]())
        except RuntimeError as exc:
            if len(wanted) == 1:
                raise
            # Both sources are free public services; don't throw away the run when one is flaky.
            log.warning("%s failed (%s); continuing with the other source.", name, exc)
            errors.append(f"{name}: {exc}")
    if not lists:
        raise RuntimeError("Every source failed: " + "; ".join(errors))
    churches = merge_churches(*lists)
    if len(lists) > 1:
        log.info("%d churches after merging %s", len(churches), " + ".join(f"{len(l)}" for l in lists))
    return churches


def _add_discover_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state", required=True, help='state code(s), e.g. "AL", "AL,GA,TN" or "all"')
    p.add_argument("--city", help="limit to one city (exact name, e.g. Birmingham)")
    p.add_argument("--source", choices=SOURCES, default="all",
                   help="where to find churches: overture (Overture Maps, most churches), osm (OpenStreetMap) "
                        "or all, both merged (default: %(default)s)")


def _add_scrape_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--output", default="church_contacts.csv", help="output CSV (default: %(default)s)")
    p.add_argument("--workers", type=int, default=8, help="churches crawled in parallel (default: %(default)s)")
    p.add_argument("--max-pages", type=int, default=8, help="max pages fetched per church (default: %(default)s)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests to the same site (default: %(default)s)")
    p.add_argument("--site-timeout", type=float, default=90.0,
                   help="max seconds to spend on one church website (default: %(default)s)")
    p.add_argument("--ignore-robots", action="store_true", help="don't check robots.txt (not recommended)")
    p.add_argument("--no-resume", action="store_true", help="overwrite the output instead of skipping churches already in it")
    p.add_argument("--limit", type=int, help="only process the first N churches (handy for a test run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="church_finder", description="Find church communications-director emails.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="list churches with websites from Overture Maps and/or OpenStreetMap")
    _add_discover_options(d)
    d.add_argument("-o", "--output", default="churches.csv")

    s = sub.add_parser("scrape", help="crawl church websites and pick the best contact email")
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("-i", "--input", help="CSV of churches (needs a website column)")
    src.add_argument("--url", action="append", help="crawl a single church website (repeatable)")
    _add_scrape_options(s)

    r = sub.add_parser("run", help="discover + scrape in one go")
    _add_discover_options(r)
    r.add_argument("--churches-out", default="churches.csv", help="where to save the discovered church list")
    _add_scrape_options(r)

    from . import nightly  # late import: nightly imports helpers from this module
    nightly.add_parser(sub)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.ERROR)
    load_dotenv()

    if args.command == "nightly":
        return nightly.main(args)

    if args.command in ("discover", "run"):
        try:
            churches = discover(_parse_states(args.state), args.city, args.source)
        except (ValueError, RuntimeError) as exc:
            log.error("%s", exc)
            return 1

    if args.command == "discover":
        write_churches(churches, args.output)
        log.info("Saved %d churches to %s", len(churches), args.output)
        return 0

    if args.command == "run":
        write_churches(churches, args.churches_out)
        log.info("Saved %d churches to %s", len(churches), args.churches_out)
    elif args.input:
        churches = read_churches(args.input)
    else:
        churches = [Church(name="", website=u) for u in args.url]

    if args.limit:
        churches = churches[: args.limit]
    if not churches:
        log.error("No churches to scrape.")
        return 1

    stats = scrape(
        churches,
        args.output,
        workers=args.workers,
        max_pages=args.max_pages,
        delay=args.delay,
        respect_robots=not args.ignore_robots,
        resume=not args.no_resume,
        site_timeout=args.site_timeout,
    )
    log.info("Done. Results in %s", args.output)
    for kind, count in sorted(stats.items(), key=lambda kv: -kv[1]):
        log.info("  %-18s %d", kind, count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
