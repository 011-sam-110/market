/* How a figure is allowed to appear on this board.
 *
 * There is one rule and it is load-bearing: a value whose denominator is 0
 * is UNKNOWN, and unknown never renders as a number. Not as 0, not as 0%,
 * not as an em-dash tucked into a corner. The components render it as a
 * stamp. These helpers return `null` for it so a caller that forgets has a
 * type error rather than a plausible-looking zero.
 */

const GBP = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const GBP_WHOLE = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const INT = new Intl.NumberFormat("en-GB");

/** Integer pence as money. Negative is kept and shown, never clamped — a
 * negative max_cost is the honest "no supplier price clears the margin". */
export function money(pence: number | null | undefined): string | null {
  if (pence === null || pence === undefined) return null;
  return GBP.format(pence / 100);
}

export function moneyWhole(pence: number | null | undefined): string | null {
  if (pence === null || pence === undefined) return null;
  return GBP_WHOLE.format(pence / 100);
}

export function count(n: number | null | undefined): string | null {
  if (n === null || n === undefined) return null;
  return INT.format(n);
}

/** Basis points as a percentage. 9500 -> "95%", 10309 -> "103.1%". */
export function bpPercent(
  value: number | null | undefined,
  digits = 1,
): string | null {
  if (value === null || value === undefined) return null;
  const pct = value / 100;
  const rounded = Number.isInteger(pct) ? pct.toFixed(0) : pct.toFixed(digits);
  return `${trimZeros(rounded)}%`;
}

/** A coverage fraction (0..1) as a percentage. */
export function coveragePercent(
  fraction: number | null | undefined,
  digits = 1,
): string | null {
  if (fraction === null || fraction === undefined) return null;
  const pct = fraction * 100;
  if (pct > 0 && pct < 0.1) return "<0.1%";
  return `${trimZeros(pct.toFixed(digits))}%`;
}

function trimZeros(s: string): string {
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

/** The denominator line that sits under every figure.
 *
 * `n=218 of 29,863 · 0.7%` — the count the figure was measured over, the
 * population it was measured within, and the fraction. When the denominator
 * is 0 this returns the words, not a number: "no observations". */
export function denominatorLine(
  denominator: number,
  population: number,
  coverage?: number,
): string {
  if (!denominator) return "no observations";
  const frac = coverage ?? (population ? denominator / population : 0);
  const pct = coveragePercent(frac, frac < 0.1 ? 2 : 1);
  return population
    ? `n=${INT.format(denominator)} of ${INT.format(population)} · ${pct}`
    : `n=${INT.format(denominator)}`;
}

/** "2026-08-29" -> "29 Aug 2026". Dates in this bundle are plain ISO days
 * with no timezone; parsing them with `new Date(s)` and formatting locally
 * shifts them a day west of UTC, so they are split rather than parsed. */
export function isoDay(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

/** A timestamp as "29 Aug 2026, 20:43 UTC". Deliberately UTC: the run
 * happens overnight and a local rendering would put a 00:40 run on the
 * previous day for half the year. */
export function stamp(iso: string): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  const day = isoDay(t.toISOString().slice(0, 10));
  const hh = String(t.getUTCHours()).padStart(2, "0");
  const mm = String(t.getUTCMinutes()).padStart(2, "0");
  return `${day}, ${hh}:${mm} UTC`;
}

/** A leaf id turned into something a second reader can hold onto.
 * The store's leaves are mostly coarse swept hubs with no name attached, so
 * this is honest about being an id rather than inventing a label. */
export function leafLabel(leaf: string | null): string {
  return leaf ? `leaf ${leaf}` : "leaf unknown";
}

/** "9355@15-55" -> "£15–55". The band lives in the niche key AFTER the @;
 * the leaf lives in its own field and is never parsed out of this string. */
export function bandLabel(band: string | null): string {
  if (!band) return "any price";
  const [from, to] = band.split("-");
  return from && to ? `£${from}–${to}` : `£${band}`;
}

const KNOB_TITLES: Record<string, string> = {
  min_margin: "Margin",
  sell_through: "Sell-through",
  price_band: "Price",
  delivery: "Delivery",
  seller_quality: "Rivals",
  saturation: "Crowding",
};

export function knobTitle(knob: string): string {
  return KNOB_TITLES[knob] ?? knob;
}

/** What each knob is actually asking, in the operator's own words rather
 * than the field name. A second reader has not read the spec. */
const KNOB_QUESTIONS: Record<string, string> = {
  min_margin:
    "The highest price you can pay a supplier and still clear the margin floor after eBay fees. A negative figure means no supplier price works at this sale price.",
  sell_through:
    "Units sold for each listing in the measured window. The figure compares to the threshold only when the window is near 30 days.",
  price_band:
    "The median landed price against the band you set. Landed price is item plus postage. The pipeline counts business sellers only.",
  delivery:
    "The median delivery estimate. The pipeline reads it from the item page and takes the late end of the range. The estimate is for this machine’s own postcode.",
  seller_quality:
    "The median feedback score of rival sellers, and the number of ratings behind it. eBay shows rivals, not suppliers.",
  saturation:
    "The number of separate sellers on this product, at the best identity tier available.",
};

export function knobQuestion(knob: string): string {
  return KNOB_QUESTIONS[knob] ?? "";
}

/** Render one knob's value in its own units. Returns null for unknown. */
export function knobValue(
  knob: string,
  value: number | null | (number | null)[],
): string | null {
  if (value === null || value === undefined) return null;
  switch (knob) {
    case "min_margin":
      return money(value as number);
    case "sell_through":
      return bpPercent(value as number, 0);
    case "price_band":
      return money(value as number);
    case "delivery":
      return `${value as number} ${value === 1 ? "day" : "days"}`;
    case "seller_quality": {
      // The headline is the feedback percentage; the rating count it rests
      // on is the secondary reading, rendered by `knobSecondary` so it can
      // sit in its own line rather than being crushed into one figure.
      const [fb] = value as (number | null)[];
      if (fb === null || fb === undefined) return null;
      return bpPercent(fb, 1);
    }
    case "saturation":
      return `${value as number}`;
    default:
      return String(value);
  }
}

/** The threshold, rendered in the same units as the value beside it. */
export function knobThreshold(
  knob: string,
  threshold: number | number[],
): string {
  switch (knob) {
    case "min_margin":
      return "above £0";
    case "sell_through":
      return `≥ ${bpPercent(threshold as number, 0)}`;
    case "price_band": {
      const [from, to] = threshold as number[];
      return `${moneyWhole(from ?? 0)}–${moneyWhole(to ?? 0)}`;
    }
    case "delivery":
      return `≤ ${threshold} days`;
    case "seller_quality": {
      // The rating count carries its unit, and the comparison holds together
      // on one line. Without either, a narrow column printed "≥ 95% on ≥"
      // and left a bare "50" on the line below, which names nothing.
      const [fb, n] = threshold as number[];
      return `≥ ${bpPercent(fb ?? 0, 1)} on ≥ ${count(n ?? 0)} ratings`;
    }
    case "saturation":
      return `≤ ${threshold} sellers`;
    default:
      return String(threshold);
  }
}

/** The secondary reading a knob carries beside its headline figure, where
 * one exists. Seller quality is two numbers and the second is the one that
 * says whether the first can be trusted. */
export function knobSecondary(
  knob: string,
  value: number | null | (number | null)[],
): string | null {
  if (knob !== "seller_quality" || !Array.isArray(value)) return null;
  const n = value[1];
  return n === null || n === undefined ? null : `on ${count(n)} ratings`;
}

export type Verdict = "pass" | "fail" | "withheld";

export function verdictOf(passed: boolean | null): Verdict {
  if (passed === true) return "pass";
  if (passed === false) return "fail";
  return "withheld";
}

/** The word, not the colour. Every verdict on this board carries this text
 * because colour alone cannot distinguish three states for every reader. */
export function verdictLabel(v: Verdict): string {
  return v === "pass" ? "Clears" : v === "fail" ? "Fails" : "Withheld";
}
