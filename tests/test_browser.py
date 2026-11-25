"""The page, in a browser. A stand-in agent plays the choice through the backend's API."""

import firm
import pytest
from cycling import cycle_layer
from playwright.sync_api import expect, sync_playwright
from stand_ins import scripted_agent

pytestmark = pytest.mark.browser


def test_the_page_shows_the_notes_and_receipts(make_layer):
    layer = make_layer(agent=scripted_agent)
    run = layer.triage()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{layer.url}/")

        expect(page.locator("article.note")).to_have_count(7)
        feed = page.locator('article.note[data-state="chosen"]')
        expect(feed.locator(".subjects li")).to_have_count(15)
        expect(feed.locator(".action")).to_contain_text("feed-outage version 1")
        expect(feed.locator(".action")).to_contain_text("Portfolio Accounting Operations")
        expect(feed.locator(".action")).to_contain_text("blocks nothing")
        expect(feed.locator(".receipt")).to_contain_text("PAS-B pas_snapshots FAILED")
        none = page.locator('article.note[data-state="no_step"] .statement')
        assert none.all_inner_texts() == ["No approved step applies."] * 6
        expect(page.locator("#notices li")).to_have_count(5)
        expect(page.locator("#playbooks li")).to_have_count(2)
        expect(page.locator("#model")).to_contain_text("never given by a model")
        for article, note in zip(page.locator("article.note").all(), run["notes"], strict=True):
            reads = note["receipt"]["reads"]
            receipt = article.locator(".receipt")
            assert receipt.is_visible()
            assert receipt.locator(".trace").all_inner_texts() == [r["trace_id"] for r in reads]
            assert receipt.locator(".result").all_inner_texts() == [r["result_id"] for r in reads]
            shown = receipt.inner_text()
            for part in (note["receipt"]["cutoff"], note["receipt"]["playbook_set"]["hash"]):
                assert part in shown
        assert page.locator("button, form, input").count() == 0

        page.set_viewport_size({"width": 375, "height": 812})
        wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not wide, "the page scrolls sideways on a phone"
        browser.close()


def test_the_button_runs_the_cycle_and_the_page_walks_its_six_steps(make_layer, tmp_path):
    layer = cycle_layer(make_layer, tmp_path)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{layer.url}/")
        expect(page.locator("#cycle-state")).to_contain_text("has not run")
        expect(page.locator("form button")).to_have_count(1)

        page.locator("#run-cycle").click()

        # The page reloads itself while the cycle runs.
        expect(page.locator("#cycle-state")).to_have_attribute("data-state", "done", timeout=60_000)
        steps = page.locator("ol#steps > li.step")
        expect(steps).to_have_count(6)
        read = [step.inner_text() for step in steps.all()]
        assert all("Not yet." not in each for each in read)
        assert read[0].count(f"playbook set version {firm.APPROVED_SET}") == 2
        assert "No approved step applies." in read[0]
        assert "custodian-b-feed-escalation" in read[1] and "gap report" in read[1]
        assert "It rests on:" in read[2] and "correction" in read[2]
        assert "Approved by the playbook owner" in read[3] and "new-rule" in read[3]
        assert read[4].count(f"playbook set version {firm.APPROVED_SET + 1}") == 2
        assert "Custodian B" in read[4] and "Do not hold the advisor's report." in read[4]
        assert "carry yesterday's price forward" in read[5] and "completed no change" in read[5]
        assert "guardrails" in read[5] and "failed" in read[5]
        expect(page.locator("#register-proposals tr")).to_have_count(3)
        expect(page.locator("form")).to_have_count(0)

        page.set_viewport_size({"width": 375, "height": 812})
        wide = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        assert not wide, "the page scrolls sideways on a phone"
        browser.close()
