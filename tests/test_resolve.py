"""companies resolve, against a fake internet (no network)."""

import json

import pytest

from rolescout import cli, companies, resolve
from rolescout.resolve import (
    Resolver, domain_pattern, mapping_report, names_match, registrable_domain, resolve_companies,
    scan_for_boards, slug_guesses,
)
from tests.conftest import FakeWeb

GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true"
GH_BOARD = "https://boards-api.greenhouse.io/v1/boards/{}"
LEVER = "https://api.lever.co/v0/postings/{}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true"
ASHBY_PAGE = "https://jobs.ashbyhq.com/{}"


def gh_jobs(n, content="<p>Join us</p>", url="https://job-boards.greenhouse.io/x/jobs/{}"):
    return {"jobs": [{"id": i, "title": f"Role {i}", "absolute_url": url.format(i),
                      "content": content} for i in range(n)]}


def lever_jobs(n, text="Join us"):
    return [{"id": f"id-{i}", "text": f"Role {i}", "hostedUrl": f"https://jobs.lever.co/x/id-{i}",
             "descriptionPlain": text} for i in range(n)]


def ashby_jobs(n, html="<p>Join us</p>"):
    return {"jobs": [{"id": f"id-{i}", "title": f"Role {i}", "descriptionHtml": html,
                      "jobUrl": f"https://jobs.ashbyhq.com/x/id-{i}"} for i in range(n)]}


def ashby_page(name, website):
    return ('<script>window.__appData = {"organization":{"organizationId":"1","name":'
            f'{json.dumps(name)},"publicWebsite":{json.dumps(website)},"theme":{{}}}}}};</script>')


def run_resolve(conn, pages, **kwargs):
    web = FakeWeb(pages)
    results = resolve_companies(conn, resolver=Resolver(session=web), **kwargs)
    return results, web


def boards_of(conn, name):
    return [(b["source"], b["ref"], b["origin"], b["evidence"])
            for c in companies.list_companies(conn) if c["name"] == name for b in c["boards"]]


def resolution(conn, name):
    row = conn.execute(
        "SELECT r.* FROM company_resolutions r JOIN companies c ON c.id = r.company_id "
        "WHERE c.name = ?", (name,)).fetchone()
    return {**dict(row), "evidence": json.loads(row["evidence"])} if row else None


# --- the four required behaviours --------------------------------------------

def test_board_linked_from_the_careers_page_is_saved(conn):
    companies.add_company(conn, "Acme", website="https://acme.com")
    (res,), web = run_resolve(conn, {
        "https://acme.com": '<a href="/about">About</a> <a href="/careers">Careers</a>',
        "https://acme.com/careers": '<a href="https://jobs.lever.co/acme-inc/123-abc">Engineer</a>',
        LEVER.format("acme-inc"): lever_jobs(4),
    })
    assert res.outcome == "mapped"
    assert boards_of(conn, "Acme") == [
        ("lever", "acme-inc", "link", "linked from https://acme.com/careers")]
    assert resolution(conn, "Acme")["outcome"] == "mapped"
    # Strong evidence found: no slug was guessed.
    assert web.urls == ["https://acme.com", "https://acme.com/careers", LEVER.format("acme-inc")]


def test_unconfirmed_guess_is_listed_but_not_saved(conn):
    companies.add_company(conn, "Bolt", website="https://bolt.com")
    (res,), _ = run_resolve(conn, {
        "https://bolt.com": "<p>We make bolts</p>",
        GH.format("bolt"): gh_jobs(5),               # exists, but nothing mentions bolt.com
        GH_BOARD.format("bolt"): {"name": "Bolt"},   # a matching name is not proof
    })
    assert res.outcome == "needs_review"
    assert boards_of(conn, "Bolt") == []
    (cand,) = res.candidates
    assert (cand.source, cand.ref, cand.kind, cand.status, cand.saved) == (
        "greenhouse", "bolt", "guess", "unconfirmed", False)
    assert cand.detail == "board is named 'Bolt', but nothing ties it to bolt.com"
    assert resolution(conn, "Bolt")["evidence"]["candidates"][0]["status"] == "unconfirmed"


def test_slug_owned_by_a_different_company_is_rejected(conn):
    companies.add_company(conn, "Cobalt Robotics", website="https://cobaltrobotics.com")
    (res,), _ = run_resolve(conn, {
        "https://cobaltrobotics.com": "<p>Robots</p>",
        # The domain-label guess "cobaltrobotics" finds nothing; "cobalt-robotics" neither...
        ASHBY.format("cobaltrobotics"): ashby_jobs(3, "<p>Mine cobalt with us at cobaltmining.io</p>"),
        ASHBY_PAGE.format("cobaltrobotics"): ashby_page("Cobalt Mining Corp", "https://cobaltmining.io"),
    })
    assert res.outcome == "unresolved"
    assert boards_of(conn, "Cobalt Robotics") == []
    (cand,) = res.candidates
    assert (cand.status, cand.saved) == ("rejected", False)
    assert cand.detail == "board belongs to 'Cobalt Mining Corp', not 'Cobalt Robotics'"


def test_unsupported_job_system_is_recorded_with_its_url(conn):
    companies.add_company(conn, "Dynamo", website="https://dynamo.com")
    (res,), web = run_resolve(conn, {
        "https://dynamo.com": '<a href="https://dynamo.com/careers">Careers</a>',
        # /careers redirects to Workday.
        "https://dynamo.com/careers": (
            "https://dynamo.wd5.myworkdayjobs.com/en-US/External", "<html>jobs</html>"),
        GH.format("dynamo"): gh_jobs(9, content="<p>Careers at dynamo.com</p>"),  # must not be tried
    })
    assert (res.outcome, res.job_system) == ("unsupported", "workday")
    assert res.job_system_url == "https://dynamo.wd5.myworkdayjobs.com/en-US/External"
    assert boards_of(conn, "Dynamo") == []
    saved = resolution(conn, "Dynamo")
    assert (saved["job_system"], saved["job_system_url"]) == ("workday", res.job_system_url)
    # A careers page that names its job system outranks guessing: no board API was called.
    assert web.urls == ["https://dynamo.com", "https://dynamo.com/careers"]


# --- confirmation, per source ------------------------------------------------

def test_guess_confirmed_by_postings_that_reference_the_company_domain(conn):
    companies.add_company(conn, "Echo Labs", website="https://www.echolabs.ai")
    (res,), _ = run_resolve(conn, {
        "https://www.echolabs.ai": "<div id=app></div>",
        GH.format("echolabs"): gh_jobs(
            4, content="&lt;p&gt;Questions? &lt;a href=&quot;mailto:jobs@echolabs.ai&quot;&gt;mail&lt;/a&gt;&lt;/p&gt;"),
    })
    assert res.outcome == "mapped"
    assert boards_of(conn, "Echo Labs") == [
        ("greenhouse", "echolabs", "guess", "4 of 4 postings reference echolabs.ai")]


def test_greenhouse_job_urls_on_the_company_domain_confirm(conn):
    companies.add_company(conn, "Flux", website="https://flux.io")
    (res,), _ = run_resolve(conn, {
        GH.format("flux"): gh_jobs(3, url="https://flux.io/careers/job?gh_jid={}"),
    })
    assert boards_of(conn, "Flux")[0][3] == "3 of 3 postings reference flux.io"


def test_a_few_stray_mentions_do_not_confirm(conn):
    companies.add_company(conn, "Gamma", website="https://gamma.app")
    jobs = lever_jobs(10)
    jobs[0]["descriptionPlain"] = "Experience with tools like gamma.app is a plus"
    (res,), _ = run_resolve(conn, {
        LEVER.format("gamma"): jobs,
        "https://jobs.lever.co/gamma": "<title>Gamma</title>",
    })
    assert res.outcome == "needs_review"  # 1 of 10 is below the threshold
    assert boards_of(conn, "Gamma") == []


def test_ashby_declared_website_confirms_even_after_a_domain_change(conn):
    companies.add_company(conn, "Helix", website="https://helix.so")
    (res,), _ = run_resolve(conn, {
        # The homepage redirects to the company's newer domain, which becomes an alias.
        "https://helix.so": ("https://www.helix.com/", "<div id=root></div>"),
        ASHBY.format("helix"): ashby_jobs(6),
        ASHBY_PAGE.format("helix"): ashby_page("Helix", "https://www.helix.com/"),
    })
    assert boards_of(conn, "Helix") == [
        ("ashby", "helix", "guess", "board declares website https://www.helix.com/")]


def test_lever_logo_link_confirms_and_title_rejects(conn):
    companies.add_company(conn, "Iris", website="https://iris.dev")
    companies.add_company(conn, "Jade", website="https://jade.dev")
    results, _ = run_resolve(conn, {
        LEVER.format("iris"): lever_jobs(2),
        "https://jobs.lever.co/iris":
            '<title>Iris</title><a href="https://www.iris.dev" class="main-header-logo">',
        LEVER.format("jade"): lever_jobs(2),
        "https://jobs.lever.co/jade":
            '<title>Jade Global Staffing</title><a href="https://jobs.lever.co/jade" class="main-header-logo">',
    })
    by = {r.company: r for r in results}
    assert boards_of(conn, "Iris") == [("lever", "iris", "guess", "board declares website https://www.iris.dev")]
    assert names_match("Jade Global Staffing", "Jade")  # a longer name is not a mismatch...
    assert by["Jade"].outcome == "needs_review"          # ...but it is not proof either
    assert boards_of(conn, "Jade") == []


# --- other outcomes ----------------------------------------------------------

def test_careers_page_with_no_known_system_is_recorded_as_custom(conn):
    companies.add_company(conn, "Kite", website="https://kite.com")
    (res,), _ = run_resolve(conn, {
        "https://kite.com": '<a href="/company/careers">Join us</a>',
        "https://kite.com/company/careers": "<h1>Work at Kite</h1><div id=jobs></div>",
    })
    assert (res.outcome, res.job_system, res.job_system_url) == (
        "unsupported", "custom", "https://kite.com/company/careers")


def test_nothing_found_stays_unresolved(conn):
    companies.add_company(conn, "Lumen", website="https://lumen.example")
    (res,), web = run_resolve(conn, {})
    assert res.outcome == "unresolved"
    assert res.notes[0] == "could not fetch https://lumen.example: HTTP 404"
    # homepage, two fallback careers paths, one slug x three sources
    assert len(web.urls) == 1 + 2 + 3
    assert resolution(conn, "Lumen")["outcome"] == "unresolved"


def test_dead_link_falls_back_to_guessing(conn):
    companies.add_company(conn, "Mango", website="https://mango.co")
    (res,), _ = run_resolve(conn, {
        "https://mango.co": '<a href="https://boards.greenhouse.io/mangoold">Jobs</a>',
        ASHBY.format("mango"): ashby_jobs(2, '<a href="https://mango.co/privacy">Privacy</a>'),
    })
    assert "links to greenhouse/mangoold, but that board does not exist" in res.notes[0]
    assert boards_of(conn, "Mango")[0][:3] == ("ashby", "mango", "guess")


def test_too_many_linked_boards_need_review(conn):
    companies.add_company(conn, "Nova Ventures", website="https://nova.vc")
    links = "".join(f'<a href="https://jobs.lever.co/portfolio{i}">x</a>' for i in range(3))
    (res,), _ = run_resolve(conn, {
        "https://nova.vc": links,
        **{LEVER.format(f"portfolio{i}"): lever_jobs(1) for i in range(3)},
    })
    assert res.outcome == "needs_review"
    assert boards_of(conn, "Nova Ventures") == []


def test_dry_run_saves_nothing(conn):
    companies.add_company(conn, "Acme", website="https://acme.com")
    (res,), _ = run_resolve(conn, {
        "https://acme.com": '<a href="https://jobs.ashbyhq.com/Acme%20Co">Jobs</a>',
        ASHBY.format("Acme Co"): ashby_jobs(1),
    }, dry_run=True)
    assert res.outcome == "mapped" and [c.ref for c in res.to_save] == ["Acme Co"]
    assert boards_of(conn, "Acme") == []
    assert resolution(conn, "Acme") is None


def test_only_unmapped_active_companies_are_resolved_unless_named(conn):
    for name in ("Mapped", "Inactive", "Open"):
        companies.add_company(conn, name, website=f"https://{name.lower()}.com")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    companies.set_active(conn, "Inactive", False)
    results, _ = run_resolve(conn, {})
    assert [r.company for r in results] == ["Open"]
    results, _ = run_resolve(conn, {}, names=["mapped"])
    assert [r.company for r in results] == ["Mapped"]
    with pytest.raises(companies.CompanyError):
        run_resolve(conn, {}, names=["Nope"])


def test_one_company_blowing_up_does_not_stop_the_rest(conn, monkeypatch):
    companies.add_company(conn, "Acme", website="https://acme.com")
    companies.add_company(conn, "Bolt", website="https://bolt.com")
    real = Resolver.resolve

    def flaky(self, company, website):
        if company == "Acme":
            raise RuntimeError("parser exploded")
        return real(self, company, website)
    monkeypatch.setattr(Resolver, "resolve", flaky)
    results, _ = run_resolve(conn, {})
    assert [(r.company, r.outcome) for r in results] == [("Acme", "unresolved"), ("Bolt", "unresolved")]
    assert "parser exploded" in results[0].notes[0]


# --- helpers -----------------------------------------------------------------

def test_scan_finds_boards_in_links_embeds_and_escaped_json():
    page = r'''
        <a href="https://boards.greenhouse.io/acme/jobs/123">x</a>
        <script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeembed"></script>
        <iframe src="https://job-boards.greenhouse.io/embed/job_board?validityToken=1&amp;for=acmeframe"></iframe>
        <script>{"url":"https:\/\/jobs.ashbyhq.com\/Acme.Labs\/abc"}</script>
        <a href="https://jobs.lever.co/Acme-Co">y</a>
        fetch("https://api.lever.co/v0/postings/acmeapi?mode=json")
        <a href="https://acme.wd1.myworkdayjobs.com/Careers">z</a>
        <a href="https://apply.workable.com/acme/">w</a>
        <a href="https://www.bamboohr.com/">we integrate with BambooHR</a>
        <a href="https://jobs.eu.lever.co/acme-eu">eu</a>
    '''
    supported, unsupported = scan_for_boards(page)
    assert set(supported) == {
        ("greenhouse", "acme"), ("greenhouse", "acmeembed"), ("greenhouse", "acmeframe"),
        ("ashby", "Acme.Labs"), ("lever", "acme-co"), ("lever", "acmeapi")}
    assert unsupported == {
        "workday": "https://acme.wd1.myworkdayjobs.com/Careers",
        "workable": "https://apply.workable.com/acme/",
        "lever-eu": "https://jobs.eu.lever.co/acme-eu"}


def test_domain_helpers():
    assert registrable_domain("https://www.jobs.Example.com/x?y") == "example.com"
    assert registrable_domain("careers.example.co.uk") == "example.co.uk"
    assert registrable_domain("") == "" and registrable_domain("localhost") == ""
    ramp = domain_pattern("ramp.com")
    for hit in ("https://ramp.com/careers", "mailto:jobs@ramp.com", "see app.ramp.com.", '"//ramp.com"'):
        assert ramp.search(hit), hit
    for miss in ("xramp.com", "ramp.community", "ramp.com.au", "ramp-com", "tramp.com/x"):
        assert not ramp.search(miss), miss


def test_name_and_slug_helpers():
    assert names_match("Palantir Technologies", "Palantir")
    assert names_match("Scale AI", "scale")
    assert not names_match("Cobalt Mining Corp", "Cobalt Robotics")
    assert slug_guesses("Scale AI", "https://scale.com") == ["scale-ai", "scaleai", "scale"]
    assert slug_guesses("Notion", "https://notion.so") == ["notion"]


# --- report and CLI ----------------------------------------------------------

@pytest.fixture
def resolved(conn):
    for name in ("Acme", "Bolt", "Dynamo", "Kite", "Lumen", "Never"):
        companies.add_company(conn, name, website=f"https://{name.lower()}.com")
    pages = {
        "https://acme.com": '<a href="https://jobs.lever.co/acme">Jobs</a>',
        LEVER.format("acme"): lever_jobs(4),
        "https://bolt.com": "<p>bolts</p>",
        GH.format("bolt"): gh_jobs(5),
        GH_BOARD.format("bolt"): {"name": "Bolt"},
        "https://dynamo.com": '<a href="https://careers.smartrecruiters.com/Dynamo">Careers</a>',
        "https://kite.com": '<a href="/careers">Careers</a>',
        "https://kite.com/careers": "<h1>Jobs</h1>",
    }
    run_resolve(conn, pages, names=["Acme", "Bolt", "Dynamo", "Kite", "Lumen"])
    return pages


def test_report_groups_every_company(conn, resolved):
    report = mapping_report(conn)
    assert report["total"] == 6
    assert [(m["company"], m["ref"], m["origin"]) for m in report["mapped"]["lever"]] == [
        ("Acme", "acme", "link")]
    assert report["unsupported"] == {
        "smartrecruiters": [{"company": "Dynamo", "url": "https://careers.smartrecruiters.com/Dynamo"}],
        "custom": [{"company": "Kite", "url": "https://kite.com/careers"}]}
    assert [(u["company"], u["reason"]) for u in report["unresolved"]] == [
        ("Bolt", "needs review"),
        ("Lumen", "could not fetch https://lumen.com: HTTP 404; could not fetch https://lumen.com/careers: HTTP 404"),
        ("Never", "not resolved yet")]


def test_resolve_and_report_commands(conn, resolved, capsys, monkeypatch):
    companies.add_company(conn, "Mango", website="https://mango.co")
    pages = {**resolved,
             "https://mango.co": "<p>fruit</p>",
             ASHBY.format("mango"): ashby_jobs(2, '<a href="https://mango.co/privacy">Privacy</a>'),
             ASHBY.format("never"): ashby_jobs(1),
             ASHBY_PAGE.format("never"): ashby_page("Zebra Staffing", "")}
    real = resolve.resolve_companies
    monkeypatch.setattr(cli, "resolve_companies", lambda conn, **kw: real(
        conn, **{**kw, "resolver": Resolver(session=FakeWeb(pages))}))

    assert cli.main(["companies", "resolve", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "(dry run: nothing is saved)" in out
    assert "Mango ... would save ashby/mango" in out
    assert "Would save (1):" in out and "2 of 2 postings reference mango.co" in out
    assert boards_of(conn, "Mango") == []

    assert cli.main(["companies", "resolve"]) == 0
    out = capsys.readouterr().out
    assert "Saved (1):" in out
    assert "Not saved, needs your confirmation (1)" in out and "greenhouse/bolt" in out
    assert "Rejected, a different company owns the slug (1):" in out
    assert "board belongs to 'Zebra Staffing', not 'Never'" in out
    assert "Job system detected but not supported (2):" in out
    assert "https://careers.smartrecruiters.com/Dynamo" in out
    assert "Unresolved (2):" in out
    assert "6 companies: 1 mapped, 1 need review, 2 on an unsupported system, 2 unresolved." in out
    assert boards_of(conn, "Mango")[0][:3] == ("ashby", "mango", "guess")

    assert cli.main(["companies", "report"]) == 0
    out = capsys.readouterr().out
    assert "7 active companies: 2 mapped, 2 on an unsupported job system, 3 unresolved." in out
    assert "smartrecruiters" in out and "custom" in out and "needs review" in out

    assert cli.main(["companies", "report", "--json"]) == 0
    assert set(json.loads(capsys.readouterr().out)["mapped"]) == {"lever", "ashby"}

    assert cli.main(["companies", "resolve"]) == 0  # mapped companies are not looked up again
    assert "Mango" not in capsys.readouterr().out
