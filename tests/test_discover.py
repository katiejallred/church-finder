import os

import pytest

from church_finder.discover import Church, merge_churches


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_merge_churches_keeps_first_per_website():
    osm = [Church(name="A", website="https://a.org", source="openstreetmap")]
    overture = [Church(name="A again", website="http://www.a.org/", source="overture"),
                Church(name="B", website="https://b.org", source="overture")]
    merged = merge_churches(osm, overture)
    assert [(c.name, c.source) for c in merged] == [("A", "openstreetmap"), ("B", "overture")]


def test_load_dotenv_sets_but_does_not_override(tmp_path, monkeypatch):
    from church_finder.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text('# comment\nWARMYSENDER_API_KEY="abc123"\nexport OTHER=x\nALREADY=new\n\nbroken line\n')
    monkeypatch.delenv("WARMYSENDER_API_KEY", raising=False)
    monkeypatch.setenv("ALREADY", "old")
    load_dotenv(str(env))
    assert os.environ["WARMYSENDER_API_KEY"] == "abc123"
    assert os.environ["OTHER"] == "x"
    assert os.environ["ALREADY"] == "old"
    load_dotenv(str(tmp_path / "missing.env"))  # no error


class FakeOverpass:
    """Answers with a status per URL (default 200 with one church), recording headers."""

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.calls = []

    def post(self, url, data, headers, **kwargs):
        self.calls.append((url, headers))
        status = self.statuses.get(url, 200)
        payload = {"elements": [{"tags": {"name": "First Church", "website": "https://first.org",
                                          "addr:city": "Birmingham"}}]}
        resp = FakeResponse(payload if status == 200 else {}, status=status)
        return resp


def test_overpass_sends_identifying_headers_and_rotates_mirrors(monkeypatch):
    import church_finder.discover as d
    from church_finder.discover import discover_osm

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    fake = FakeOverpass({d.OVERPASS_URLS[0]: 504})
    churches = discover_osm(["AL"], session=fake)
    assert [c.name for c in churches] == ["First Church"]
    assert [u for u, _ in fake.calls] == d.OVERPASS_URLS[:2]
    ua = fake.calls[0][1]["User-Agent"]
    assert "ChurchFinderBot" in ua and "python-requests" not in ua
    assert "Referer" in fake.calls[0][1]


def test_overpass_406_fails_fast_with_clear_message(monkeypatch):
    import church_finder.discover as d
    from church_finder.discover import discover_osm

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    fake = FakeOverpass({u: 406 for u in d.OVERPASS_URLS})
    with pytest.raises(RuntimeError, match="406"):
        discover_osm(["AL"], session=fake)
    assert len(fake.calls) == 1


def test_source_all_survives_one_source_failing(monkeypatch, caplog):
    import church_finder.cli as cli

    def broken(states, city):
        raise RuntimeError("service down")

    overture = [Church(name="O", website="https://o.org", source="overture")]
    osm = [Church(name="O again", website="http://www.o.org/", source="openstreetmap"),
           Church(name="S", website="https://s.org", source="openstreetmap")]

    monkeypatch.setattr(cli, "discover_overture", lambda states, city: overture)
    monkeypatch.setattr(cli, "discover_osm", lambda states, city: osm)
    assert [c.name for c in cli.discover(["AL"], None, "all")] == ["O", "S"]
    assert [c.name for c in cli.discover(["AL"], None, "osm")] == ["O again", "S"]

    monkeypatch.setattr(cli, "discover_osm", broken)
    assert [c.name for c in cli.discover(["AL"], None, "all")] == ["O"]
    assert "osm failed" in caplog.text and "continuing with the other source" in caplog.text
    with pytest.raises(RuntimeError, match="service down"):
        cli.discover(["AL"], None, "osm")

    monkeypatch.setattr(cli, "discover_overture", broken)
    with pytest.raises(RuntimeError, match="Every source failed"):
        cli.discover(["AL"], None, "all")
