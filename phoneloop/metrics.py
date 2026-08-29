"""Every figure spec section 6 defines, over a niche or a product, each one
carrying the denominator it was computed over (contracts section 2.10).

That is the whole point of this module. A sell-through of 3.2 over 4 known
rows out of 240 is not the same claim as 3.2 over 200 out of 240 -- and
`Metrics` makes both numbers visible side by side rather than letting the
figure travel alone and be read as more certain than it is.

Three rules that make these honest, enforced here rather than asserted in a
docstring elsewhere:

1. **Missing sold is unknown, not zero.** A row with `sold_qty is None` is
   excluded from `sold_known` and from every sum that depends on it.
   `coverage` says how much was excluded. Measured coverage on a full
   240-row page was 13%, not the 32% a shallow top-60 sample would suggest
   (Best Match front-loads winners) -- which is exactly why this denominator
   has to travel with the figure rather than be assumed.
2. **Lifetime is not a window.** `str_lifetime` and `velocity_day1` average
   over a listing's whole life and hide decline; only `str_window_bp` may be
   compared against `knobs.str_threshold_bp`. `check()` never falls back to
   comparing `str_lifetime` when `str_window_bp` is None -- it reports the
   sell-through knob as unknown (`passed=None`) instead.
3. **The result-count header is never a denominator.** Every denominator
   below is a count over the rows actually passed in; nothing in this
   module takes a header count as an argument.

Two formulas in spec section 6 are written as a bare sum with no explicit
divisor (`velocity_day1 = Sigma (sold_qty / age_days)`, and the analogous
`velocity_win`). This module takes that literally: both are the SUM of each
listing's own rate, not an average -- a niche-level "units moving per day"
figure that scales with how many listings are actually selling, which is
what a saturation/demand read needs. `str_lifetime` and `str_window_bp`, by
contrast, are explicitly written as an average over a denominator in the
spec text and are computed as one. This interpretation is recorded in
`unverified[]` in the build report, per contracts section "what unverified
means".

`metrics.py` imports `phoneloop.knobs` (same stream) and nothing else from
`phoneloop` -- it must not import `fees`, `market_store` or `identity`.
`board.py` is the module that composes all three.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from phoneloop.knobs import Knobs

# The window str_window_bp claims to measure (spec section 6: "Sigma
# Delta-sold(30d)"), and the window `knobs.str_threshold_bp` was set for.
#
# The window boundary itself is enforced by which two snapshot dates produced
# `deltas` -- a decision `phoneloop.snapshots.difference` makes, not this
# module. But `check()` DOES read this now, to refuse a comparison between a
# figure measured over one night and a threshold meant for thirty days. It
# used to be documentation only, and the comment here admitted as much; that
# was the bug (see MIN_COMPARABLE_WINDOW_FRACTION).
STR_WINDOW_DAYS = 30

# How much of STR_WINDOW_DAYS a measured window must cover before
# `str_window_bp` may be judged against `knobs.str_threshold_bp` at all.
#
# A judgement call, stated here rather than buried at the call site. The
# direction of the error is what settles it: a short window under-reads
# accumulated sales by construction, so the wrong answer it produces is a
# confident FAIL -- which a reader takes as "this niche has no demand"
# rather than "we measured it for one night". 0.8 leaves room for a couple
# of missed nights inside a nominal 30-day window (the machine sleeps; see
# the scheduling notes) without turning a 24- or 25-day window into an
# unknown, while still refusing the 1-day window that the daily schedule
# actually produces on its second night.
MIN_COMPARABLE_WINDOW_FRACTION = 0.8

_NEW_LISTING_DAYS = 7
_NEW_ACCOUNT_DAYS = 365
_PRICE_FLOOR_SAMPLE = 5


def _round_half_up(x: float) -> int:
    """Round-to-nearest, ties away from zero. `x` is a ratio, never money --
    money is rounded exactly once, in fees.bp(), never here.
    """
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _median_int(values: Sequence[int]) -> int | None:
    """Median of an even-sized set of integers rounds half up:
    (a + b + 1) // 2. An odd-sized set returns its exact middle value, no
    rounding involved.
    """
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid] + 1) // 2


def _median_float(values: Sequence[float]) -> float | None:
    """Median of a float sample, no rounding. Separate from `_median_int`
    because that one rounds half up to stay an integer, which is right for
    pence and wrong for a span of days."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _coverage(denominator: int, n_listings: int) -> float:
    return denominator / n_listings if n_listings else 0.0


def _fmt_days(days: float | None) -> str:
    """A window length as it should read on a board row: "1", "1.5", "30"."""
    if days is None:
        return "?"
    return f"{days:g}"


@dataclass(frozen=True)
class Metrics:
    """Spec section 6 over one niche or one product. Every value sits beside
    its denominator; see the module docstring for the three rules that keep
    them honest.
    """

    # population
    n_listings: int
    n_sellers: int
    n_business: int
    n_private: int

    # the denominator that governs every demand figure
    sold_known: int                       # rows where sold_qty IS NOT NULL
    coverage: float                       # sold_known / n_listings; 0.0 when n_listings == 0

    # demand, lifetime -- averages over each listing's whole life, they hide decline
    str_lifetime: float | None
    str_lifetime_denominator: int         # == sold_known
    velocity_day1: float | None           # sold per day, from listed_at
    velocity_day1_denominator: int        # rows with sold_qty AND listed_at AND age_days > 0

    # demand, windowed -- the only figure knob 2 may be compared against
    str_window_bp: int | None             # basis points; None when no second snapshot
    str_window_denominator: int           # listings with a known delta on both days
    velocity_win: float | None
    velocity_win_denominator: int

    # distribution of demand
    success_rate: float | None            # sold_qty > 0 over sold_known
    success_rate_denominator: int
    dispersion: float | None              # sellers with sales / sellers with a known sold count
    dispersion_denominator: int

    # saturation
    new_7d: int                           # listings with listed_at in the last 7 days
    new_7d_denominator: int               # rows with listed_at at all
    promoted_pct: float | None
    promoted_denominator: int             # rows where promoted IS NOT NULL

    # price, BUSINESS sellers only -- private prices include the buyer-protection fee
    price_median_pence: int | None
    price_median_denominator: int
    price_floor_pence: int | None         # min landed of the five cheapest business listings
    price_floor_denominator: int          # how many of the five actually existed

    # delivery
    eta_median_days: int | None
    eta_median_denominator: int

    # competitor profile (knob 5, reframed)
    feedback_median_bp: int | None
    feedback_median_denominator: int
    feedback_median_n: int | None
    new_account_share: float | None       # accounts under a year old
    new_account_denominator: int

    # How long a window `str_window_bp` actually spans, in days -- the median
    # of the per-listing elapsed times that fed it. Appended at the END with
    # a default because no other field carries one and `compute()` builds
    # Metrics by keyword, so adding it here breaks no existing caller.
    #
    # Load-bearing, not decorative: `check()` compares it against
    # STR_WINDOW_DAYS and withholds the sell-through verdict when the window
    # is materially shorter. `None` means "no windowed figure" or "the window
    # length is unknown" -- both of which are also grounds for withholding.
    str_window_days: float | None = None


def compute(rows: Sequence[Mapping], *,
            now: datetime | None = None,
            deltas: Mapping[str, tuple[int, int]] | None = None) -> Metrics:
    """Spec section 6 over ROW_FIELDS rows. `deltas` maps source_id ->
    (delta_sold, delta_days) for listings with a known delta between two
    snapshot dates -- see `phoneloop.snapshots.difference`. Rows whose
    `source_id` is absent from `deltas` have no known delta this pass, which
    is why `deltas=None` (the default, before a second snapshot exists)
    leaves `str_window_bp` and `velocity_win` at None rather than 0.
    """
    now = now or datetime.now(UTC)
    n_listings = len(rows)

    seller_ids = [r.get("seller_id") for r in rows if r.get("seller_id")]
    n_sellers = len(set(seller_ids))
    n_business = sum(1 for r in rows if r.get("seller_type") == "business")
    n_private = sum(1 for r in rows if r.get("seller_type") == "private")

    # --- demand, lifetime ---------------------------------------------------
    known_sold = [(r, r["sold_qty"]) for r in rows if r.get("sold_qty") is not None]
    sold_known = len(known_sold)
    coverage = _coverage(sold_known, n_listings)

    str_lifetime = (sum(q for _, q in known_sold) / sold_known) if sold_known else None

    velocity_terms = []
    for r, q in known_sold:
        listed_at = r.get("listed_at")
        if listed_at is None:
            continue
        age_days = (now - listed_at).total_seconds() / 86400.0
        if age_days <= 0:
            # Listed today (or a clock skew): no elapsed time to rate against.
            continue
        velocity_terms.append(q / age_days)
    velocity_day1 = sum(velocity_terms) if velocity_terms else None
    velocity_day1_denominator = len(velocity_terms)

    # --- demand, windowed -----------------------------------------------------
    window_deltas: list[int] = []
    window_days_seen: list[float] = []
    velocity_win_terms: list[float] = []
    if deltas:
        for r in rows:
            sid = r.get("source_id")
            if sid is None or sid not in deltas:
                continue
            delta_sold, delta_days = deltas[sid]
            if delta_sold is None:
                continue
            window_deltas.append(delta_sold)
            if delta_days is not None:
                # The MEDIAN of these, not the max: a single listing whose
                # two observations happened to straddle an extra sweep would
                # otherwise stretch the reported window for the whole group.
                window_days_seen.append(float(delta_days))
            if delta_days and delta_days > 0:
                velocity_win_terms.append(delta_sold / delta_days)

    str_window_days = _median_float(window_days_seen)
    str_window_denominator = len(window_deltas)
    if window_deltas:
        # Sigma delta-sold / denominator, as a fraction, x100 for a percent
        # (spec section 6), x100 again to store as basis points.
        str_window_bp = _round_half_up(
            (sum(window_deltas) / str_window_denominator) * 10000)
    else:
        str_window_bp = None

    velocity_win_denominator = len(velocity_win_terms)
    velocity_win = sum(velocity_win_terms) if velocity_win_terms else None

    # --- distribution of demand ----------------------------------------------
    success_rate_denominator = sold_known
    success_rate = (
        sum(1 for _, q in known_sold if q > 0) / sold_known
    ) if sold_known else None

    sellers_known = {r.get("seller_id") for r, q in known_sold if r.get("seller_id")}
    sellers_with_sales = {r.get("seller_id") for r, q in known_sold
                          if q > 0 and r.get("seller_id")}
    dispersion_denominator = len(sellers_known)
    dispersion = (len(sellers_with_sales) / dispersion_denominator
                 if dispersion_denominator else None)

    # --- saturation ------------------------------------------------------------
    listed_rows = [r for r in rows if r.get("listed_at") is not None]
    new_7d_denominator = len(listed_rows)
    new_7d = sum(
        1 for r in listed_rows
        if 0 <= (now - r["listed_at"]).total_seconds() <= _NEW_LISTING_DAYS * 86400
    )

    promoted_rows = [r for r in rows if r.get("promoted") is not None]
    promoted_denominator = len(promoted_rows)
    promoted_pct = (
        sum(1 for r in promoted_rows if r["promoted"]) / promoted_denominator
    ) if promoted_denominator else None

    # --- price, business sellers only ------------------------------------------
    # Rows with seller_type unset are NOT assumed business -- an unlabelled
    # row is excluded from the price aggregates exactly like a private one,
    # and the denominator says how many survived either way.
    business_landed = sorted(
        r["total_pence"] for r in rows
        if r.get("seller_type") == "business" and r.get("total_pence") is not None
    )
    price_median_denominator = len(business_landed)
    price_median_pence = _median_int(business_landed)

    price_floor_denominator = min(_PRICE_FLOOR_SAMPLE, len(business_landed))
    # The min of the five cheapest is, by construction, the single cheapest
    # value -- the "five" sets how many comparable listings the floor
    # required to exist, which price_floor_denominator reports, not a
    # different arithmetic result.
    price_floor_pence = business_landed[0] if business_landed else None

    business_etas = sorted(
        r["eta_days"] for r in rows
        if r.get("seller_type") == "business" and r.get("eta_days") is not None
    )
    eta_median_denominator = len(business_etas)
    eta_median_days = _median_int(business_etas)

    # --- competitor profile (knob 5) --------------------------------------------
    feedback_rows = [
        r for r in rows
        if r.get("seller_feedback_bp") is not None and r.get("seller_feedback_n") is not None
    ]
    feedback_median_denominator = len(feedback_rows)
    feedback_median_bp = _median_int([r["seller_feedback_bp"] for r in feedback_rows])
    feedback_median_n = _median_int([r["seller_feedback_n"] for r in feedback_rows])

    joined_rows = [r for r in rows if r.get("store_joined") is not None]
    new_account_denominator = len(joined_rows)
    if new_account_denominator:
        today = now.date()
        new_accounts = sum(
            1 for r in joined_rows
            if (today - r["store_joined"]).days < _NEW_ACCOUNT_DAYS
        )
        new_account_share = new_accounts / new_account_denominator
    else:
        new_account_share = None

    return Metrics(
        n_listings=n_listings,
        n_sellers=n_sellers,
        n_business=n_business,
        n_private=n_private,
        sold_known=sold_known,
        coverage=coverage,
        str_lifetime=str_lifetime,
        str_lifetime_denominator=sold_known,
        velocity_day1=velocity_day1,
        velocity_day1_denominator=velocity_day1_denominator,
        str_window_bp=str_window_bp,
        str_window_denominator=str_window_denominator,
        velocity_win=velocity_win,
        velocity_win_denominator=velocity_win_denominator,
        success_rate=success_rate,
        success_rate_denominator=success_rate_denominator,
        dispersion=dispersion,
        dispersion_denominator=dispersion_denominator,
        new_7d=new_7d,
        new_7d_denominator=new_7d_denominator,
        promoted_pct=promoted_pct,
        promoted_denominator=promoted_denominator,
        price_median_pence=price_median_pence,
        price_median_denominator=price_median_denominator,
        price_floor_pence=price_floor_pence,
        price_floor_denominator=price_floor_denominator,
        eta_median_days=eta_median_days,
        eta_median_denominator=eta_median_denominator,
        feedback_median_bp=feedback_median_bp,
        feedback_median_denominator=feedback_median_denominator,
        feedback_median_n=feedback_median_n,
        new_account_share=new_account_share,
        new_account_denominator=new_account_denominator,
        str_window_days=str_window_days,
    )


@dataclass(frozen=True)
class KnobResult:
    knob: str            # "min_margin" | "sell_through" | "price_band" | "delivery" | "seller_quality" | "saturation"
    value: object
    threshold: object
    passed: bool | None  # None = unknown, because the denominator was zero
    denominator: int
    coverage: float
    note: str            # e.g. "lifetime, not window - not comparable to the threshold"


def check(m: Metrics, knobs: Knobs, *,
          competitors: int | None = None,
          max_cost_at_floor_pence: int | None = None,
          max_cost_at_median_pence: int | None = None) -> list[KnobResult]:
    """Pass/fail per knob, in knob order. A zero denominator is `None`, never
    False -- "we don't know" and "it failed" are different claims, and only
    one of them is the input the board should act on.

    `max_cost_at_floor_pence` / `max_cost_at_median_pence` come from
    `fees.max_cost` (this module never computes fees itself) and
    `competitors` comes from an identity-tier competitor count (this module
    never computes identity itself) -- both are the caller's job, per the
    module docstring's import restriction.
    """
    results: list[KnobResult] = []

    # 1 -- min net profit margin, inverted into max_cost (spec section 7 /
    # knob 1). There is no per-listing pass/fail for a margin eBay cannot
    # see the supplier cost for; the knob reads "satisfied" when the
    # expected-price basis (price_median) yields a positive max_cost, i.e. a
    # supplier price exists that clears the margin floor. Falls back to the
    # conservative (price_floor) basis only when no median price was seen.
    using_median_basis = max_cost_at_median_pence is not None
    margin_value = (max_cost_at_median_pence if using_median_basis
                    else max_cost_at_floor_pence)
    margin_denominator = (m.price_median_denominator if using_median_basis
                           else m.price_floor_denominator)
    margin_basis_note = ("max_cost at price_median_pence" if using_median_basis
                          else "max_cost at price_floor_pence (no median price observed)")
    results.append(KnobResult(
        knob="min_margin", value=margin_value, threshold=0,
        passed=None if margin_value is None else margin_value > 0,
        denominator=margin_denominator,
        coverage=_coverage(margin_denominator, m.n_listings),
        note=f"{margin_basis_note}; positive means a supplier "
             "price exists that clears the margin",
    ))

    # 2 -- sell-through. str_lifetime is NEVER compared to the threshold,
    # however good it looks -- see the module docstring, rule 2. The
    # denominator/coverage here MUST be str_lifetime_denominator (== the
    # sold_known count str_lifetime was actually averaged over), not
    # str_window_denominator -- that field is 0 whenever there is no second
    # snapshot (the exact branch this `if` is on), which printed a real
    # figure sitting on a reported denominator of 0 / 0% coverage
    # (integration bug, fixed here: found while wiring the board's offline
    # end-to-end run).
    if m.str_window_bp is None:
        results.append(KnobResult(
            knob="sell_through", value=m.str_lifetime, threshold=knobs.str_threshold_bp,
            passed=None,
            denominator=m.str_lifetime_denominator,
            coverage=_coverage(m.str_lifetime_denominator, m.n_listings),
            note="lifetime figure only (no second snapshot yet) - not "
                 "comparable to the window threshold",
        ))
    else:
        # A windowed figure exists -- but a window is only comparable to
        # knobs.str_threshold_bp if it is the window that threshold was set
        # for. A daily schedule produces a ONE-DAY window on its second
        # night, and comparing one night of accumulated sales against a
        # 30-day bar returns a confident FAIL for almost every niche. That is
        # the same error as comparing str_lifetime to the threshold, and the
        # same error as calling a zero denominator False: an unknown reported
        # as a number. Withheld here, with both window lengths named, rather
        # than answered.
        min_days = STR_WINDOW_DAYS * MIN_COMPARABLE_WINDOW_FRACTION
        if m.str_window_days is None:
            window_note = (f"window length unknown - the {STR_WINDOW_DAYS}-day "
                          "threshold is not comparable to a window nobody can "
                          "measure")
        elif m.str_window_days < min_days:
            window_note = (f"{_fmt_days(m.str_window_days)}-day window against a "
                          f"threshold intended for {STR_WINDOW_DAYS} days - "
                          "not comparable")
        else:
            window_note = ""
        results.append(KnobResult(
            knob="sell_through", value=m.str_window_bp, threshold=knobs.str_threshold_bp,
            passed=(None if window_note
                    else m.str_window_bp >= knobs.str_threshold_bp),
            denominator=m.str_window_denominator,
            coverage=_coverage(m.str_window_denominator, m.n_listings),
            note=window_note or f"{_fmt_days(m.str_window_days)}-day window",
        ))

    # 3 -- retail price range, landed. Judged on price_median_pence (the
    # expected figure); the board shows price_floor alongside it separately.
    in_band = (m.price_median_pence is not None
              and knobs.price_from_pence <= m.price_median_pence <= knobs.price_to_pence)
    results.append(KnobResult(
        knob="price_band", value=m.price_median_pence,
        threshold=(knobs.price_from_pence, knobs.price_to_pence),
        passed=None if m.price_median_pence is None else in_band,
        denominator=m.price_median_denominator,
        coverage=_coverage(m.price_median_denominator, m.n_listings),
        note="" if m.price_median_pence is not None else "no business landed price observed",
    ))

    # 4 -- max delivery + handling.
    results.append(KnobResult(
        knob="delivery", value=m.eta_median_days, threshold=knobs.max_eta_days,
        passed=None if m.eta_median_days is None else m.eta_median_days <= knobs.max_eta_days,
        denominator=m.eta_median_denominator,
        coverage=_coverage(m.eta_median_denominator, m.n_listings),
        note="" if m.eta_median_days is not None else "no ETA observed on a business listing",
    ))

    # 5 -- competitor quality floors (reframed from "min supplier quality").
    feedback_known = m.feedback_median_bp is not None and m.feedback_median_n is not None
    results.append(KnobResult(
        knob="seller_quality", value=(m.feedback_median_bp, m.feedback_median_n),
        threshold=(knobs.min_seller_feedback_bp, knobs.min_seller_feedback_n),
        passed=(None if not feedback_known else
                m.feedback_median_bp >= knobs.min_seller_feedback_bp
                and m.feedback_median_n >= knobs.min_seller_feedback_n),
        denominator=m.feedback_median_denominator,
        coverage=_coverage(m.feedback_median_denominator, m.n_listings),
        note="" if feedback_known else "no seller feedback observed",
    ))

    # 6 -- market saturation cap. Distinct sellers at the best identity
    # tier -- this module has no identity knowledge, so the caller supplies
    # the count (phoneloop.identity + board.py compute it). There is no row
    # population behind this knob (it is one caller-supplied scalar, not a
    # fraction over `rows`), so `denominator`/`coverage` here mean "was the
    # count supplied at all" (1/1.0) rather than "how many of n_listings are
    # known" -- NOT the competitor count itself. That keeps a real
    # `competitors=0` (zero rivals found, a known and possibly passing
    # result) from presenting as denominator=0, which `check()`'s own rule
    # reserves for "unknown".
    results.append(KnobResult(
        knob="saturation", value=competitors, threshold=knobs.max_competitors,
        passed=None if competitors is None else competitors <= knobs.max_competitors,
        denominator=1 if competitors is not None else 0,
        coverage=1.0 if competitors is not None else 0.0,
        note="" if competitors is not None else "competitor count not supplied",
    ))

    return results
