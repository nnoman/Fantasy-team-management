"""Tests for the published dashboard.

This page goes on a public URL, so the things worth testing are that it renders
at all, that it stays self-contained, and that player names cannot inject markup.
"""

from __future__ import annotations

import re

import pytest

from fpl import dashboard
from fpl.store import Store


@pytest.fixture(scope="module")
def page():
    store = Store()
    try:
        if store.snapshot_count() == 0:
            pytest.skip("no snapshots collected — run `python -m fpl.collect` first")
        yield dashboard.render(store)
    finally:
        store.close()


def test_page_is_well_formed(page):
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    assert page.count("<div") == page.count("</div>")
    assert page.count("<table") == page.count("</table>")
    assert re.search(r"<title>.*?</title>", page)


def test_page_loads_no_external_resources(page):
    """No external CSS, fonts, scripts or images: GitHub Pages serves one file,
    and a CDN going away must not be able to break it.

    Anchor hrefs are fine and deliberately excluded — a link is somewhere the
    reader may choose to go, not something the page fetches to render itself.
    """
    resources = re.findall(
        r'<(?:link|script|img|iframe|source|embed)\s[^>]*?(?:src|href)="(https?://[^"]+)"',
        page, re.I)
    assert resources == [], f"external resources found: {resources}"


def test_outbound_links_are_only_to_github(page):
    links = re.findall(r'<a\s[^>]*?href="(https?://[^"]+)"', page, re.I)
    assert links, "the sync button should be present"
    for url in links:
        assert url.startswith("https://github.com/"), f"unexpected outbound link: {url}"


def test_page_declares_both_themes(page):
    assert "prefers-color-scheme:dark" in page.replace(" ", "")
    assert "--ground" in page


def test_the_plan_is_the_first_thing_on_the_page(page):
    """The page exists to answer "what do I do before Friday", so the transfer
    recommendation comes before the squad and before any model diagnostics."""
    plan_at = page.index("Recommended transfers")
    assert plan_at < page.index("Your squad")
    assert plan_at < page.index("Recent price moves")


def test_squad_and_captain_are_shown(page):
    assert ">C<" in page, "the captain must be marked"
    assert "Your squad" in page
    # 15 owned players, 15 in the model's ideal squad, plus the price table.
    assert page.count("<tr") >= 30


def test_deadline_is_readable_without_javascript(page):
    """The countdown is progressive enhancement; the date itself is in the text."""
    assert re.search(r"\d{2}:\d{2} UTC", page)


def test_page_says_it_is_advisory(page):
    """The page is public and shows a squad; it must not read as an instruction
    that something acted on the team."""
    assert "Advisory only" in page
    assert "nothing here writes to the team" in page


def test_player_names_are_escaped(monkeypatch):
    """Names come from FPL, not from us. An unescaped one would be an injection
    on a page served from the user's own domain."""
    assert dashboard._esc('<img src=x onerror="alert(1)">') == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;")
    assert dashboard._esc("O'Reilly") == "O&#x27;Reilly"


def test_iso_parsing_handles_both_stamp_formats():
    assert dashboard._iso("2026-09-04T17:30:00Z") is not None
    assert dashboard._iso("2026-09-04T17:30:00+00:00") is not None
    assert dashboard._iso("garbage") is None
    assert dashboard._iso(None) is None
