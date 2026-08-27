"""Fee model tests (design spec section 7, contracts section 2.2).

Every expectation here is hand-computed in integer pence, worked out in each
docstring, and checked against the contract's own worked-example table so a
future "fix" that reintroduces float rounding or clamps max_cost to zero has
something concrete to fail against.
"""

from dataclasses import dataclass

from phoneloop import fees


@dataclass(frozen=True)
class LocalKnobs:
    """A small frozen dataclass that satisfies fees.FeeKnobs structurally,
    per the contract: "s1's tests use a small local frozen dataclass that
    satisfies FeeKnobs" -- fees.py must not import phoneloop.knobs, so this
    is deliberately not that class.
    """

    vat_on_fees_bp: int = 12000
    ad_rate_bp: int = 0
    import_vat_term_bp: int = 0
    min_margin_pct_bp: int = 2500
    min_margin_fixed_pence: int = 600


DEFAULT_KNOBS = LocalKnobs()


# --- bp() -------------------------------------------------------------

def test_bp_rounds_half_up():
    """0.5% of 100p is exactly 0.5p -- rounds UP to 1, not down to 0.
    0.49% of 100p is 0.49p -- rounds DOWN to 0. This is the one arithmetic
    fact the whole module rests on."""
    assert fees.bp(100, 50) == 1
    assert fees.bp(100, 49) == 0


def test_bp_never_returns_float():
    result = fees.bp(12345, 990)
    assert isinstance(result, int)
    assert not isinstance(result, bool)  # bool is an int subclass; guard the obvious trap too


def test_bp_matches_the_per_order_identity_from_the_contract():
    """Contract section 2.2's own sanity check: 40p x 1.2 = 48p exactly."""
    assert fees.bp(fees.PER_ORDER_PENCE_HIGH, 12000) == 48


def test_fvf_plus_regulatory_matches_the_spec_quoted_rate():
    """Contract section 2.2's identity, asserted on the RATE, not the pence:
    Mobile Phones 9.9% + 0.35% regulatory = 10.25%; x1.2 VAT-on-fees = 12.3%
    -- exactly the "12.3% + £0.48" the design spec's table quotes. This must
    be asserted on the rate and not on the pence, because sequential
    per-step rounding (what fees() actually does) can differ from this
    single combined rate by a penny at some prices -- see test below and the
    module docstring."""
    mobile_phones_bp = 990
    assert (mobile_phones_bp + fees.REGULATORY_BP) * 12000 // 10000 == 1230  # 12.30%


# --- fvf_bp -------------------------------------------------------------

def test_fvf_bp_unknown_leaf_falls_to_everything_else():
    assert fees.fvf_bp(None) == fees.EVERYTHING_ELSE_BP
    assert fees.fvf_bp("no-such-leaf") == fees.EVERYTHING_ELSE_BP
    assert fees.fvf_bp("no-such-leaf", top_level_of={}) == fees.EVERYTHING_ELSE_BP


def test_fvf_bp_resolves_through_the_full_chain(monkeypatch):
    """Exercises leaf -> top_level_of -> TOP_LEVEL_ID_TO_NAME -> rate table.

    The ids used here ("9355", "139971") are test-local fixture values that
    exist only for the duration of this test (monkeypatch reverts them) --
    they are never present in the shipped TOP_LEVEL_ID_TO_NAME, which ships
    empty because no id in it has been read off a real eBay page (see the
    module docstring and unverified[] in this stream's report). This tests
    the traversal logic, not eBay's real category tree.
    """
    monkeypatch.setitem(fees.TOP_LEVEL_ID_TO_NAME, "9355",
                         "Mobile Phones & Communication")
    top_level_of = {"139971": "9355"}
    assert fees.fvf_bp("139971", top_level_of=top_level_of) == 990


def test_fvf_bp_leaf_missing_from_top_level_of_falls_to_everything_else(monkeypatch):
    monkeypatch.setitem(fees.TOP_LEVEL_ID_TO_NAME, "9355",
                         "Mobile Phones & Communication")
    top_level_of = {"other-leaf": "9355"}
    assert fees.fvf_bp("139971", top_level_of=top_level_of) == fees.EVERYTHING_ELSE_BP


def test_fvf_bp_unknown_category_charges_worst_rate_not_cheapest():
    """An unknown category must not resolve to the cheapest rate in the
    table (Vehicle Parts, 950bp) -- it must resolve to the worst
    (EVERYTHING_ELSE_BP, 1290bp), because overstating eBay's cut is the safe
    direction for max_cost."""
    assert fees.fvf_bp("totally-unrecognised") == 1290
    assert fees.fvf_bp("totally-unrecognised") != 950


# --- per_order_pence ------------------------------------------------------

def test_per_order_pence_boundary():
    assert fees.per_order_pence(1000) == 30    # exactly GBP 10 -> low band
    assert fees.per_order_pence(1001) == 40    # one penny over -> high band
    assert fees.per_order_pence(500) == 30
    assert fees.per_order_pence(5000) == 40


# --- the three worked examples (contract section 2.2) --------------------

def test_worked_example_mobile_phones_30_pounds(monkeypatch):
    """price=3000p, Mobile Phones & Communication (990bp), defaults.

    fvf+reg = 990+35 = 1025bp. bp(3000, 1025) = (3075000+5000)//10000 = 308.
    variable = bp(308, 12000) = (3696000+5000)//10000 = 370.
    order: 3000p > 1000p -> 40p pre-VAT. bp(40, 12000) = 48.
    fees = 370 + 48 = 418.
    target = max(bp(3000, 2500), 600) = max(750, 600) = 750.
    max_cost = 3000 - 418 - 750 = 1832.
    """
    monkeypatch.setitem(fees.TOP_LEVEL_ID_TO_NAME, "9355",
                         "Mobile Phones & Communication")
    top_level_of = {"leaf-phones": "9355"}
    assert fees.fees(3000, "leaf-phones", DEFAULT_KNOBS, top_level_of=top_level_of) == 418
    assert fees.max_cost(3000, "leaf-phones", DEFAULT_KNOBS, top_level_of=top_level_of) == 1832


def test_worked_example_everything_else_15_pounds():
    """price=1500p, no known category -> EVERYTHING_ELSE_BP (1290bp), defaults.

    fvf+reg = 1290+35 = 1325bp. bp(1500, 1325) = (1987500+5000)//10000 = 199.
    variable = bp(199, 12000) = (2388000+5000)//10000 = 239.
    order: 1500p > 1000p -> 40p pre-VAT. bp(40, 12000) = 48.
    fees = 239 + 48 = 287.
    target = max(bp(1500, 2500), 600) = max(375, 600) = 600.
    max_cost = 1500 - 287 - 600 = 613.
    """
    assert fees.fees(1500, None, DEFAULT_KNOBS) == 287
    assert fees.max_cost(1500, None, DEFAULT_KNOBS) == 613


def test_worked_example_home_furniture_diy_10_pounds(monkeypatch):
    """price=1000p, Home, Furniture & DIY (1190bp), defaults.

    fvf+reg = 1190+35 = 1225bp. bp(1000, 1225) = (1225000+5000)//10000 = 123.
    variable = bp(123, 12000) = (1476000+5000)//10000 = 148.
    order: 1000p <= 1000p -> 30p pre-VAT (the boundary lands in the LOW band
    here, unlike the other two worked examples). bp(30, 12000) = 36.
    fees = 148 + 36 = 184.
    target = max(bp(1000, 2500), 600) = max(250, 600) = 600.
    max_cost = 1000 - 184 - 600 = 216.
    """
    monkeypatch.setitem(fees.TOP_LEVEL_ID_TO_NAME, "11450", "Home, Furniture & DIY")
    top_level_of = {"leaf-hfd": "11450"}
    assert fees.fees(1000, "leaf-hfd", DEFAULT_KNOBS, top_level_of=top_level_of) == 184
    assert fees.max_cost(1000, "leaf-hfd", DEFAULT_KNOBS, top_level_of=top_level_of) == 216


def test_sequential_rounding_differs_from_combined_rate_by_a_penny():
    """The module docstring's own claim, pinned: sequential rounding on the
    GBP 30 Mobile Phones example gives 370p for the variable fee, while the
    single combined 12.3% rate would give 369p (3000 * 0.123 = 369.0 exactly,
    no rounding needed there -- the difference comes entirely from rounding
    the FVF+regulatory step to whole pence BEFORE applying VAT, at 307.5 ->
    308). Both come from real published rates; this project picked
    sequential, so 370 is right and a change back to 369 is a regression.
    """
    combined_369 = 3000 * 1230 // 10000  # 369, the single-step comparison figure
    assert combined_369 == 369
    sequential_variable = fees.bp(fees.bp(3000, 990 + fees.REGULATORY_BP), 12000)
    assert sequential_variable == 370
    assert sequential_variable != combined_369


# --- VAT registration -------------------------------------------------

def test_vat_registered_removes_exactly_the_vat_uplift():
    """vat_on_fees_bp=10000 is a x1.0 multiplier. bp(x, 10000) ==
    (x*10000+5000)//10000 == x for every non-negative integer x (the +5000
    never crosses a whole-pence boundary), so at VAT-registered rates
    fees() must equal exactly the pre-VAT FVF+regulatory pence plus the raw
    per-order pence -- nothing else in the arithmetic path may change.
    """
    price = 3000
    registered = LocalKnobs(vat_on_fees_bp=10000)
    pre_vat_variable = fees.bp(price, fees.EVERYTHING_ELSE_BP + fees.REGULATORY_BP)
    expected = pre_vat_variable + fees.per_order_pence(price)
    assert fees.fees(price, None, registered) == expected
    # and the non-registered figure really is higher -- the uplift is real
    assert fees.fees(price, None, DEFAULT_KNOBS) > expected


# --- max_cost is not clamped -------------------------------------------

def test_max_cost_goes_negative_rather_than_clamping():
    """A GBP 5 item can never clear a GBP 6 fixed margin floor once fees are
    subtracted too -- max_cost must say so as a negative number, not as 0.
    A 0 here would read as "free stock works", which is the direction that
    loses money if a supplier is actually sourced at that price."""
    result = fees.max_cost(500, None, DEFAULT_KNOBS)
    assert result < 0
    assert result == -215, (
        "fvf+reg=1325bp; bp(500,1325)=66; variable=bp(66,12000)=79; "
        "order: 500<=1000 -> 30, bp(30,12000)=36; fees=79+36=115; "
        "target=max(bp(500,2500),600)=max(125,600)=600; "
        "max_cost=500-115-600=-215"
    )
