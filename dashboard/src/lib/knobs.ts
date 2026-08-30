/* The second implementation of the knob arithmetic.
 *
 * The board re-thresholds knobs in the browser so the operator can move a
 * slider and watch the funnel move, which means `fees.max_cost` and the six
 * comparisons in `metrics.check` now exist twice — once in Python, once here.
 * Two implementations of one rule drift, and this drift is SILENT: the page
 * would show a confident PASS on a row the pipeline calls a FAIL, with every
 * test on both sides green.
 *
 * The only thing standing between here and that is
 * `test/knob-golden.test.ts`, which replays Python's own recorded output
 * (`test/fixtures/knob-golden.json`, 608 expectations over 76 cases) through
 * this file and demands equality down to the nulls and the note strings.
 * Change anything here and run it.
 *
 * Ported deliberately literally from phoneloop/fees.py:133-208 and
 * phoneloop/metrics.py:393-532. Where the Python has a comment explaining why
 * a line is the way it is, that reasoning is repeated rather than summarised,
 * because the next person to "simplify" one of these is the failure mode.
 */

export const EVERYTHING_ELSE_BP = 1290;
export const REGULATORY_BP = 35;
export const PER_ORDER_THRESHOLD_PENCE = 1000;
export const PER_ORDER_PENCE_LOW = 30;
export const PER_ORDER_PENCE_HIGH = 40;

/** The window `str_window_bp` claims to measure. */
export const STR_WINDOW_DAYS = 30;
/** Below this fraction of STR_WINDOW_DAYS a window is not comparable at all. */
export const MIN_COMPARABLE_WINDOW_FRACTION = 0.8;

export interface Knobs {
  min_margin_pct_bp: number;
  min_margin_fixed_pence: number;
  str_threshold_bp: number;
  price_from_pence: number;
  price_to_pence: number;
  max_eta_days: number;
  max_competitors: number;
  min_seller_feedback_bp: number;
  min_seller_feedback_n: number;
  vat_on_fees_bp: number;
  ad_rate_bp: number;
  import_vat_term_bp: number;
}

/** The 15 `Metrics` fields `check` and `max_cost` actually read.
 *
 * This is not a convenience subset: it is `CHECK_INPUT_FIELDS` from
 * phoneloop/dashexport/golden.py, and a pytest test proves it is exhaustive
 * by perturbing every other `Metrics` field and asserting no verdict moves.
 * Adding a field here without adding it there re-opens the drift. */
export interface MetricsInput {
  n_listings: number;
  price_median_pence: number | null;
  price_median_denominator: number;
  price_floor_pence: number | null;
  price_floor_denominator: number;
  str_window_bp: number | null;
  str_window_denominator: number;
  str_window_days: number | null;
  str_lifetime: number | null;
  str_lifetime_denominator: number;
  eta_median_days: number | null;
  eta_median_denominator: number;
  feedback_median_bp: number | null;
  feedback_median_n: number | null;
  feedback_median_denominator: number;
}

export type KnobName =
  | "min_margin"
  | "sell_through"
  | "price_band"
  | "delivery"
  | "seller_quality"
  | "saturation";

export interface KnobResult {
  knob: KnobName;
  /** A scalar, a pair (price band, seller quality), or null for unknown. */
  value: number | null | (number | null)[];
  threshold: number | number[];
  /** `null` is "we do not know", and is NOT `false`. Never conflate them. */
  passed: boolean | null;
  denominator: number;
  coverage: number;
  note: string;
}

/* ------------------------------------------------------------------ fees */

/** amount × bp/10000, rounded half up, in integer pence.
 *
 * Python writes this `(amount * bp + 5000) // 10000` and this is a literal
 * transcription of it.
 *
 * Honest note, because the comment here used to claim more than it could:
 * `Math.round(a * b / 10000)` and `((a * b + 5000) / 10000) | 0` were both
 * checked against this expression over every amount from 0 to 200,000 pence
 * against all thirteen rates this codebase uses — 2.6 million pairs — and
 * disagree on NONE of them. So neither is a bug waiting to happen on today's
 * inputs, and an injected regression swapping one in stays green in the
 * golden replay, correctly.
 *
 * The form is kept anyway for two reasons that are real: it is the same
 * expression as the Python, so the two read as one rule rather than two that
 * happen to agree; and it is the only one of the three that still matches
 * Python if a negative amount ever reaches it. Python floors toward negative
 * infinity, `Math.round` rounds half toward positive infinity, and `| 0`
 * truncates toward zero — three different answers below zero. Every caller
 * today passes a price or a fee, never a difference, so that case does not
 * arise; it is one refactor away from arising. */
export function bp(amountPence: number, basisPoints: number): number {
  return Math.floor((amountPence * basisPoints + 5000) / 10000);
}

/** Final value fee for a leaf, via its top-level ancestor.
 *
 * The dashboard does not carry the category tree: `fees.json` ships
 * `fvf_bp_by_leaf`, already resolved by Python for every leaf in the store.
 * Any leaf missing from it falls to the everything-else rate — the same
 * direction Python degrades in, and the safe one, because that rate is the
 * highest. */
export function fvfBp(
  leafCategoryId: string | null,
  fvfByLeaf: Record<string, number>,
): number {
  if (!leafCategoryId) return EVERYTHING_ELSE_BP;
  const rate = fvfByLeaf[leafCategoryId];
  return rate === undefined ? EVERYTHING_ELSE_BP : rate;
}

/** 30p at or below £10, 40p above. Quoted ex-VAT like every other eBay fee. */
export function perOrderPence(pricePence: number): number {
  return pricePence <= PER_ORDER_THRESHOLD_PENCE
    ? PER_ORDER_PENCE_LOW
    : PER_ORDER_PENCE_HIGH;
}

/** Total eBay fees on a sale at this landed price, integer pence.
 *
 * The FVF+regulatory rounding happens BEFORE the VAT step, not combined with
 * it — `bp(bp(price, fvf + reg), vat)`, two roundings. Folding them into one
 * multiplication changes the answer by a penny on a large minority of prices,
 * and a penny is enough to move a max_cost across zero. */
export function fees(
  pricePence: number,
  leafCategoryId: string | null,
  knobs: Knobs,
  fvfByLeaf: Record<string, number>,
): number {
  const fvf = fvfBp(leafCategoryId, fvfByLeaf);
  const variable = bp(bp(pricePence, fvf + REGULATORY_BP), knobs.vat_on_fees_bp);
  const order = bp(perOrderPence(pricePence), knobs.vat_on_fees_bp);
  const ads = bp(pricePence, knobs.ad_rate_bp);
  const importVat = bp(pricePence, knobs.import_vat_term_bp);
  return variable + order + ads + importVat;
}

/** Highest landed supplier cost that still clears the margin knob.
 *
 * Deliberately NOT clamped at zero. A negative result is the honest answer —
 * "no supplier price clears the margin at this sale price" — and clamping it
 * would read as "free stock works", which is the direction that loses money
 * if acted on. Anything downstream that does `Math.max(0, ...)` on this has
 * reintroduced the bug. */
export function maxCost(
  pricePence: number,
  leafCategoryId: string | null,
  knobs: Knobs,
  fvfByLeaf: Record<string, number>,
): number {
  const totalFees = fees(pricePence, leafCategoryId, knobs, fvfByLeaf);
  const target = Math.max(
    bp(pricePence, knobs.min_margin_pct_bp),
    knobs.min_margin_fixed_pence,
  );
  return pricePence - totalFees - target;
}

/* --------------------------------------------------------------- the six */

function coverage(denominator: number, nListings: number): number {
  return nListings ? denominator / nListings : 0.0;
}

/** Python's `f"{days:g}"`.
 *
 * The window length is interpolated into a note string that the golden
 * fixture compares character for character, so "1.18729" has to come out as
 * "1.18729" and "30.0" as "30". `%g` is six significant digits with trailing
 * zeros stripped, falling to exponential outside 10^-4 .. 10^6 — a range no
 * window length reaches, but implemented anyway so this never silently
 * diverges if one ever does. */
export function formatG(x: number | null): string {
  if (x === null) return "?";
  if (!Number.isFinite(x)) return String(x);
  if (x === 0) return "0";
  const exponent = Math.floor(Math.log10(Math.abs(x)));
  if (exponent < -4 || exponent >= 6) {
    const [mantissa, exp] = x.toExponential(5).split("e");
    const trimmed = stripTrailingZeros(mantissa ?? "");
    const sign = exp?.startsWith("-") ? "-" : "+";
    const digits = (exp ?? "").replace(/^[+-]/, "").padStart(2, "0");
    return `${trimmed}e${sign}${digits}`;
  }
  return stripTrailingZeros(x.toPrecision(6));
}

function stripTrailingZeros(s: string): string {
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

export interface CheckArgs {
  competitors?: number | null;
  maxCostAtFloorPence?: number | null;
  maxCostAtMedianPence?: number | null;
}

/** Pass/fail per knob, in knob order.
 *
 * A zero denominator is `null`, never `false` — "we don't know" and "it
 * failed" are different claims and only one of them is worth acting on. */
export function check(
  m: MetricsInput,
  knobs: Knobs,
  {
    competitors = null,
    maxCostAtFloorPence = null,
    maxCostAtMedianPence = null,
  }: CheckArgs = {},
): KnobResult[] {
  const results: KnobResult[] = [];

  // 1 — min net profit margin, inverted into max_cost. There is no
  // per-listing pass/fail for a margin eBay cannot see the supplier cost
  // for; the knob reads "satisfied" when the expected-price basis
  // (price_median) yields a positive max_cost. Falls back to the
  // conservative price_floor basis only when no median price was seen.
  const usingMedianBasis = maxCostAtMedianPence !== null;
  const marginValue = usingMedianBasis ? maxCostAtMedianPence : maxCostAtFloorPence;
  const marginDenominator = usingMedianBasis
    ? m.price_median_denominator
    : m.price_floor_denominator;
  const marginBasisNote = usingMedianBasis
    ? "max_cost at price_median_pence"
    : "max_cost at price_floor_pence (no median price observed)";
  results.push({
    knob: "min_margin",
    value: marginValue,
    threshold: 0,
    passed: marginValue === null ? null : marginValue > 0,
    denominator: marginDenominator,
    coverage: coverage(marginDenominator, m.n_listings),
    note:
      `${marginBasisNote}; positive means a supplier ` +
      "price exists that clears the margin",
  });

  // 2 — sell-through. `str_lifetime` is NEVER compared to the threshold,
  // however good it looks: a lifetime average hides decline. When there is
  // no window the denominator MUST be str_lifetime_denominator — using the
  // window denominator here printed a real figure on a denominator of 0.
  if (m.str_window_bp === null) {
    results.push({
      knob: "sell_through",
      value: m.str_lifetime,
      threshold: knobs.str_threshold_bp,
      passed: null,
      denominator: m.str_lifetime_denominator,
      coverage: coverage(m.str_lifetime_denominator, m.n_listings),
      note:
        "lifetime figure only (no second snapshot yet) - not " +
        "comparable to the window threshold",
    });
  } else {
    // A windowed figure exists — but a window is only comparable to the
    // threshold if it is the window that threshold was set for. A daily
    // schedule produces a ONE-DAY window on its second night, and comparing
    // one night of accumulated sales against a 30-day bar returns a
    // confident FAIL for almost every niche. Withheld, with both window
    // lengths named, rather than answered.
    const minDays = STR_WINDOW_DAYS * MIN_COMPARABLE_WINDOW_FRACTION;
    let windowNote: string;
    if (m.str_window_days === null) {
      windowNote =
        `window length unknown - the ${STR_WINDOW_DAYS}-day ` +
        "threshold is not comparable to a window nobody can " +
        "measure";
    } else if (m.str_window_days < minDays) {
      windowNote =
        `${formatG(m.str_window_days)}-day window against a ` +
        `threshold intended for ${STR_WINDOW_DAYS} days - ` +
        "not comparable";
    } else {
      windowNote = "";
    }
    results.push({
      knob: "sell_through",
      value: m.str_window_bp,
      threshold: knobs.str_threshold_bp,
      passed: windowNote ? null : m.str_window_bp >= knobs.str_threshold_bp,
      denominator: m.str_window_denominator,
      coverage: coverage(m.str_window_denominator, m.n_listings),
      note: windowNote || `${formatG(m.str_window_days)}-day window`,
    });
  }

  // 3 — retail price range, landed. Judged on price_median_pence (the
  // expected figure); the board shows price_floor alongside it separately.
  const inBand =
    m.price_median_pence !== null &&
    knobs.price_from_pence <= m.price_median_pence &&
    m.price_median_pence <= knobs.price_to_pence;
  results.push({
    knob: "price_band",
    value: m.price_median_pence,
    threshold: [knobs.price_from_pence, knobs.price_to_pence],
    passed: m.price_median_pence === null ? null : inBand,
    denominator: m.price_median_denominator,
    coverage: coverage(m.price_median_denominator, m.n_listings),
    note: m.price_median_pence !== null ? "" : "no business landed price observed",
  });

  // 4 — max delivery + handling.
  results.push({
    knob: "delivery",
    value: m.eta_median_days,
    threshold: knobs.max_eta_days,
    passed:
      m.eta_median_days === null ? null : m.eta_median_days <= knobs.max_eta_days,
    denominator: m.eta_median_denominator,
    coverage: coverage(m.eta_median_denominator, m.n_listings),
    note: m.eta_median_days !== null ? "" : "no ETA observed on a business listing",
  });

  // 5 — competitor quality floors. eBay shows rivals, not suppliers, so this
  // describes the competitive field.
  const feedbackKnown =
    m.feedback_median_bp !== null && m.feedback_median_n !== null;
  results.push({
    knob: "seller_quality",
    value: [m.feedback_median_bp, m.feedback_median_n],
    threshold: [knobs.min_seller_feedback_bp, knobs.min_seller_feedback_n],
    passed: !feedbackKnown
      ? null
      : (m.feedback_median_bp as number) >= knobs.min_seller_feedback_bp &&
        (m.feedback_median_n as number) >= knobs.min_seller_feedback_n,
    denominator: m.feedback_median_denominator,
    coverage: coverage(m.feedback_median_denominator, m.n_listings),
    note: feedbackKnown ? "" : "no seller feedback observed",
  });

  // 6 — market saturation cap. There is no row population behind this knob
  // (it is one caller-supplied scalar, not a fraction over rows), so
  // denominator/coverage mean "was the count supplied at all" — NOT the
  // competitor count itself. That keeps a real `competitors = 0` (zero
  // rivals, a known and possibly passing result) from presenting as
  // denominator 0, which this function reserves for "unknown".
  results.push({
    knob: "saturation",
    value: competitors,
    threshold: knobs.max_competitors,
    passed: competitors === null ? null : competitors <= knobs.max_competitors,
    denominator: competitors !== null ? 1 : 0,
    coverage: competitors !== null ? 1.0 : 0.0,
    note: competitors !== null ? "" : "competitor count not supplied",
  });

  return results;
}

/** `score = passed − failed`, exactly as `board.py` ranks.
 *
 * An unknown scores identically to a satisfied knob. That is the pipeline's
 * behaviour and this must reproduce it rather than quietly improve on it —
 * the board's job is to make that visible, which it does by showing the
 * unknown count beside the score, not by changing the arithmetic. */
export function score(results: KnobResult[]): number {
  let passed = 0;
  let failed = 0;
  for (const r of results) {
    if (r.passed === true) passed += 1;
    else if (r.passed === false) failed += 1;
  }
  return passed - failed;
}

export function tally(results: KnobResult[]): {
  passed: number;
  failed: number;
  unknown: number;
} {
  let passed = 0;
  let failed = 0;
  let unknown = 0;
  for (const r of results) {
    if (r.passed === true) passed += 1;
    else if (r.passed === false) failed += 1;
    else unknown += 1;
  }
  return { passed, failed, unknown };
}
