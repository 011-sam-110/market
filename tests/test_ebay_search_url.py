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


def test_search_key_is_independent_of_the_url_fix():
    # Recordings are named by this key; changing the URL must not rename
    # them, or every recording captured today would go missing again.
    assert search_key("sacat", "26677", 15, 55, 1) == "sacat:26677:15-55:p1"


def test_browse_node_redirect_is_detected():
    from phoneloop.ebay import is_browse_redirect
    assert is_browse_redirect("https://www.ebay.co.uk/b/bn_821820")
    assert is_browse_redirect("https://www.ebay.co.uk/b/bn_821820?_udlo=15&_pgn=1")
    assert not is_browse_redirect(search_url("sacat", "26677", 15, 55, 1))
    assert not is_browse_redirect("https://www.ebay.co.uk/sch/9394/i.html?_fsrp=1")
