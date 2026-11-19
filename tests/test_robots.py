"""robots.txt evaluated as RFC 9309 says, wherever it is checked: the longest
matching rule wins, Allow wins a tie, and * and $ are wildcards."""

from pathlib import Path

import pytest

from rolescout.adapters import SourceError, get_adapter
from rolescout.adapters.workday import WorkdayAdapter
from rolescout.http import USER_AGENT
from rolescout.portfolio import Fetcher
from rolescout.resolve import Resolution, Resolver
from rolescout.robots import ALLOW_ALL, DISALLOW_ALL, Robots, path_of, product_token
from tests.conftest import FakeWeb

FIXTURES = Path(__file__).parent / "fixtures"
ALLOWS_AFTER_DISALLOW_ALL = (FIXTURES / "robots_disallow_all_before_longer_allows.txt").read_text()
QUERIES_AND_WILDCARDS = (FIXTURES / "robots_rules_with_queries_and_wildcards.txt").read_text()
US = "rolescout/9.9 (+https://example.org/about)"


def allowed(text, path, agent=US):
    return Robots.parse(text).allows(agent, "https://site.example" + path)


# --- which rule wins -----------------------------------------------------------

@pytest.mark.parametrize("rules", [
    "Disallow: /\nAllow: /api/apply",          # the order in the file does not matter
    "Allow: /api/apply\nDisallow: /",
])
def test_the_longest_matching_rule_wins_whatever_the_order(rules):
    text = "User-agent: *\n" + rules
    assert allowed(text, "/api/apply/v2/jobs?domain=acme.com&start=0")
    assert allowed(text, "/api/apply")
    assert not allowed(text, "/api/other")
    assert not allowed(text, "/")


def test_allow_wins_when_both_rules_are_equally_long():
    assert allowed("User-agent: *\nDisallow: /page\nAllow: /page", "/page/1")
    assert allowed("User-agent: *\nAllow: /page\nDisallow: /page", "/page/1")
    assert not allowed("User-agent: *\nAllow: /page\nDisallow: /page/", "/page/1")     # one longer
    assert allowed("User-agent: *\nAllow: /page\nDisallow: /page/", "/page")


def test_a_path_no_rule_matches_is_allowed_and_so_is_robots_txt_itself():
    text = "User-agent: *\nDisallow: /private"
    assert allowed(text, "/public") and allowed(text, "/") and allowed(text, "")
    assert allowed("User-agent: *\nDisallow: /", "/robots.txt")
    assert allowed("User-agent: *\nDisallow:", "/anything")           # an empty Disallow disallows nothing
    assert allowed("", "/anything") and ALLOW_ALL.allows(US, "https://site.example/x")
    assert not DISALLOW_ALL.allows(US, "https://site.example/x")


def test_the_verdict_names_the_rule_that_decided():
    rules = Robots.parse("# comment\nUser-agent: *\nDisallow: /   # everything\nAllow: /careers\n")
    refused = rules.check(US, "https://site.example/about")
    assert (refused.allowed, refused.rule.line, refused.rule.text) == (False, 3, "Disallow: /")
    assert refused.reason() == "line 3: Disallow: /"
    let_in = rules.check(US, "https://site.example/careers?domain=acme.com")
    assert (bool(let_in), let_in.reason()) == (True, "line 4: Allow: /careers")
    assert Robots.parse("").check(US, "https://site.example/").reason() == "no rule matches"


# --- wildcards -----------------------------------------------------------------

@pytest.mark.parametrize("pattern, path, matches", [
    ("/jobs", "/jobs/123", True),                    # a plain pattern is a prefix
    ("/jobs", "/job", False),
    ("/jobs$", "/jobs", True),                       # $ is the end of the path
    ("/jobs$", "/jobs/123", False),
    ("/jobs$", "/jobs?page=2", False),               # the query is part of what is compared
    ("/*.json", "/api/list.json", True),             # * is any run of characters
    ("/*.json", "/api/list.json?x=1", True),
    ("/*.json$", "/api/list.json?x=1", False),
    ("/*.json$", "/api/list.json", True),
    ("/a/*/c", "/a/b/c", True),
    ("/a/*/c", "/a/c", False),
    ("/a/*/c", "/a/b/x/c/d", True),
    ("/a*b*c$", "/abbc", True),
    ("/a*b*c$", "/abcb", False),
    ("/a*bc$", "/abcbc", True),                      # the last piece is taken from the end
    ("/results?*&page=", "/results?q=x&page=2", True),
    ("/results?*&page=", "/results?page=2", False),
    ("*", "/anything", True),
    ("/$", "/", True),
    ("/$", "/x", False),
    ("/a$b", "/a$b", True),                          # $ only means the end at the end
])
def test_star_and_dollar_wildcards(pattern, path, matches):
    assert allowed(f"User-agent: *\nDisallow: {pattern}", path) is not matches


def test_many_stars_do_not_make_matching_slow():
    text = "User-agent: *\nDisallow: /" + "*a" * 200 + "$"
    assert allowed(text, "/" + "a" * 5000 + "b")
    assert not allowed(text, "/" + "a" * 5000)


def test_a_longer_pattern_with_a_wildcard_beats_a_shorter_plain_one():
    text = "User-agent: *\nAllow: /shop\nDisallow: /shop/*/checkout\nAllow: /shop/*/checkout/help$"
    assert allowed(text, "/shop/item/1")
    assert not allowed(text, "/shop/item/checkout")
    assert not allowed(text, "/shop/item/checkout/pay")
    assert allowed(text, "/shop/item/checkout/help")


# --- which group applies ---------------------------------------------------------

def test_our_own_group_replaces_the_group_for_everyone():
    named_out = "User-agent: *\nAllow: /\n\nUser-agent: RoleScout\nDisallow: /"
    named_in = "User-agent: *\nDisallow: /\n\nUser-agent: rolescout/1.0\nAllow: /"
    assert not allowed(named_out, "/x") and allowed(named_out, "/x", agent="otherbot/1.0")
    assert allowed(named_in, "/x") and not allowed(named_in, "/x", agent="otherbot/1.0")
    assert product_token(USER_AGENT) == "rolescout"
    # A group for an agent whose name merely starts like ours is not ours.
    assert allowed("User-agent: rolescoutplus\nDisallow: /", "/x")


def test_agents_listed_together_share_rules_and_groups_for_one_agent_add_up():
    text = ("User-agent: a\nUser-agent: rolescout\nDisallow: /one\n\n"
            "Sitemap: https://site.example/sitemap.xml\nCrawl-delay: 5\nDisallow: /two\n\n"   # still the first group
            "User-agent: b\nDisallow: /three\n\n"
            "User-agent: rolescout\nDisallow: /four\n")
    assert [allowed(text, p) for p in ("/one", "/two", "/three", "/four")] == [False, False, True, False]
    assert [allowed(text, p, agent="b") for p in ("/one", "/three")] == [True, False]
    # Field names in any case, spaces around the colon, a byte order mark, old line endings.
    assert not allowed("﻿USER-AGENT : *\r\ndisallow :/x\r\n", "/x/y")


def test_escapes_are_compared_in_one_spelling():
    text = "User-agent: *\nDisallow: /café\nDisallow: /a%7Eb\nDisallow: /q%2fr"
    assert not allowed(text, "/caf%C3%A9/menu") and not allowed(text, "/café")
    assert not allowed(text, "/a~b") and not allowed(text, "/a%7eb")
    assert not allowed(text, "/q%2Fr") and allowed(text, "/q/r")       # an escaped slash is not a slash
    assert path_of("https://site.example") == "/" and path_of("https://site.example/a?b=1#c") == "/a?b=1"


# --- files recorded from live sites ------------------------------------------------

def test_disallow_all_followed_by_longer_allows_lets_the_listed_paths_through():
    rules = Robots.parse(ALLOWS_AFTER_DISALLOW_ALL)
    jobs = rules.check(US, "https://careers.acme.example/api/apply/v2/jobs?domain=acme.example&start=0&num=10")
    assert (jobs.allowed, jobs.reason()) == (True, "line 5: Allow: /api/apply")
    assert rules.check(US, "https://careers.acme.example/api/pcsx/search?domain=acme.example").reason() \
        == "line 6: Allow: /api/pcsx"
    assert rules.allows(US, "https://careers.acme.example/careers?domain=acme.example")
    assert rules.allows(US, "https://careers.acme.example/")                # Allow: /$
    assert rules.check(US, "https://careers.acme.example/api/other").reason() == "line 2: Disallow: /"
    assert not rules.allows(US, "https://careers.acme.example/careerhub")   # only /careerhub/explore/jobs is listed


def test_a_rule_ending_in_a_question_mark_does_not_close_the_whole_site():
    rules = Robots.parse(QUERIES_AND_WILDCARDS)
    site = "https://www.example.com"
    feed = rules.check(US, site + "/about/careers/applications/jobs/feed.xml")
    assert (feed.allowed, feed.rule) == (True, None)                 # "Disallow: /?" is about queries on the root
    assert not rules.allows(US, site + "/?q=x") and rules.allows(US, site + "/")
    assert rules.allows(US, site + "/?hl=en") and not rules.allows(US, site + "/?hl=en&q=x")
    assert rules.allows(US, site + "/?hl=en&gws_rd=ssl") and not rules.allows(US, site + "/?hl=en&gws_rd=ssl&x=1")
    assert rules.allows(US, site + "/shopping?udm=28") and rules.allows(US, site + "/shopping?udm=281")
    assert rules.allows(US, site + "/search/about") and not rules.allows(US, site + "/search?q=x")
    results = site + "/about/careers/applications/jobs/results"
    assert rules.allows(US, results) and rules.allows(US, results + "?q=engineer")
    assert not rules.allows(US, results + "?page=2") and not rules.allows(US, results + "?q=engineer&page=2")
    assert rules.allows(US, site + "/maps/reserve/partners") and not rules.allows(US, site + "/maps/reserve/api/x")
    # An agent named in the first group and in a group of its own obeys both.
    assert not rules.allows("otherbot", results) and not rules.allows("otherbot", site + "/search")
    assert rules.allows("adbot", site + "/search") and rules.allows("adbot", site + "/maps/api/js")
    assert not rules.allows("adbot", site + "/maps/api/js/x")


# --- the same answer wherever robots.txt is checked ------------------------------------

def test_every_caller_reads_robots_txt_the_same_way(conn):
    robots = "User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /wday/cxs/\nAllow: /v1/boards"

    # Adapters (the shared check).
    adapter = get_adapter("greenhouse", FakeWeb({"https://boards.example/robots.txt": robots}))
    adapter._check_robots("https://boards.example/v1/boards/acme/jobs?content=true", "acme")
    with pytest.raises(SourceError, match=r"disallows /v2/x \(board 'acme'; line 2: Disallow: /\)"):
        adapter._check_robots("https://boards.example/v2/x", "acme")

    # Workday has its own check.
    web = FakeWeb({"https://acme.wd5.myworkdayjobs.com/robots.txt": robots})
    workday = WorkdayAdapter(web, min_interval=0, sleep=lambda seconds: None)
    from rolescout.adapters.workday import parse_ref
    workday._check_robots(parse_ref("acme/wd5/Careers"))
    closed = WorkdayAdapter(FakeWeb({"https://acme.wd5.myworkdayjobs.com/robots.txt": "User-agent: *\nAllow: /\nDisallow: /wday/"}),
                            min_interval=0, sleep=lambda seconds: None)
    with pytest.raises(SourceError, match=r"disallows /wday/cxs/acme/Careers/jobs \(line 3: Disallow: /wday/\)"):
        closed._check_robots(parse_ref("acme/wd5/Careers"))

    # Resolve, on a company's own website.
    resolver = Resolver(FakeWeb({"https://acme.com/robots.txt": robots}), delay=0)
    res = Resolution(company="Acme")
    assert resolver._allowed("https://acme.com/careers", res)
    assert resolver._allowed("https://acme.com/careers/engineering?team=data", res)
    assert not resolver._allowed("https://acme.com", res) and not resolver._allowed("https://acme.com/jobs", res)

    # The portfolio board reader.
    fetcher = Fetcher(FakeWeb({"https://jobs.fund.example/robots.txt": robots,
                               "https://jobs.fund.example/careers": "<p>ok</p>"}), delay=0)
    assert fetcher.get("https://jobs.fund.example", "/careers")[0] == 200
    assert fetcher.get("https://jobs.fund.example", "/jobs") == ("robots", "")
