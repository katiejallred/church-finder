from church_finder.extract import (
    classify_text,
    clean_email,
    decode_cfemail,
    extract_emails,
    merge_hits,
    rank_candidates,
)

STAFF_PAGE = """
<html><body>
<nav><a href="/staff">Staff</a></nav>
<div class="staff">
  <div class="card">
    <h3>Rev. John Smith</h3><p>Senior Pastor</p>
    <a href="mailto:jsmith@fictional-gracechurch.org">Email John</a>
  </div>
  <div class="card">
    <h3>Mary Jones</h3><p>Director of Communications</p>
    <a href="mailto:mjones@fictional-gracechurch.org?subject=Hi">Email Mary</a>
  </div>
  <div class="card">
    <h3>Tim Lee</h3><p>Youth Pastor</p>
    <p>tlee@fictional-gracechurch.org</p>
  </div>
  <div class="card">
    <h3>Ann Fox</h3><p>Church Administrator</p>
    <a href="mailto:afox@fictional-gracechurch.org">Email</a>
  </div>
</div>
<footer>Church Office: <a href="mailto:office@fictional-gracechurch.org">office@fictional-gracechurch.org</a></footer>
<script>var x = "tracker@sentry.io";</script>
</body></html>
"""


def best(html, url="https://www.fictional-gracechurch.org/staff"):
    ranked = rank_candidates(merge_hits(extract_emails(html, url)))
    return ranked


def test_comms_director_wins():
    ranked = best(STAFF_PAGE)
    top = ranked[0].best
    assert top.email == "mjones@fictional-gracechurch.org"
    assert top.category == "communications"
    assert "Communications" in top.title


def test_each_person_gets_their_own_title():
    by_email = {c.email: c.best for c in best(STAFF_PAGE)}
    assert by_email["jsmith@fictional-gracechurch.org"].category == "pastor"
    assert by_email["afox@fictional-gracechurch.org"].category == "admin"
    assert by_email["tlee@fictional-gracechurch.org"].category == "other"  # youth pastor isn't "the pastor"
    assert by_email["office@fictional-gracechurch.org"].category == "admin"
    assert "tracker@sentry.io" not in by_email


def test_admin_beats_pastor_when_no_comms():
    html = STAFF_PAGE.replace("Director of Communications", "Worship Leader")
    ranked = best(html)
    assert ranked[0].best.category == "admin"
    assert ranked[0].email == "afox@fictional-gracechurch.org"


def test_pastor_when_nothing_else():
    html = """<div><h3>Pastor Dan Webb</h3><p>Lead Pastor</p>
              <a href="mailto:dan@firstbaptist.org">dan@firstbaptist.org</a></div>"""
    ranked = best(html, "https://firstbaptist.org/")
    assert ranked[0].best.category == "pastor"


def test_cloudflare_protected_email():
    assert decode_cfemail("422f27262b2302242b21362b2d2c232e6f2530232127212a3730212a6c2d3025") == "media@fictional-gracechurch.org"
    html = """<p>Media Team: <a href="/cdn-cgi/l/email-protection#422f27262b2302242b21362b2d2c232e6f2530232127212a3730212a6c2d3025">
              <span class="__cf_email__" data-cfemail="422f27262b2302242b21362b2d2c232e6f2530232127212a3730212a6c2d3025">[email&#160;protected]</span></a></p>"""
    ranked = best(html)
    assert ranked[0].email == "media@fictional-gracechurch.org"
    assert ranked[0].best.category == "communications"


def test_obfuscated_email():
    html = "<p>Communications Coordinator: sarah [at] hopechurch [dot] org</p>"
    ranked = best(html, "https://hopechurch.org/contact")
    assert ranked[0].email == "sarah@hopechurch.org"
    assert ranked[0].best.category == "communications"


def test_mailbox_name_hint():
    html = "<p>Questions? <a href='mailto:communications@stmarks.org'>Write us</a></p>"
    ranked = best(html, "https://stmarks.org")
    assert ranked[0].best.category == "communications"


def test_clean_email_filters_junk():
    assert clean_email("logo@2x.png") is None
    assert clean_email("noreply@church.org") is None
    assert clean_email("you@example.com") is None
    assert clean_email("Office@Church.org.") == "office@church.org"


def test_classify_text():
    assert classify_text("Communications Director")[1] == "communications"
    assert classify_text("Executive Pastor")[1] == "admin"
    assert classify_text("Children's Minister")[1] == "other"
    assert classify_text("Pastor")[1] == "pastor"


def test_clean_email_rejects_fake_tlds_and_vendors():
    # Fragments seen in real output: "pastor@St.Luke's", a line break after "church.or"
    assert clean_email("pastor@st.luke") is None
    assert clean_email("secretary@stlukecchsv.or") is None
    assert clean_email("feel@home.we") is None
    assert clean_email("pastor@grace.he") is None
    # Template placeholders and website vendors
    assert clean_email("info@mydomain.com") is None
    assert clean_email("test@gmail.com") is None
    assert clean_email("blog@wordpress.com") is None
    assert clean_email("support@netministries.org") is None
    # Real ones still pass, including newer and country TLDs
    assert clean_email("hello@fictional-hopetown.church") == "hello@fictional-hopetown.church"
    assert clean_email("office@stmarys.org.uk") == "office@stmarys.org.uk"
    assert clean_email("info@parish.ca") == "info@parish.ca"
