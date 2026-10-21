"""Work through a queue of states and cities one night at a time.

Originally run by a GitHub Actions workflow that has since been removed,
but works anywhere. The queue is a text file of targets, one per line, in
priority order: a state code (``TX``) or a city (``Houston, TX``). Progress
and results live in a data folder that the workflow keeps on a ``data`` git
branch so every night picks up where the last one stopped:

    data/church_contacts.csv   every contact found so far (the master file)
    data/churches/<slug>.csv   the church list discovered for each target
    data/progress.json         what is done, partial or failed
    data/STATUS.md             the same, as a table
    data/warmysender_sync.csv  every contact already pushed to WarmySender

Four steps, so the targets can run as parallel jobs:

    python -m church_finder nightly plan  --targets targets.txt --data data --count 10
    python -m church_finder nightly run   --target "Houston, TX" --data data --out results/houston-tx
    python -m church_finder nightly merge --data data --results results
    python -m church_finder nightly sync  --data data

``plan`` prints the next targets as JSON. ``run`` discovers (or reuses the
saved list for) one target and scrapes only churches not already in the
master file, stopping at a time limit; it writes the church list to its
``--out`` folder too, because in Actions each ``run`` works on a throwaway
clone of the data branch. ``merge`` appends each run's new rows to the master
file, copies each run's church list into ``data/churches/`` so later nights
skip discovery, and updates the progress file. ``sync`` pushes
contacts not yet in WarmySender to their lists, with tags and custom fields
(see warmysender.py).
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone

from .cli import OUTPUT_FIELDS, SOURCES, _done_websites, _key, discover, load_dotenv, scrape
from .discover import US_STATES, read_churches, write_churches
from . import warmysender

log = logging.getLogger("church_finder")

MASTER = "church_contacts.csv"
PROGRESS = "progress.json"
STATUS_MD = "STATUS.md"
CHURCHES_DIR = "churches"
CHURCHES_FILE = "churches.csv"  # the church list, in each run's --out folder
MAX_FAILURES = 3  # give up on a target after this many failed nights in a row


def parse_target(line: str) -> tuple[str, str | None]:
    """'TX' -> ('TX', None); 'Houston, TX' -> ('TX', 'Houston')."""
    text = line.strip()
    if "," in text:
        city, state = (part.strip() for part in text.rsplit(",", 1))
    else:
        city, state = "", text
    state = state.upper()
    if state not in US_STATES:
        raise ValueError(f"Unknown state in target {line!r}")
    return state, (city or None)


def target_name(state: str, city: str | None) -> str:
    return f"{city}, {state}" if city else state


def slugify(target: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")


def read_targets(path: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            state, city = parse_target(line)
            name = target_name(state, city)
            if name.lower() not in seen:  # keep the first spelling: OSM matches city names exactly
                seen.add(name.lower())
                targets.append(name)
    return targets


def load_progress(data_dir: str) -> dict:
    path = os.path.join(data_dir, PROGRESS)
    if not os.path.exists(path):
        return {"targets": {}}
    with open(path, encoding="utf-8") as fh:
        progress = json.load(fh)
    progress.setdefault("targets", {})
    return progress


def save_progress(data_dir: str, progress: dict) -> None:
    progress["updated"] = _now()
    with open(os.path.join(data_dir, PROGRESS), "w", encoding="utf-8") as fh:
        json.dump(progress, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def result_id() -> str:
    """Identifies one night's attempt. In GitHub Actions it is the run id plus
    attempt number, so re-running a job cannot count the run's earlier
    artifacts a second time; elsewhere it is a timestamp."""
    run = os.environ.get("GITHUB_RUN_ID")
    if run:
        return f"{run}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def needs_work(entry: dict | None) -> bool:
    """Should this target run tonight, given what earlier nights recorded?"""
    if not entry:
        return True
    status = entry.get("status")
    if status == "done":
        return False
    if status == "failed":
        return int(entry.get("failures", 0)) < MAX_FAILURES
    # partial, capped (a list the old Google source cut short; see
    # _needs_rediscovery), or anything unexpected
    return True


def _needs_rediscovery(entry: dict) -> bool:
    """Church lists saved by the retired Google Places source were cut short by
    its request cap ("capped"); Overture finds the rest for free, so throw the
    saved list away and discover again."""
    return entry.get("status") == "capped" or bool(entry.get("google_capped"))


def plan(targets: list[str], progress: dict, count: int) -> list[dict]:
    chosen: list[dict] = []
    for target in targets:
        if len(chosen) >= count:
            break
        if needs_work(progress["targets"].get(target)):
            state, city = parse_target(target)
            chosen.append({"target": target, "slug": slugify(target), "state": state, "city": city or ""})
    return chosen


def run_target(
    target: str,
    data_dir: str,
    out_dir: str,
    source: str,
    minutes: float,
    workers: int,
    delay: float = 1.0,
    max_pages: int = 8,
    site_timeout: float = 90.0,
) -> dict:
    """Discover + scrape one target. Writes <out_dir>/contacts.csv (new rows
    only), <out_dir>/churches.csv (the church list, for merge to keep) and
    <out_dir>/result.json, and returns the result."""
    started = time.monotonic()
    deadline = started + minutes * 60
    state, city = parse_target(target)
    slug = slugify(target)
    os.makedirs(out_dir, exist_ok=True)
    churches_dir = os.path.join(data_dir, CHURCHES_DIR)
    os.makedirs(churches_dir, exist_ok=True)
    cache = os.path.join(churches_dir, f"{slug}.csv")
    previous = load_progress(data_dir)["targets"].get(target, {})

    result = {
        "target": target, "slug": slug, "status": "failed", "started": _now(), "result_id": result_id(),
        "churches": 0, "scraped": 0, "unfinished": 0, "emails": 0, "error": "",
    }
    contacts = os.path.join(out_dir, "contacts.csv")
    try:
        # Reuse last night's church list: a big state takes several nights and
        # the list does not change in between. Lists the old Google source cut
        # short are the exception and are discovered again.
        if os.path.exists(cache) and not _needs_rediscovery(previous):
            churches = read_churches(cache)
            log.info("Using the saved church list for %s (%d churches)", target, len(churches))
        else:
            churches = discover([state], city, source)
            write_churches(churches, cache)
            log.info("Saved %d churches for %s to %s", len(churches), target, cache)
        result["churches"] = len(churches)
        # The out folder is what reaches merge (as an artifact); the data folder
        # here may be a clone that is thrown away, so the list travels with the
        # results and merge copies it onto the data branch. Copied as-is: a
        # re-serialised list would lose the source column.
        shutil.copyfile(cache, os.path.join(out_dir, CHURCHES_FILE))

        master_done = _done_websites(os.path.join(data_dir, MASTER))
        stats = scrape(
            churches, contacts, workers=workers, max_pages=max_pages, delay=delay,
            site_timeout=site_timeout, resume=False, already_done=master_done, deadline=deadline,
        )
        result["unfinished"] = stats.pop("unfinished", 0)
        result["scraped"] = sum(stats.values())
        result["emails"] = sum(n for kind, n in stats.items() if kind in ("Communications", "Admin / Office", "Pastor", "Other"))
        result["status"] = "partial" if result["unfinished"] else "done"
    except Exception as exc:  # noqa: BLE001 - record the failure so merge can count it
        log.error("%s failed: %s", target, exc, exc_info=True)
        result["error"] = str(exc)[:500]
    result["finished"] = _now()
    result["minutes"] = round((time.monotonic() - started) / 60, 1)
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result


def merge(data_dir: str, results_dir: str) -> dict:
    """Fold every results/<slug>/ folder into the data folder."""
    progress = load_progress(data_dir)
    master = os.path.join(data_dir, MASTER)
    done = _done_websites(master)
    write_header = not (os.path.exists(master) and os.path.getsize(master) > 0)
    added = 0
    summary: dict[str, str] = {}

    with open(master, "a" if not write_header else "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=OUTPUT_FIELDS)
        if write_header:
            writer.writeheader()
        for result_path in sorted(glob.glob(os.path.join(results_dir, "*", "result.json"))):
            with open(result_path, encoding="utf-8") as fh:
                result = json.load(fh)
            previous = progress["targets"].get(result["target"], {})
            if result.get("result_id") and result["result_id"] == previous.get("result_id"):
                # Already folded in: a re-run job downloads the run's older
                # artifacts too, and counting them twice doubled the tallies.
                log.info("%-24s already merged (%s), skipping", result["target"], result["result_id"])
                continue
            result_dir = os.path.dirname(result_path)
            _keep_church_list(data_dir, result, result_dir)
            contacts = os.path.join(result_dir, "contacts.csv")
            new_rows = 0
            if os.path.exists(contacts) and os.path.getsize(contacts) > 0:
                with open(contacts, newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        key = _key(row.get("website", ""))
                        if key in done:
                            continue
                        done.add(key)
                        writer.writerow({f: row.get(f, "") for f in OUTPUT_FIELDS})
                        new_rows += 1
            added += new_rows
            entry = _update_entry(previous, result, new_rows)
            progress["targets"][result["target"]] = entry
            summary[result["target"]] = entry["status"]
            log.info("%-24s %-8s +%d contacts", result["target"], entry["status"], new_rows)

    save_progress(data_dir, progress)
    write_status(data_dir, progress)
    log.info("Added %d contacts to %s (%d total)", added, master, len(done))
    return {"added": added, "total": len(done), "targets": summary}


def _keep_church_list(data_dir: str, result: dict, result_dir: str) -> None:
    """Copy the run's church list into data/churches/<slug>.csv so the next
    night reuses it instead of discovering again."""
    source = os.path.join(result_dir, CHURCHES_FILE)
    if not (os.path.exists(source) and os.path.getsize(source) > 0):
        return
    slug = result.get("slug") or slugify(result["target"])
    churches_dir = os.path.join(data_dir, CHURCHES_DIR)
    os.makedirs(churches_dir, exist_ok=True)
    shutil.copyfile(source, os.path.join(churches_dir, f"{slug}.csv"))


def sync_warmysender(data_dir: str, api_key: str | None, max_per_night: int, dry_run: bool = False,
                     rpm: int = 50, client: "warmysender.WarmySender | None" = None) -> dict:
    """Push new master-file contacts to WarmySender and record the outcome in progress.json."""
    progress = load_progress(data_dir)
    master = os.path.join(data_dir, MASTER)
    if client is None and api_key:
        client = warmysender.WarmySender(api_key, rpm=rpm)
    try:
        report = warmysender.sync(data_dir, master, client, max_per_night=max_per_night, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - keep the night's data; say what broke in STATUS.md
        report = dict(progress.get("warmysender") or {}, last_sync=_now(), error=str(exc)[:300])
        log.error("WarmySender sync failed: %s", exc, exc_info=True)
    progress["warmysender"] = report
    save_progress(data_dir, progress)
    write_status(data_dir, progress)
    return report


def _update_entry(entry: dict, result: dict, new_rows: int) -> dict:
    entry = dict(entry)
    entry["nights"] = int(entry.get("nights", 0)) + 1
    entry["last_run"] = result.get("finished") or _now()
    if result.get("result_id"):
        entry["result_id"] = result["result_id"]
    entry["churches"] = result.get("churches", entry.get("churches", 0))
    entry["contacts"] = int(entry.get("contacts", 0)) + new_rows
    for legacy in ("google_requests", "google_capped", "max_requests"):
        entry.pop(legacy, None)  # bookkeeping of the retired Google Places source
    status = result["status"]
    if status == "failed":
        entry["failures"] = int(entry.get("failures", 0)) + 1
        entry["error"] = result.get("error", "")
        if entry["failures"] >= MAX_FAILURES:
            entry["note"] = f"gave up after {MAX_FAILURES} failed nights; fix the error and delete this entry to retry"
    else:
        entry["failures"] = 0
        entry.pop("error", None)
        entry.pop("note", None)
    if status == "done":
        entry["status"] = "done"
        entry["finished"] = entry["last_run"]
    else:
        entry["status"] = status
    return entry


def write_status(data_dir: str, progress: dict) -> None:
    rows = sorted(progress["targets"].items(), key=lambda kv: kv[0])
    total = sum(int(e.get("contacts", 0)) for _, e in rows)
    lines = [
        "# Church finder progress",
        "",
        f"Updated {progress.get('updated', '')}. {total} contacts in `{MASTER}`.",
        "",
        "| Target | Status | Churches | Contacts | Nights | Last run | Note |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for target, e in rows:
        note = e.get("note") or e.get("error") or (
            "list cut short by the retired Google source; rediscovered on the next run" if e.get("status") == "capped" else "")
        lines.append(f"| {target} | {e.get('status', '')} | {e.get('churches', 0)} | {e.get('contacts', 0)} | "
                     f"{e.get('nights', 0)} | {e.get('last_run', '')[:10]} | {note} |")
    lines += ["", "Status: `done` = finished, `partial` = more nights needed, `failed` = see note."]
    lines += warmysender.status_lines(progress.get("warmysender"))
    with open(os.path.join(data_dir, STATUS_MD), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def add_parser(sub: argparse._SubParsersAction) -> None:
    n = sub.add_parser("nightly", help="work through a queue of states/cities one night at a time")
    steps = n.add_subparsers(dest="step", required=True)

    p = steps.add_parser("plan", help="print the next targets as JSON")
    p.add_argument("--targets", default="targets.txt")
    p.add_argument("--data", default="data")
    p.add_argument("--count", type=int, default=10, help="targets per night (default: %(default)s)")

    r = steps.add_parser("run", help="discover + scrape one target")
    r.add_argument("--target", required=True, help='"TX" or "Houston, TX"')
    r.add_argument("--data", default="data")
    r.add_argument("--out", required=True, help="folder for this target's contacts.csv and result.json")
    r.add_argument("--source", choices=SOURCES, default="all")
    r.add_argument("--minutes", type=float, default=330, help="stop starting new churches after this long")
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--delay", type=float, default=1.0)
    r.add_argument("--max-pages", type=int, default=8)
    r.add_argument("--site-timeout", type=float, default=90.0)

    m = steps.add_parser("merge", help="fold run results into the data folder")
    m.add_argument("--data", default="data")
    m.add_argument("--results", default="results")

    w = steps.add_parser("sync", help="push contacts not yet in WarmySender to their lists, with tags")
    w.add_argument("--data", default="data")
    w.add_argument("--api-key", default=None, help=f"WarmySender API key (or set {warmysender.API_KEY_ENV})")
    w.add_argument("--max", type=int, default=1000, help="most contacts to push per run (default: %(default)s)")
    w.add_argument("--rpm", type=int, default=50, help="API requests per minute (Pro plans allow 60)")
    w.add_argument("--dry-run", action="store_true", help="show what would be pushed and which lists are missing")


def main(args: argparse.Namespace) -> int:
    load_dotenv()
    if args.step == "plan":
        chosen = plan(read_targets(args.targets), load_progress(args.data), args.count)
        print(json.dumps(chosen))
        return 0
    if args.step == "run":
        result = run_target(
            args.target, args.data, args.out, args.source, args.minutes, args.workers,
            delay=args.delay, max_pages=args.max_pages, site_timeout=args.site_timeout,
        )
        log.info("%s: %s (%d churches, %d scraped tonight, %d left)", args.target, result["status"],
                 result["churches"], result["scraped"], result["unfinished"])
        return 0 if result["status"] != "failed" else 1
    if args.step == "merge":
        merge(args.data, args.results)
        return 0
    if args.step == "sync":
        api_key = args.api_key or os.environ.get(warmysender.API_KEY_ENV)
        report = sync_warmysender(args.data, api_key, args.max, dry_run=args.dry_run, rpm=args.rpm)
        return 1 if report.get("error") else 0
    return 2


if __name__ == "__main__":
    sys.exit(main(argparse.ArgumentParser().parse_args()))
