
import duckdb
import pytest

from church_finder.discover import Church
from church_finder.overture import (
    BUCKET_URL,
    STATE_BBOXES,
    denomination,
    discover_overture,
    latest_release,
    pick_website,
    places_files,
)


class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeBucket:
    """Answers S3 ListObjectsV2 requests for a few releases, one page at a time."""

    def __init__(self):
        self.calls = []

    def get(self, url, params, **kwargs):
        self.calls.append(params)
        assert url == BUCKET_URL
        prefix = params["prefix"]
        if prefix == "release/" and params.get("delimiter") == "/":
            body = "".join(f"<CommonPrefixes><Prefix>release/{r}/</Prefix></CommonPrefixes>"
                           for r in ("2026-08-19.0", "2026-09-23.1", "2026-09-23.0"))
            return FakeResponse(f"<ListBucketResult>{body}</ListBucketResult>")
        if prefix.startswith("release/2026-09-23.1/theme=places/type=place/"):
            if "continuation-token" not in params:
                return FakeResponse(
                    f"<ListBucketResult><Contents><Key>{prefix}part-00000.zstd.parquet</Key></Contents>"
                    "<NextContinuationToken>page2</NextContinuationToken></ListBucketResult>"
                )
            return FakeResponse(
                f"<ListBucketResult><Contents><Key>{prefix}part-00001.zstd.parquet</Key></Contents>"
                f"<Contents><Key>{prefix}_SUCCESS</Key></Contents></ListBucketResult>"
            )
        return FakeResponse("<ListBucketResult></ListBucketResult>")


def test_latest_release_picks_newest_unless_pinned(monkeypatch):
    monkeypatch.delenv("OVERTURE_RELEASE", raising=False)
    assert latest_release(FakeBucket()) == "2026-09-23.1"
    monkeypatch.setenv("OVERTURE_RELEASE", "2026-01-21.0")
    assert latest_release(FakeBucket()) == "2026-01-21.0"


def test_places_files_follows_pagination_and_keeps_parquet_only():
    bucket = FakeBucket()
    files = places_files("2026-09-23.1", bucket)
    assert files == [
        BUCKET_URL + "release/2026-09-23.1/theme=places/type=place/part-00000.zstd.parquet",
        BUCKET_URL + "release/2026-09-23.1/theme=places/type=place/part-00001.zstd.parquet",
    ]
    assert len(bucket.calls) == 2 and bucket.calls[1]["continuation-token"] == "page2"
    with pytest.raises(RuntimeError, match="no places files"):
        places_files("2020-01-01.0", FakeBucket())


def test_pick_website_skips_uncrawlable_hosts():
    assert pick_website(["https://www.facebook.com/gracechurch", "http://grace.org/"]) == "http://grace.org/"
    assert pick_website(["https://m.facebook.com/x", "https://linktr.ee/x", "https://us06web.zoom.us/j/1"]) == ""
    assert pick_website(["grace.org"]) == "grace.org"
    assert pick_website(None) == "" and pick_website([None, ""]) == ""
    # Directories, giving pages and denominational headquarters are not the church's own site.
    assert pick_website(["https://www.yellowpages.com/x", "https://give.tithe.ly/x", "https://www.umc.org/"]) == ""
    # Only the host is matched, not a church whose own domain merely ends the same way.
    assert pick_website(["https://notfacebook.com"]) == "https://notfacebook.com"
    assert pick_website(["https://firstumc.org"]) == "https://firstumc.org"


def test_denomination_from_category():
    assert denomination("baptist_place_of_worship") == "baptist"
    assert denomination("anglican_or_episcopal_place_of_worship") == "anglican_or_episcopal"
    assert denomination("christian_place_of_worship") == ""
    assert denomination("protestant_place_of_worship") == ""
    assert denomination("religious_organization") == ""
    assert denomination(None) == ""


def test_state_bboxes_are_sane():
    for code, (south, west, north, east) in STATE_BBOXES.items():
        assert -90 < south < north < 90 and -180 <= west < east < 0, code


def _place(name, lon, lat, websites, hierarchy, *, city="Birmingham", region="AL", confidence=0.92,
           status="open", phone="+12055550100", postcode="35203", street="100 Main St", emails=None):
    return {
        "name": name, "lon": lon, "lat": lat, "websites": websites, "primary": hierarchy[-1],
        "hierarchy": hierarchy, "city": city, "region": region, "confidence": confidence,
        "status": status, "phone": phone, "postcode": postcode, "street": street, "emails": emails,
    }


CHRISTIAN = ["cultural_and_historic", "place_of_worship", "christian_place_of_worship"]
BAPTIST = CHRISTIAN + ["protestant_place_of_worship", "baptist_place_of_worship"]
MUSLIM = ["cultural_and_historic", "place_of_worship", "muslim_place_of_worship"]
MINISTRY = ["cultural_and_historic", "religious_organization"]
SHOP = ["shopping", "specialty_store", "religious_and_spiritual_goods_store"]


def write_places(path, places):
    """Write a Parquet file with the parts of Overture's places schema the query reads."""
    con = duckdb.connect()
    con.execute("""
        CREATE TABLE places AS
        SELECT
            struct_pack("primary" := name)                                            AS names,
            websites, phones, emails,
            [struct_pack(freeform := street, locality := city, postcode := postcode,
                         region := region, country := 'US')]                          AS addresses,
            struct_pack("primary" := category, hierarchy := hierarchy, alternates := NULL::VARCHAR[]) AS taxonomy,
            confidence, operating_status,
            struct_pack(xmin := lon, xmax := lon, ymin := lat, ymax := lat)           AS bbox
        FROM (SELECT
            unnest(?::VARCHAR[]) AS name, unnest(?::VARCHAR[][]) AS websites, unnest(?::VARCHAR[][]) AS phones,
            unnest(?::VARCHAR[][]) AS emails,
            unnest(?::VARCHAR[]) AS street, unnest(?::VARCHAR[]) AS city, unnest(?::VARCHAR[]) AS postcode,
            unnest(?::VARCHAR[]) AS region, unnest(?::VARCHAR[]) AS category, unnest(?::VARCHAR[][]) AS hierarchy,
            unnest(?::DOUBLE[]) AS confidence, unnest(?::VARCHAR[]) AS operating_status,
            unnest(?::DOUBLE[]) AS lon, unnest(?::DOUBLE[]) AS lat)
    """, [
        [p["name"] for p in places], [p["websites"] for p in places],
        [[p["phone"]] if p["phone"] else None for p in places],
        [p["emails"] for p in places],
        [p["street"] for p in places], [p["city"] for p in places], [p["postcode"] for p in places],
        [p["region"] for p in places], [p["primary"] for p in places], [p["hierarchy"] for p in places],
        [p["confidence"] for p in places], [p["status"] for p in places],
        [p["lon"] for p in places], [p["lat"] for p in places],
    ])
    con.execute("COPY places TO ? (FORMAT PARQUET)", [str(path)])
    return str(path)


def test_discover_overture_filters_and_maps_rows(tmp_path):
    bham = (-86.8, 33.5)
    places = [
        _place("Grace Baptist", *bham, ["https://www.facebook.com/grace", "https://grace.org"], BAPTIST,
               emails=["not an email", "Office@Grace.org"]),
        _place("Grace Baptist (dup)", *bham, ["http://grace.org/"], BAPTIST),  # same website, dropped
        _place("Hope Church", *bham, ["https://hope.org"], CHRISTIAN, city="Hoover", phone=None, street=None),
        _place("City Ministries", *bham, ["https://cityministries.org"], MINISTRY),
        _place("Masjid", *bham, ["https://masjid.org"], MUSLIM),  # not a church
        _place("Bible Store", *bham, ["https://store.com"], SHOP),
        _place("Closed Chapel", *bham, ["https://closed.org"], CHRISTIAN, status="permanently_closed"),
        _place("Doubtful Chapel", *bham, ["https://doubtful.org"], CHRISTIAN, confidence=0.2),
        _place("Facebook Only", *bham, ["https://facebook.com/only"], CHRISTIAN),
        _place("Across The Line", -85.0, 34.99, ["https://georgia.org"], CHRISTIAN, city="Columbus", region="GA"),
        _place("Out West", -118.0, 34.0, ["https://la.org"], CHRISTIAN, city="Los Angeles", region="CA"),
        _place("Nameless", *bham, ["https://noname.org"], CHRISTIAN),
    ]
    places[-1]["name"] = None
    f = write_places(tmp_path / "part-00000.parquet", places)

    churches = discover_overture(["AL"], files=[f])
    assert [c.name for c in churches] == ["Grace Baptist", "Hope Church", "City Ministries"]
    assert churches[0] == Church(
        name="Grace Baptist", website="https://grace.org", street="100 Main St", city="Birmingham", state="AL",
        zip="35203", phone="+12055550100", email="office@grace.org", denomination="baptist", source="overture",
    )
    assert churches[1].phone == "" and churches[1].street == "" and churches[1].denomination == ""
    assert churches[1].email == ""

    assert [c.name for c in discover_overture(["AL"], city="hoover", files=[f])] == ["Hope Church"]
    assert [c.name for c in discover_overture(["GA"], files=[f])] == ["Across The Line"]
    assert [c.name for c in discover_overture(["AL", "CA"], files=[f])] == [
        "Grace Baptist", "Hope Church", "City Ministries", "Out West"]
    with pytest.raises(ValueError, match="Unknown state"):
        discover_overture(["ZZ"], files=[f])


def test_discover_overture_wraps_query_errors(tmp_path):
    missing = str(tmp_path / "nope.parquet")
    with pytest.raises(RuntimeError, match="Overture query for AL failed"):
        discover_overture(["AL"], files=[missing])
