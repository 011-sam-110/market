"""scripts/bulk_ebay.py -- the checkpoint default and the browse-redirect
guard (integration review 2026-08-28, blocking item [1/3]).

run_daily.cmd runs `--leaf-sweep` without --fresh on purpose (a failed run
must resume, not restart), but the checkpoint used to live at ONE path for
ever, so the 03:00 run on the next day would have skipped every shard the
previous day finished, and Tier 3 could never re-observe the population it
differences. The default is now dated (UTC, matching the store snapshot
dates): a same-day re-run resumes, a new day starts clean.

The redirect guard: eBay answers a category-only search with a redirect to
a /b/ browse node unless `_fsrp=1` is present (tests/test_ebay_search_url.py).
A browse node is not a results page, and its zero rows must never be
recorded as "end of results".
"""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bulk_ebay.py"
_spec = importlib.util.spec_from_file_location("bulk_ebay_checkpoint_under_test", _SCRIPT)
bulk_ebay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bulk_ebay)


def test_default_checkpoint_is_dated():
    p = bulk_ebay.default_checkpoint_path(Path("d"), today=date(2026, 8, 28))
    assert p == Path("d") / "bulk_ebay_checkpoint_2026-08-28.json"


def test_next_day_gets_a_fresh_checkpoint():
    a = bulk_ebay.default_checkpoint_path(Path("d"), today=date(2026, 8, 28))
    b = bulk_ebay.default_checkpoint_path(Path("d"), today=date(2026, 8, 29))
    assert a != b


class _FakePage:
    """Just enough of a Playwright page: goto() lands wherever `final` says."""

    def __init__(self, final: str, body: str = "ok", rows=None):
        self.url = final
        self._body = body
        self._rows = rows or []

    def goto(self, url, **kw):
        return None

    def wait_for_timeout(self, ms):
        return None

    def inner_text(self, sel):
        return self._body

    def evaluate(self, js):
        return self._rows


def test_browse_redirect_is_a_failure_not_an_empty_shard():
    page = _FakePage("https://www.ebay.co.uk/b/bn_821820")
    with pytest.raises(bulk_ebay.RedirectedOffResults):
        bulk_ebay.fetch_page(page, "https://www.ebay.co.uk/sch/i.html?_sacat=26677&_fsrp=1")


def test_results_page_still_returns_rows():
    url = "https://www.ebay.co.uk/sch/i.html?_sacat=26677&_fsrp=1"
    page = _FakePage(url, rows=[{"text": "x", "href": "y"}])
    assert bulk_ebay.fetch_page(page, url) == [{"text": "x", "href": "y"}]


# --- per-shard page cap -------------------------------------------------
# --max-pages budgets the WHOLE run; one 10,000-result hub can spend it all
# before the second shard starts. A recording session that wants N pages of
# every leaf needs a per-shard bound.

def test_pages_per_shard_bounds_each_shard():
    assert list(bulk_ebay.pages_for_shard(3)) == [1, 2, 3]


def test_zero_means_the_cap_derived_maximum():
    assert bulk_ebay.pages_for_shard(0) == range(1, bulk_ebay.MAX_PAGE + 1)
