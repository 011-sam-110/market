/* Turning a knob without re-measuring anything.
 *
 * The exporter ships INPUTS, not verdicts — a knob's `value` is the measured
 * figure and `threshold` is what it happened to be compared against. So
 * moving a slider re-runs the COMPARISON, never the measurement, and this
 * file is that comparison.
 *
 * Three rules, and getting any of them wrong puts a confident answer on
 * screen that the pipeline would refuse to make:
 *
 * 1. `denominator === 0` is unknown and STAYS unknown at every threshold.
 *    There is no slider position that turns "nobody measured this" into a
 *    verdict.
 *
 * 2. A knob the exporter withheld while HOLDING a figure — denominator > 0
 *    but `passed === null` — was withheld structurally, not by threshold.
 *    The only case in this store is sell-through: a 1.19-day window against
 *    a bar set for 30 days. Lowering the bar does not make a one-day window
 *    comparable, so this stays withheld too. Re-comparing it here is exactly
 *    the bug `metrics.check` exists to prevent.
 *
 * 3. The margin knob is not a comparison at all. Its value IS a function of
 *    the fee and margin knobs (`fees.max_cost`), so moving any of those five
 *    fields changes the figure, not just the bar — and recomputing it needs
 *    the product's price, which lives in the lazily-loaded metrics payload.
 *    Until that arrives the knob reports that it is stale rather than
 *    showing a figure computed under a setting nobody is looking at.
 */

import { check, maxCost, type Knobs, type MetricsInput } from "./knobs";
import type { ExportedKnob, KnobName, ProductRow } from "./types";

/** The knob fields that change what `max_cost` IS, rather than what it is
 * compared against. */
export const MARGIN_INPUT_FIELDS: (keyof Knobs)[] = [
  "min_margin_pct_bp",
  "min_margin_fixed_pence",
  "vat_on_fees_bp",
  "ad_rate_bp",
  "import_vat_term_bp",
];

export function marginInputsMoved(knobs: Knobs, defaults: Knobs): boolean {
  return MARGIN_INPUT_FIELDS.some((f) => knobs[f] !== defaults[f]);
}

export type Recomputed = ExportedKnob & {
  /** True when the figure itself is out of date, not just the comparison. */
  stale?: boolean;
};

function compare(knob: KnobName, value: unknown, knobs: Knobs): boolean | null {
  switch (knob) {
    case "sell_through":
      return (value as number) >= knobs.str_threshold_bp;
    case "price_band": {
      const v = value as number;
      return knobs.price_from_pence <= v && v <= knobs.price_to_pence;
    }
    case "delivery":
      return (value as number) <= knobs.max_eta_days;
    case "seller_quality": {
      const [fb, n] = value as (number | null)[];
      if (fb === null || fb === undefined || n === null || n === undefined) {
        return null;
      }
      return fb >= knobs.min_seller_feedback_bp && n >= knobs.min_seller_feedback_n;
    }
    case "saturation":
      return (value as number) <= knobs.max_competitors;
    case "min_margin":
      return (value as number) > 0;
    default:
      return null;
  }
}

function thresholdFor(knob: KnobName, knobs: Knobs): number | number[] {
  switch (knob) {
    case "min_margin":
      return 0;
    case "sell_through":
      return knobs.str_threshold_bp;
    case "price_band":
      return [knobs.price_from_pence, knobs.price_to_pence];
    case "delivery":
      return knobs.max_eta_days;
    case "seller_quality":
      return [knobs.min_seller_feedback_bp, knobs.min_seller_feedback_n];
    case "saturation":
      return knobs.max_competitors;
    default:
      return 0;
  }
}

/** Re-run one knob's comparison at the current setting. */
export function rethresholdKnob(
  k: ExportedKnob,
  knobs: Knobs,
  { marginStale }: { marginStale: boolean },
): Recomputed {
  const threshold = thresholdFor(k.knob, knobs);

  // Rule 1 — nothing measured, nothing to compare.
  if (k.denominator === 0 || k.value === null) {
    return { ...k, threshold, passed: null, unknown: true };
  }

  // Rule 2 — a structural withhold survives any threshold.
  if (k.passed === null) {
    return { ...k, threshold, passed: null, unknown: true };
  }

  // Rule 3 — the margin knob's figure, not its bar, moved.
  if (k.knob === "min_margin" && marginStale) {
    return { ...k, threshold, passed: null, unknown: true, stale: true };
  }

  const passed = compare(k.knob, k.value, knobs);
  return { ...k, threshold, passed, unknown: passed === null };
}

export interface Rethresholded {
  knobs: Recomputed[];
  score: number;
  passed: number;
  failed: number;
  unknown: number;
  /** The margin figure could not be recomputed under this setting. */
  marginStale: boolean;
}

export function rethresholdRow(
  row: Pick<ProductRow, "knobs">,
  knobs: Knobs,
  defaults: Knobs,
): Rethresholded {
  const marginStale = marginInputsMoved(knobs, defaults);
  const out = row.knobs.map((k) => rethresholdKnob(k, knobs, { marginStale }));
  let passed = 0;
  let failed = 0;
  let unknown = 0;
  for (const k of out) {
    if (k.passed === true) passed += 1;
    else if (k.passed === false) failed += 1;
    else unknown += 1;
  }
  return { knobs: out, score: passed - failed, passed, failed, unknown, marginStale };
}

/** The exact path: recompute a row from its full metrics, the same way
 * Python does. Used once the metrics payload has loaded, and pinned to
 * Python by `test/knob-golden.test.ts`. */
export function rethresholdExact(
  metrics: MetricsInput,
  leafCategoryId: string | null,
  competitors: number | null,
  knobs: Knobs,
  fvfByLeaf: Record<string, number>,
): ExportedKnob[] {
  const floor =
    metrics.price_floor_pence !== null
      ? maxCost(metrics.price_floor_pence, leafCategoryId, knobs, fvfByLeaf)
      : null;
  const median =
    metrics.price_median_pence !== null
      ? maxCost(metrics.price_median_pence, leafCategoryId, knobs, fvfByLeaf)
      : null;

  return check(metrics, knobs, {
    competitors,
    maxCostAtFloorPence: floor,
    maxCostAtMedianPence: median,
  }).map((r) => ({ ...r, unknown: r.passed === null }));
}

/* ------------------------------------------------------- binding knob */

export interface BindingReport {
  /** The knob eliminating the most rows, or null when nothing fails. */
  knob: KnobName | null;
  /** Rows each knob fails, and rows each knob cannot judge. */
  failed: Record<KnobName, number>;
  withheld: Record<KnobName, number>;
  rows: number;
}

/** Which knob is actually carrying the load.
 *
 * Failures and withholds are counted SEPARATELY and never summed. A knob
 * that withholds on 27,000 rows is not "strict" — it is unmeasured, and
 * loosening it would change nothing at all. Merging the two counts is how a
 * board tells you to turn the wrong dial. */
export function bindingConstraint(
  rows: Rethresholded[],
  knobNames: KnobName[],
): BindingReport {
  const failed = Object.fromEntries(knobNames.map((k) => [k, 0])) as Record<
    KnobName,
    number
  >;
  const withheld = Object.fromEntries(knobNames.map((k) => [k, 0])) as Record<
    KnobName,
    number
  >;

  for (const row of rows) {
    for (const k of row.knobs) {
      if (k.passed === false) failed[k.knob] += 1;
      else if (k.passed === null) withheld[k.knob] += 1;
    }
  }

  let binding: KnobName | null = null;
  let most = 0;
  for (const name of knobNames) {
    if (failed[name] > most) {
      most = failed[name];
      binding = name;
    }
  }

  return { knob: binding, failed, withheld, rows: rows.length };
}
