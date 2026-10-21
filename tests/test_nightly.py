import csv
import json
import shutil

import pytest

import church_finder.crawler as crawler_mod
import church_finder.nightly as nightly
from church_finder.cli import scrape
from church_finder.discover import Church, read_churches
from church_finder.nightly import (
    MAX_FAILURES,
    load_progress,
    merge,
    needs_work,
    parse_target,
    plan,
    read_targets,
    run_target,
    slugify,
)

from .test_crawler import SITE, FakeSession


def test_parse_target_state_and_city():
    assert parse_target("TX") == ("TX", None)
    assert parse_target(" tx ") == ("TX", None)
    assert parse_target("Houston, TX") == ("TX", "Houston")
    assert parse_target("Winston-Salem, nc") == ("NC", "Winston-Salem")
    with pytest.raises(ValueError):
        parse_target("Paris, FR")


def test_slugify():
    assert slugify("Houston, TX") == "houston-tx"
    assert slugify("St. Louis, MO") == "st-louis-mo"


def test_read_targets_skips_comments_and_duplicates(tmp_path):
    f = tmp_path / "targets.txt"
    f.write_text("# queue\nHouston, TX  # big\n\nTX\nhouston, tx\nCA\n")
    assert read_targets(str(f)) == ["Houston, TX", "TX", "CA"]


def test_needs_work_rules():
    assert needs_work(None)
    assert not needs_work({"status": "done"})
    assert needs_work({"status": "partial"})
    assert needs_work({"status": "capped", "max_requests": 3000})  # cut short by the old Google cap
    assert needs_work({"status": "failed", "failures": MAX_FAILURES - 1})
    assert not needs_work({"status": "failed", "failures": MAX_FAILURES})


def test_plan_picks_next_unfinished_in_order():
    progress = {"targets": {"CA": {"status": "done"}, "FL": {"status": "partial"}}}
    chosen = plan(["CA", "TX", "FL", "Houston, TX", "NY"], progress, 3)
    assert [c["target"] for c in chosen] == ["TX", "FL", "Houston, TX"]
    assert chosen[2] == {"target": "Houston, TX", "slug": "houston-tx", "state": "TX", "city": "Houston"}


def test_scrape_skips_already_done_and_stops_at_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    out = tmp_path / "out.csv"
    churches = [Church(name="Grace", website="https://grace.org"), Church(name="Other", website="https://other.org")]
    stats = scrape(churches, str(out), workers=1, delay=0, already_done={"https://grace.org"})
    assert stats == {"fetch_error": 1}  # grace.org skipped, other.org has no fake pages

    out2 = tmp_path / "out2.csv"
    stats = scrape(churches, str(out2), workers=1, delay=0, deadline=0)  # already past
    assert stats["unfinished"] == 1
    assert len(list(csv.DictReader(out2.open()))) == 1


def test_run_and_merge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    calls = []

    def fake_discover(states, city, source):
        calls.append((states, city, source))
        return [Church(name="Grace", website="https://grace.org", city="Houston", state="TX")]

    monkeypatch.setattr(nightly, "discover", fake_discover)
    data = tmp_path / "data"
    data.mkdir()
    results = tmp_path / "results"

    result = run_target("Houston, TX", str(data), str(results / "houston-tx"), "all", 60, 1, delay=0)
    assert calls == [(["TX"], "Houston", "all")]
    assert result["status"] == "done"
    assert result["churches"] == 1 and result["scraped"] == 1 and result["emails"] == 1
    assert (data / "churches" / "houston-tx.csv").exists()

    summary = merge(str(data), str(results))
    assert summary == {"added": 1, "total": 1, "targets": {"Houston, TX": "done"}}
    rows = list(csv.DictReader((data / "church_contacts.csv").open()))
    assert rows[0]["best_email"] == "kim@grace.org"
    progress = load_progress(str(data))
    entry = progress["targets"]["Houston, TX"]
    assert entry["status"] == "done" and entry["contacts"] == 1
    assert "Houston, TX | done" in (data / "STATUS.md").read_text()

    # The state run reuses nothing from discovery but skips the church already in the master file.
    def state_discover(states, city, source):
        return [Church(name="Grace", website="https://grace.org/"), Church(name="New", website="https://new.org")]

    monkeypatch.setattr(nightly, "discover", state_discover)
    result = run_target("TX", str(data), str(results / "tx"), "all", 60, 1, delay=0)
    assert result["scraped"] == 1
    merge(str(data), str(results))
    rows = list(csv.DictReader((data / "church_contacts.csv").open()))
    assert [r["website"] for r in rows] == ["https://grace.org", "https://new.org"]
    progress = load_progress(str(data))
    assert progress["targets"]["TX"]["status"] == "done"
    assert not needs_work(progress["targets"]["TX"])

    # Next night the saved church list is reused instead of discovering again.
    monkeypatch.setattr(nightly, "discover", lambda *a, **k: pytest.fail("should not rediscover"))
    result = run_target("Houston, TX", str(data), str(tmp_path / "r2" / "houston-tx"), "all", 60, 1, delay=0)
    assert result["status"] == "done" and result["scraped"] == 0


def test_merge_keeps_church_list_so_next_night_skips_discovery(tmp_path, monkeypatch):
    """In Actions every job clones the data branch afresh and only results/ is
    passed on, so the church list must travel with the results: run_target
    writes it to --out, merge copies it onto the data branch, and the next
    night's run finds it there instead of discovering again."""
    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    calls = []

    def fake_discover(states, city, source):
        calls.append(states)
        return [Church(name="Grace", website="https://grace.org", city="Houston", state="TX")]

    monkeypatch.setattr(nightly, "discover", fake_discover)
    data = tmp_path / "data"  # the data branch, as the collect job sees it
    data.mkdir()
    results = tmp_path / "results"

    # Night 1: the find job works on its own throwaway clone of the data branch.
    clone = tmp_path / "clone1"
    clone.mkdir()
    result = run_target("Houston, TX", str(clone), str(results / "houston-tx"), "all", 60, 1, delay=0)
    assert calls == [["TX"]]
    assert (results / "houston-tx" / "churches.csv").exists()
    assert not (data / "churches" / "houston-tx.csv").exists()

    merge(str(data), str(results))
    saved = data / "churches" / "houston-tx.csv"
    assert saved.exists()
    assert [c.website for c in read_churches(str(saved))] == ["https://grace.org"]
    assert saved.read_text() == (results / "houston-tx" / "churches.csv").read_text()

    # Night 2: a fresh clone of the data branch already has the list.
    monkeypatch.setattr(nightly, "discover", lambda *a, **k: pytest.fail("should not rediscover"))
    clone2 = tmp_path / "clone2"
    shutil.copytree(data, clone2)
    result = run_target("Houston, TX", str(clone2), str(tmp_path / "r2" / "houston-tx"), "all", 60, 1, delay=0)
    assert result["status"] == "done" and result["churches"] == 1 and calls == [["TX"]]
    merge(str(data), str(tmp_path / "r2"))
    assert load_progress(str(data))["targets"]["Houston, TX"]["nights"] == 2

    # Merging the same results again changes nothing (a re-run's collect job).
    before = saved.read_text()
    merge(str(data), str(results))
    assert saved.read_text() == before


def test_list_cut_short_by_the_old_google_cap_is_rediscovered(tmp_path, monkeypatch):
    """progress.json entries written by the retired Google Places source may say
    "capped": the saved church list is incomplete, so discovery runs again and
    the Google bookkeeping fields are dropped from the entry."""
    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))
    data = tmp_path / "data"
    data.mkdir()
    (data / "churches").mkdir()
    (data / "churches" / "wy.csv").write_text("name,website\nGrace,https://grace.org\n")
    nightly.save_progress(str(data), {"targets": {"WY": {
        "status": "capped", "google_capped": True, "google_requests": 500, "max_requests": 500,
        "churches": 1, "contacts": 1, "nights": 1,
    }}})
    assert needs_work(load_progress(str(data))["targets"]["WY"])

    calls = []

    def fake_discover(states, city, source):
        calls.append(states)
        return [Church(name="Grace", website="https://grace.org"), Church(name="New", website="https://new.org")]

    monkeypatch.setattr(nightly, "discover", fake_discover)
    result = run_target("WY", str(data), str(tmp_path / "r1" / "wy"), "all", 60, 1, delay=0)
    assert calls == [["WY"]] and result["churches"] == 2
    merge(str(data), str(tmp_path / "r1"))
    entry = load_progress(str(data))["targets"]["WY"]
    assert entry["status"] == "done" and entry["nights"] == 2
    assert not any(k in entry for k in ("google_capped", "google_requests", "max_requests"))
    assert [c.website for c in read_churches(str(data / "churches" / "wy.csv"))] == ["https://grace.org", "https://new.org"]

    # Now that it is done, the list is reused.
    monkeypatch.setattr(nightly, "discover", lambda *a, **k: pytest.fail("should not rediscover"))
    assert not needs_work(entry)
    run_target("WY", str(data), str(tmp_path / "r2" / "wy"), "all", 60, 1, delay=0)


def test_run_records_failure_and_merge_counts_it(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("Overpass down")

    monkeypatch.setattr(nightly, "discover", broken)
    data = tmp_path / "data"
    data.mkdir()
    for night in range(MAX_FAILURES):
        result = run_target("WY", str(data), str(tmp_path / f"r{night}" / "wy"), "osm", 60, 1, delay=0)
        assert result["status"] == "failed" and "Overpass down" in result["error"]
        merge(str(data), str(tmp_path / f"r{night}"))
    entry = load_progress(str(data))["targets"]["WY"]
    assert entry["failures"] == MAX_FAILURES and "gave up" in entry["note"]
    assert plan(["WY", "VT"], load_progress(str(data)), 5) == [
        {"target": "VT", "slug": "vt", "state": "VT", "city": ""}
    ]


def test_merge_skips_results_already_counted(tmp_path, monkeypatch):
    """A re-run job downloads the run's older artifacts too; they must not be tallied twice."""
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(crawler_mod, "make_session", lambda: FakeSession(SITE))

    monkeypatch.setattr(nightly, "discover", lambda states, city, source: [Church(name="Grace", website="https://grace.org")])
    data = tmp_path / "data"
    data.mkdir()
    results = tmp_path / "results"
    result = run_target("WY", str(data), str(results / "wy"), "all", 60, 1, delay=0)
    assert result["result_id"] == "123-1"

    first = merge(str(data), str(results))
    again = merge(str(data), str(results))  # same artifacts seen by a re-run's collect job
    assert first["added"] == 1 and again["added"] == 0
    entry = load_progress(str(data))["targets"]["WY"]
    assert entry["nights"] == 1 and entry["result_id"] == "123-1"

    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")  # a genuine re-run is a new result
    run_target("WY", str(data), str(tmp_path / "r2" / "wy"), "all", 60, 1, delay=0)
    merge(str(data), str(tmp_path / "r2"))
    assert load_progress(str(data))["targets"]["WY"]["nights"] == 2
