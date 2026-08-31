"""phoneloop.ebay.search_url / search_key / is_browse_redirect -- the live
URL shape the sweep depends on.

Measured 2026-08-28 (Chrome for Testing 152, home IP, no login): a
category-only search (`_sacat=` with no `_nkw=`) is redirected by eBay to
the /b/bn_<n> browse page of that category for EVERY shape tried -- plain,
rt=nc, empty _nkw, _sop=12, LH_BIN=1, page 2, and the /sch/<id>/i.html path
form. For a hub such as 26677 that browse page lists nothing at all; the
sweep would have recorded it as done=0 ("swept, genuinely nothing there")
and the board would have stayed empty with every test green. Adding
`_fsrp=1` kept both 26677 and 9394 on a 256-row results page, page 2
included, with the price band applied.

Measured 2026-08-31, the same way: eBay picks the results VIEW per category,
and the gallery card carries no seller line. Leaf 246 returned 169 rows with
a seller on 4.7% of them under the sweep's URL, and 255 rows with a seller on
97.3% under `&_dmd=1`. Pet Supplies leaf 20742 -- already list by default,
96.9% -- dropped to 3.1% when forced to `&_dmd=2`, which is what makes the
view the cause rather than something about toys.
"""
from phoneloop.ebay import search_key, search_url


def test_sacat_search_forces_the_results_page():
    url = search_url("sacat", "26677", 15, 55, 1)
    assert "_fsrp=1" in url
    assert "_sacat=26677" in url
    assert "_udlo=15" in url and "_udhi=55" in url and "_pgn=1" in url


def test_keyword_search_shape_unchanged():
    url = search_url("nkw", "car phone holder", 15, 55, 2)
    assert "_nkw=car+phone+holder" in url
    assert "_fsrp" not in url
    assert "_pgn=2" in url


def test_the_view_is_pinned_on_every_search():
    """Not "toys_games gets list view" -- every search does.

    Leaving it to eBay is what let one department go dark for four nights
    while every sibling read 97%. A per-category default that can change
    under the sweep is the failure, and naming the view on every request is
    the fix.
    """
    for url in (search_url("sacat", "26677", 15, 55, 1),
                search_url("nkw", "car phone holder", 15, 55, 2)):
        assert "_dmd=1" in url


def test_the_key_names_what_changes_the_page():
    """The key renames recordings, so it moves only when it must.

    `_fsrp=1` changed how the page is REACHED -- same leaf, same band, same
    page, same results once you get there -- so it did not belong in the key,
    and this test used to assert exactly that.

    `_dmd` changes what comes BACK. The same leaf, band and page returns a
    gallery card with no seller line or a list card with one, and every
    recording in the cassette before 2026-08-31 is a gallery-era page. Left
    out of the key, replay would serve one under a key that now promises the
    other -- a silently wrong page rather than a missing one, which is the
    single failure mode the harness exists to prevent. In the key, those
    recordings stop matching and replay raises.
    """
    assert search_key("sacat", "26677", 15, 55, 1) == "sacat:26677:15-55:p1:list"
    assert search_key("sacat", "26677", 15, 55, 1) != "sacat:26677:15-55:p1"


def test_browse_node_redirect_is_detected():
    from phoneloop.ebay import is_browse_redirect
    assert is_browse_redirect("https://www.ebay.co.uk/b/bn_821820")
    assert is_browse_redirect("https://www.ebay.co.uk/b/bn_821820?_udlo=15&_pgn=1")
    assert not is_browse_redirect(search_url("sacat", "26677", 15, 55, 1))
    assert not is_browse_redirect("https://www.ebay.co.uk/sch/9394/i.html?_fsrp=1")
