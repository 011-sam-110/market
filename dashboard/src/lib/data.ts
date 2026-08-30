/* Loading and decoding the bundle.
 *
 * The bundle is 21 MB raw and about 3 MB over the wire, with nothing sampled
 * and nothing capped — the payloads say so with `capped: false` and a
 * `limit`, and if either ever changes the board says so on screen rather
 * than quietly showing a truncated board. That is why the loaders below
 * check those fields instead of trusting them.
 *
 * Products and listings are columnar and dictionary-encoded: each column is
 * one array, and a dictionary column holds integer indices into
 * `dict[<column>]` with -1 for null. Decoding a row is therefore cheap and
 * decoding all 27,206 of them is not, so `decodeProduct` takes an index and
 * the views decode only what they render.
 */

import type {
  Bundle,
  CoveragePayload,
  DeltasPayload,
  ExportedKnob,
  FeesPayload,
  KnobColumns,
  KnobsPayload,
  ListingsPayload,
  Manifest,
  NichesPayload,
  ProductRow,
  ProductsPayload,
} from "./types";
import type { KnobName, Knobs } from "./knobs";

const BASE = "data";

async function getJSON<T>(file: string): Promise<T> {
  const res = await fetch(`${import.meta.env.BASE_URL}${BASE}/${file}`, {
    cache: "no-cache",
  });
  if (!res.ok) {
    throw new Error(
      `${file} came back ${res.status}. The bundle is written by ` +
        `scripts/export_dashboard.py; a missing file means the export did ` +
        `not finish, not that there is no data.`,
    );
  }
  return (await res.json()) as T;
}

/** Everything the board needs before it can render anything at all.
 *
 * Listings and product metrics are NOT here: they are 7.2 MB and 4.1 MB and
 * only two views need them, so they load on demand. */
export async function loadBundle(): Promise<Bundle> {
  const manifest = await getJSON<Manifest>("manifest.json");
  const d = manifest.latest_date;
  const window = manifest.difference_window;

  const [knobs, fees, niches, products, coverage] = await Promise.all([
    getJSON<KnobsPayload>("knobs.json"),
    getJSON<FeesPayload>("fees.json"),
    getJSON<NichesPayload>(`niches-${d}.json`),
    getJSON<ProductsPayload>(`products-${d}.json`),
    getJSON<CoveragePayload>(`coverage-${d}.json`),
  ]);

  const deltas =
    manifest.has_difference && window
      ? await getJSON<DeltasPayload>(
          `deltas-${window.date_a}-${window.date_b}.json`,
        )
      : null;

  return { manifest, knobs, fees, niches, products, coverage, deltas };
}

export function loadListings(latestDate: string): Promise<ListingsPayload> {
  return getJSON<ListingsPayload>(`listings-${latestDate}.json`);
}

export function loadProductMetrics(
  latestDate: string,
): Promise<Record<string, unknown>> {
  return getJSON(`product-metrics-${latestDate}.json`);
}

/* ------------------------------------------------------------- decoding */

/** Resolve a dictionary-encoded cell. `-1` is null, by the payload's own
 * `null_index`; a raw string means the column was not dictionary-encoded. */
export function dictValue(
  payload: { dict: Record<string, string[]>; null_index: number },
  column: string,
  raw: number | string | boolean | null | undefined,
): string | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw === "string") return raw;
  if (typeof raw === "boolean") return String(raw);
  if (raw === payload.null_index) return null;
  const table = payload.dict[column];
  return table ? (table[raw] ?? null) : null;
}

function knobAt(
  name: KnobName,
  slot: KnobColumns,
  notes: string[],
  i: number,
): ExportedKnob {
  const passed = slot.passed[i] ?? null;
  const threshold =
    slot.threshold_constant !== null && slot.threshold_constant !== undefined
      ? slot.threshold_constant
      : (slot.threshold?.[i] ?? 0);
  const noteIdx = slot.note_idx[i];
  return {
    knob: name,
    value: slot.value[i] ?? null,
    threshold,
    passed,
    denominator: slot.denominator[i] ?? 0,
    coverage: slot.coverage[i] ?? 0,
    note: noteIdx === undefined || noteIdx < 0 ? "" : (notes[noteIdx] ?? ""),
    unknown: passed === null,
  };
}

/** One product row, decoded out of the column arrays. */
export function decodeProduct(p: ProductsPayload, i: number): ProductRow {
  const col = <T,>(name: string): T => p.columns[name]?.[i] as T;
  const str = (name: string): string =>
    dictValue(p, name, p.columns[name]?.[i]) ?? "";

  return {
    index: i,
    key: col<string>("key"),
    label: col<string>("label"),
    leaf_category_id: str("leaf_category_id"),
    identity_tier: str("identity_tier"),
    str_source: str("str_source"),
    score: col<number>("score"),
    n_listings: col<number>("n_listings"),
    n_members: col<number>("n_members"),
    n_sellers: col<number>("n_sellers"),
    n_business: col<number>("n_business"),
    n_private: col<number>("n_private"),
    n_passed: col<number>("n_passed"),
    n_failed: col<number>("n_failed"),
    n_unknown: col<number>("n_unknown"),
    sold_known: col<number>("sold_known"),
    max_cost_at_floor_pence: col<number | null>("max_cost_at_floor_pence"),
    max_cost_at_median_pence: col<number | null>("max_cost_at_median_pence"),
    knobs: p.knob_names.map((name) =>
      knobAt(name, p.knobs.by_knob[name], p.knobs.notes, i),
    ),
  };
}

/* ------------------------------------------------- re-thresholding client-side */

/** Whether a knob's verdict can be recomputed in the browser at all.
 *
 * The board ships INPUTS, not verdicts, for exactly five of the six knobs.
 * The sixth — sell-through — depends on a window length the exporter
 * measured, and its withhold rule is reproduced faithfully in `knobs.ts`.
 * What the browser cannot do is re-derive a median from listings it does not
 * hold per product, so moving a threshold re-runs the COMPARISON, never the
 * measurement. That distinction is why a knob whose denominator is 0 stays
 * unknown no matter where the slider goes. */
export function isRethresholdable(k: ExportedKnob): boolean {
  return k.denominator > 0;
}

/** The knob defaults as the exporter recorded them, as a `Knobs` object. */
export function knobsFromPayload(payload: KnobsPayload): Knobs {
  const out = {} as Record<string, number>;
  for (const f of payload.fields) out[f.field] = f.value;
  return out as unknown as Knobs;
}

export function knobDefaults(payload: KnobsPayload): Knobs {
  const out = {} as Record<string, number>;
  for (const f of payload.fields) out[f.field] = f.default;
  return out as unknown as Knobs;
}

/* ------------------------------------------------------------ integrity */

export interface BundleWarning {
  what: string;
  detail: string;
}

/** Anything about the bundle the operator should be told before reading it.
 *
 * A capped board and a stale bundle both look completely normal on screen.
 * This is what makes them visible. */
export function bundleWarnings(b: Bundle): BundleWarning[] {
  const out: BundleWarning[] = [];

  if (b.products.capped) {
    out.push({
      what: "The product board is truncated",
      detail: `Only ${b.products.n.toLocaleString()} of the products were exported (limit ${b.products.limit.toLocaleString()}). Rankings below the cut are missing entirely.`,
    });
  }
  if (b.niches.capped) {
    out.push({
      what: "The niche board is truncated",
      detail: `Only ${b.niches.n.toLocaleString()} niches were exported (limit ${b.niches.limit.toLocaleString()}).`,
    });
  }
  if (b.manifest.empty) {
    out.push({
      what: "The store is empty",
      detail:
        "The export ran against a store with no rows. Every figure below is absent rather than zero.",
    });
  }
  if (!b.manifest.has_difference) {
    out.push({
      what: "Nothing can be differenced yet",
      detail:
        "Day-over-day change needs two snapshots. Until then sell-through is a lifetime average, which is not comparable to the window threshold.",
    });
  }
  if (b.coverage.latest_date !== b.manifest.latest_date) {
    out.push({
      what: "The coverage payload is from a different day",
      detail: `manifest says ${b.manifest.latest_date}, coverage says ${b.coverage.latest_date}. One of the two files is stale.`,
    });
  }
  return out;
}
