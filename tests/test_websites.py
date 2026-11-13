"""Company websites are normalized on entry, repaired on upgrade, and never break resolve."""

import json

import pytest

from rolescout import cli, companies, db, gaps
from rolescout.resolve import Resolver, resolve_companies
from rolescout.transfer import TransferError, import_companies
from rolescout.urls import InvalidWebsite, normalize_website
from tests.conftest import FakeWeb
from tests.test_resolve import ASHBY, ASHBY_PAGE, GH, GH_BOARD, LEVER, ashby_jobs, ashby_page, boards_of, gh_jobs, lever_jobs, resolution


def website(conn, name):
    return conn.execute("SELECT website FROM companies WHERE name = ?", (name,)).fetchone()[0]


# --- the normal form ---------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("alation.com", "https://alation.com"),
    ("  Alation.COM  ", "https://alation.com"),
    ("www.alation.com/about?x=1", "https://www.alation.com/about?x=1"),
    ("//alation.com", "https://alation.com"),
    ("https://alation.com", "https://alation.com"),
    ("HTTPS://Alation.com/Careers", "https://alation.com/Careers"),   # host lowercased, path kept
    ("http://old.example.co.uk", "http://old.example.co.uk"),         # an explicit scheme is kept
    ("alation.com:8443/jobs", "https://alation.com:8443/jobs"),
    ("alation.com.", "https://alation.com"),
    ("https://alation.com/#team", "https://alation.com/"),
    ("", ""), (None, ""), ("   ", ""),
])
def test_normalize_website(given, expected):
    assert normalize_website(given) == expected
    assert normalize_website(expected) == expected                    # already-normal values do not change


@pytest.mark.parametrize("bad", [
    "alation", "n/a", "none", "TBD", "not a website", "alation dot com", "ftp://alation.com",
    "mailto:jobs@alation.com", "jobs@alation.com", "https://", "http:///careers", "alation..com",
    "-alation.com", "192.168.1.5", "localhost", "https://exa mple.com", "alation.c",
])
def test_obviously_invalid_websites_are_rejected(bad):
    with pytest.raises(InvalidWebsite, match="is not a website"):
        normalize_website(bad)


# --- every entry point -------------------------------------------------------

def test_companies_add_normalizes_and_rejects(conn, capsys):
    assert cli.main(["companies", "add", "Alation", "--website", "alation.com"]) == 0
    assert website(conn, "Alation") == "https://alation.com"
    companies.add_company(conn, "Plain")                               # no website is still fine
    assert website(conn, "Plain") == ""
    capsys.readouterr()
    assert cli.main(["companies", "add", "Broken", "--website", "n/a"]) == 1
    assert "error: 'n/a' is not a website: expected something like example.com" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) FROM companies WHERE name = 'Broken'").fetchone()[0] == 0


def test_csv_import_normalizes_and_a_bad_row_changes_nothing(conn, tmp_path, capsys):
    good = tmp_path / "good.csv"
    good.write_text("company_name,website,tier\nAlation,alation.com,2\nBolt,https://bolt.com,1\nCobalt,,3\n")
    assert companies.import_csv(conn, good) == (3, 0)
    assert [website(conn, n) for n in ("Alation", "Bolt", "Cobalt")] == [
        "https://alation.com", "https://bolt.com", ""]
    # Re-importing a scheme-less value for an existing company normalizes it too.
    good.write_text("company_name,website,tier\nBolt,www.bolt.com,1\n")
    companies.import_csv(conn, good)
    assert website(conn, "Bolt") == "https://www.bolt.com"

    bad = tmp_path / "bad.csv"
    bad.write_text("company_name,website,tier\nDelta,delta.com,1\nEcho,see linkedin,2\n")
    assert cli.main(["companies", "import", str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}: Echo: 'see linkedin' is not a website: it contains a space" in err
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 3      # Delta was not added either


def test_json_import_normalizes_and_rejects(conn):
    def export(site):
        return {"format": "rolescout-companies", "version": 1, "companies": [
            {"name": "Alation", "website": site, "tier": 2, "boards": []},
            {"name": "Plain", "boards": []}]}
    import_companies(conn, export("alation.com"))
    assert (website(conn, "Alation"), website(conn, "Plain")) == ("https://alation.com", "")
    import_companies(conn, export("WWW.Alation.com/"))                  # an update is normalized as well
    assert website(conn, "Alation") == "https://www.alation.com/"
    with pytest.raises(TransferError, match="Alation: 'ftp://alation.com' is not a website"):
        import_companies(conn, export("ftp://alation.com"))
    assert website(conn, "Alation") == "https://www.alation.com/"      # unchanged by the refused file


def test_mcp_add_target_company_normalizes_and_rejects(conn, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path))
    session.call("add_target_company", name="Alation", website="alation.com", tier=2)
    assert website(conn, "Alation") == "https://alation.com"
    listed = session.call("list_target_companies")["companies"]
    assert listed[0]["website"] == "https://alation.com"
    assert "'nope' is not a website" in session.error("add_target_company", name="Broken", website="nope")
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


# --- existing databases ------------------------------------------------------

def test_stored_scheme_less_websites_are_repaired_on_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 4)
    old = db.connect(path)
    assert db.schema_version(old) == 4
    stored = {"Alation": "alation.com", "Bolt": "WWW.Bolt.com/about", "Fine": "https://fine.com",
              "Http": "http://http.com", "Empty": "", "Junk": "ask recruiter"}
    for name, site in stored.items():
        old.execute("INSERT INTO companies (name, website, created_at) VALUES (?, ?, 'x')", (name, site))
    old.close()
    monkeypatch.undo()

    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 5
    assert {r["name"]: r["website"] for r in conn.execute("SELECT name, website FROM companies")} == {
        "Alation": "https://alation.com", "Bolt": "https://www.bolt.com/about",
        "Fine": "https://fine.com", "Http": "http://http.com", "Empty": "",
        "Junk": "ask recruiter"}                                        # not a website: left for resolve to report
    conn.close()
    db.connect(path).close()                                           # opening again changes nothing


# --- resolve -----------------------------------------------------------------

def store_raw(conn, name, site, tier=None):
    """Put a value in the database as an older version would have, bypassing normalization."""
    conn.execute("INSERT INTO companies (name, website, tier, created_at) VALUES (?, ?, ?, 'x')",
                 (name, site, tier))


def test_resolve_of_a_company_stored_without_a_scheme(conn):
    store_raw(conn, "Alation", "alation.com")
    web = FakeWeb({
        "https://alation.com": '<a href="/careers">Careers</a>',
        "https://alation.com/careers": '<a href="https://jobs.lever.co/alation">Open roles</a>',
        LEVER.format("alation"): lever_jobs(4),
    })
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))
    # Every request is a full URL: nothing scheme-less reaches the HTTP layer.
    assert web.urls == ["https://alation.com/robots.txt", "https://alation.com",
                        "https://alation.com/careers", LEVER.format("alation")]
    assert res.outcome == "mapped" and not any("MissingSchema" in note for note in res.notes)
    assert boards_of(conn, "Alation") == [
        ("lever", "alation", "link", "linked from https://alation.com/careers")]


def test_domain_tie_for_guessed_boards_works_without_a_scheme(conn):
    """The tie compares registrable domains, which never needed the scheme."""
    store_raw(conn, "Alation", "alation.com")
    store_raw(conn, "Bolt", "www.bolt.io/home")
    store_raw(conn, "Cobalt", "cobalt.dev")
    web = FakeWeb({
        # Postings that reference the company's domain.
        GH.format("alation"): gh_jobs(3, content="<p>Questions? jobs@alation.com</p>"),
        # A board whose declared website is the company's.
        ASHBY.format("bolt"): ashby_jobs(2),
        ASHBY_PAGE.format("bolt"): ashby_page("Bolt", "https://bolt.io"),
        # A namesake: same slug and name, nothing ties it to cobalt.dev.
        GH.format("cobalt"): gh_jobs(2),
        GH_BOARD.format("cobalt"): {"name": "Cobalt"},
    })
    results = {r.company: r for r in resolve_companies(conn, resolver=Resolver(session=web))}
    assert boards_of(conn, "Alation") == [
        ("greenhouse", "alation", "guess", "3 of 3 postings reference alation.com")]
    assert boards_of(conn, "Bolt") == [("ashby", "bolt", "guess", "board declares website https://bolt.io")]
    assert results["Cobalt"].outcome == "needs_review" and boards_of(conn, "Cobalt") == []
    assert results["Cobalt"].candidates[0].detail == "board is named 'Cobalt', but nothing ties it to cobalt.dev"


def test_a_bad_stored_website_is_reported_as_invalid_not_as_a_fetch_failure(conn):
    store_raw(conn, "Junk", "ask recruiter")
    web = FakeWeb({})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))
    assert res.outcome == "unresolved"
    assert res.notes[0].startswith("invalid website on record ('ask recruiter' is not a website: it contains a space)")
    assert not any("could not fetch" in note or "MissingSchema" in note for note in res.notes)
    # Its site was not read at all; only board lookups by name were tried.
    assert web.urls and all(".greenhouse.io/" in u or ".lever.co/" in u or ".ashbyhq.com/" in u for u in web.urls)
    assert resolution(conn, "Junk")["evidence"]["notes"][0].startswith("invalid website on record")
    entry = next(e for e in gaps.classify(conn) if e["company"] == "Junk")
    assert entry["state"] == "unidentified" and entry["reason"].startswith("invalid website on record")


def test_export_carries_normalized_websites(conn):
    from rolescout.transfer import export_companies
    companies.add_company(conn, "Alation", website="alation.com")
    exported = json.loads(json.dumps(export_companies(conn)))
    assert exported["companies"][0]["website"] == "https://alation.com"
