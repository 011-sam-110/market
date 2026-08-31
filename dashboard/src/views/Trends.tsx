/* Change — one day-pair difference, and what everything else waits for.
 *
 * The store holds two snapshots. Two snapshots make one difference. They do
 * not make a trend. This view therefore draws no line, no sparkline, no arrow
 * and no percentage-change badge. It reports one difference and it names the
 * conditions that hold the rest of the board shut.
 *
 * Two shapes in this payload disagree with `lib/types.ts`, and the runtime
 * shape wins. `aggregates_omitted.niche` is an object with `of` and `omitted`,
 * not a number. Each `DeltaAggregate` also carries `worst_negative`. The
 * readers below narrow both from `unknown` instead of trusting the
 * declaration, because a wrong declaration here would print a wrong number.
 *
 * Every sentence the pipeline wrote is quoted, never paraphrased. A note that
 * explains a refusal is the product, so rewording it would change the claim.
 */

import { useEffect, useMemo, useState } from "react";

import { Bars, Composition, Locked, type BarDatum, type Segment } from "../components/charts";
import { Figure } from "../components/Figure";
import { Stamp } from "../components/Stamp";
import { dictValue, loadDeltaDetail } from "../lib/data";
import {
  bandLabel,
  bpPercent,
  count,
  coveragePercent,
  denominatorLine,
  isoDay,
  leafLabel,
} from "../lib/format";
import type {
  Bundle,
  Comparability,
  DeltaAggregate,
  DeltaDetailPayload,
  DeltasPayload,
} from "../lib/types";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/trends.css";

/* ------------------------------------------------------------- readers */

/* The coverage panels ship their `data` as `unknown`. Each reader below
 * checks the fields it needs and returns null when one is absent. A missing
 * panel then renders as a named absence, never as a zero. */

type Rec = Record<string, unknown>;

function isRec(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function asString(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** The `data` of one live coverage panel, or null when the panel is absent. */
function panelData(bundle: Bundle, id: string): unknown {
  const panel = bundle.coverage.panels.find((p) => p.id === id);
  return panel && panel.state === "live" ? panel.data : null;
}

interface Churn {
  fresh: number;
  gone: number;
  persisting: number;
  priceKnown: number;
  priceMoved: number;
  soldKnown: number;
  soldListings: number;
}

function readChurn(data: unknown): Churn | null {
  if (!isRec(data)) return null;
  const fresh = asNumber(data["new"]);
  const gone = asNumber(data["gone"]);
  const persisting = asNumber(data["persisting"]);
  const priceKnown = asNumber(data["price_known"]);
  const priceMoved = asNumber(data["price_moved"]);
  const soldKnown = asNumber(data["sold_known"]);
  const soldListings = asNumber(data["sold_listings"]);
  if (
    fresh === null ||
    gone === null ||
    persisting === null ||
    priceKnown === null ||
    priceMoved === null ||
    soldKnown === null ||
    soldListings === null
  ) {
    return null;
  }
  return { fresh, gone, persisting, priceKnown, priceMoved, soldKnown, soldListings };
}

interface Sellers {
  entered: number;
  exited: number;
  persisting: number;
  total: number;
}

function readSellers(data: unknown): Sellers | null {
  if (!isRec(data)) return null;
  const entered = Array.isArray(data["entered"]) ? data["entered"].length : null;
  const exited = Array.isArray(data["exited"]) ? data["exited"].length : null;
  const persisting = asNumber(data["persisting"]);
  if (entered === null || exited === null || persisting === null) return null;
  return { entered, exited, persisting, total: entered + exited + persisting };
}

interface Drift {
  leaves: number;
  comparable: number;
  up: number;
  down: number;
  flat: number;
}

/** Count the leaves whose median price the pipeline read on both snapshots.
 *
 * The panel ships a median with no count behind it, so this reader deliberately
 * returns counts of leaves and never a price. A price with no denominator has
 * no place on this board. */
function readDrift(data: unknown): Drift | null {
  if (!isRec(data)) return null;
  const series = data["series"];
  if (!isRec(series)) return null;
  let leaves = 0;
  let comparable = 0;
  let up = 0;
  let down = 0;
  let flat = 0;
  for (const points of Object.values(series)) {
    leaves += 1;
    if (!Array.isArray(points) || points.length !== 2) continue;
    const first = points[0];
    const last = points[1];
    if (!isRec(first) || !isRec(last)) continue;
    const a = asNumber(first["p50"]);
    const b = asNumber(last["p50"]);
    if (a === null || b === null) continue;
    comparable += 1;
    if (b > a) up += 1;
    else if (b < a) down += 1;
    else flat += 1;
  }
  return { leaves, comparable, up, down, flat };
}

interface SoldColumn {
  dates: string[];
  fractions: number[];
}

function readSoldColumn(data: unknown): SoldColumn | null {
  if (!isRec(data)) return null;
  const dates = Array.isArray(data["dates"])
    ? data["dates"].filter((d): d is string => typeof d === "string")
    : [];
  const series = data["series"];
  if (!isRec(series)) return null;
  const raw = series["sold_qty"];
  if (!Array.isArray(raw)) return null;
  const fractions = raw.filter((v): v is number => typeof v === "number");
  if (dates.length !== fractions.length || fractions.length === 0) return null;
  return { dates, fractions };
}

interface SellThroughRow {
  niche: string;
  nKnown: number;
  nListings: number;
  windowBp: number;
}

interface SellThroughPanel {
  rows: SellThroughRow[];
  total: number;
  unreadable: number;
  thresholdBp: number | null;
}

function readSellThrough(data: unknown): SellThroughPanel | null {
  if (!isRec(data)) return null;
  const raw = data["rows"];
  if (!Array.isArray(raw)) return null;
  const rows: SellThroughRow[] = [];
  for (const item of raw) {
    if (!isRec(item)) continue;
    const niche = asString(item["niche"]);
    const nKnown = asNumber(item["n_known"]);
    const nListings = asNumber(item["n_listings"]);
    const windowBp = asNumber(item["str_window_bp"]);
    if (niche === null || nKnown === null || nListings === null || windowBp === null) {
      continue;
    }
    rows.push({ niche, nKnown, nListings, windowBp });
  }
  return {
    rows,
    total: asNumber(data["total"]) ?? raw.length,
    unreadable: asNumber(data["unreadable"]) ?? 0,
    thresholdBp: asNumber(data["threshold_bp"]),
  };
}

interface Ledger {
  gaps: number[];
  blockers: { what: string; why: string }[];
  consecutiveness: string | null;
  unlocks: Map<number, string>;
}

function readLedger(data: unknown): Ledger | null {
  if (!isRec(data)) return null;
  const gaps = Array.isArray(data["gaps"])
    ? data["gaps"].filter((g): g is number => typeof g === "number")
    : [];
  const blockers: { what: string; why: string }[] = [];
  if (Array.isArray(data["blockers"])) {
    for (const pair of data["blockers"]) {
      if (!Array.isArray(pair)) continue;
      const what = asString(pair[0]);
      const why = asString(pair[1]);
      if (what !== null && why !== null) blockers.push({ what, why });
    }
  }
  /* The manifest carries the same unlock sentences, but its copy reached the
   * browser with its em-dashes mis-encoded. This panel holds the correct text,
   * so the view quotes this copy and repairs nothing itself. */
  const unlocks = new Map<number, string>();
  if (Array.isArray(data["unlocks"])) {
    for (const item of data["unlocks"]) {
      if (!isRec(item)) continue;
      const need = asNumber(item["need"]);
      const what = asString(item["what"]);
      if (need !== null && what !== null) unlocks.set(need, what);
    }
  }
  return {
    gaps,
    blockers,
    consecutiveness: asString(data["consecutiveness"]),
    unlocks,
  };
}

interface Pruned {
  of: number;
  omitted: number;
}

/** `aggregates_omitted.niche` and `.product` are declared as numbers and ship
 * as objects. Narrow from `unknown` so the wrong declaration cannot print. */
function readPruned(value: unknown): Pruned | null {
  if (!isRec(value)) return null;
  const of = asNumber(value["of"]);
  const omitted = asNumber(value["omitted"]);
  return of === null || omitted === null ? null : { of, omitted };
}

/* ----------------------------------------------------------- utilities */

function fmt(value: number): string {
  return count(value) ?? String(value);
}

function signed(value: number): string {
  const body = fmt(Math.abs(value));
  if (value > 0) return `+${body}`;
  if (value < 0) return `−${body}`;
  return body;
}

function nicheLabel(key: string): string {
  const parts = key.split("@");
  const leaf = parts[0] ?? null;
  const band = parts[1] ?? null;
  return `${leafLabel(leaf)} · ${bandLabel(band)}`;
}

/** ISO day arithmetic in UTC. `new Date("2026-08-28")` parses as UTC midnight
 * and formats one day west in a negative offset, which would move a derived
 * date by a whole night. */
function dayMs(iso: string): number | null {
  const parts = iso.split("-").map(Number);
  const [y, m, d] = parts;
  if (!y || !m || !d) return null;
  return Date.UTC(y, m - 1, d);
}

function addDays(iso: string, days: number): string | null {
  const base = dayMs(iso);
  if (base === null) return null;
  return new Date(base + days * 86_400_000).toISOString().slice(0, 10);
}

function daysBetween(from: string, to: string): number | null {
  const a = dayMs(from);
  const b = dayMs(to);
  if (a === null || b === null) return null;
  return Math.round((b - a) / 86_400_000);
}

/* ------------------------------------------------------- small pieces */

function Chain({
  rows,
}: {
  rows: { label: string; value: string; den?: string }[];
}) {
  return (
    <ol className="trends-chain">
      {rows.map((row) => (
        <li key={row.label}>
          <span className="trends-chain__label">{row.label}</span>
          <span className="trends-chain__value">{row.value}</span>
          {row.den ? <span className="den trends-chain__den">{row.den}</span> : null}
        </li>
      ))}
    </ol>
  );
}

function Steps({
  steps,
}: {
  steps: { label: string; value: string; derived?: boolean; caveat?: string }[];
}) {
  return (
    <ol className="trends-steps">
      {steps.map((step) => (
        <li key={step.label}>
          <span className="trends-steps__label">{step.label}</span>
          <span
            className={`trends-steps__value ${
              step.derived ? "trends-steps__value--derived" : ""
            }`}
          >
            {step.value}
          </span>
          {step.caveat ? (
            <span className="trends-steps__caveat">{step.caveat}</span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

/* One of the two sections that need the detail file, in whichever state it
 * is in. Never a zero and never an empty list while the fetch is in flight:
 * an empty movers list means "the pipeline found no product whose counter
 * moved", which is a real and different claim. */
function Detail({
  detail,
  error,
  what,
  children,
}: {
  detail: DeltaDetailPayload | null;
  error: string | null;
  what: string;
  children: () => React.ReactNode;
}) {
  if (error !== null) {
    return (
      <div className="state state--inline">
        <h4 className="state__title">{what} did not load</h4>
        <p className="state__body">{error}</p>
      </div>
    );
  }
  if (detail === null) {
    return (
      <p className="panel__note trends-pending">
        Loading {what}. This part of the difference is one file on its own,
        because it is the part that grows with the population.
      </p>
    );
  }
  return <>{children()}</>;
}

/** A sentence the pipeline wrote, marked as quoted and never reworded. */
function Quote({ children }: { children: React.ReactNode }) {
  return <blockquote className="trends-quote">{children}</blockquote>;
}

/* ------------------------------------------------------------- the view */

export function Trends({ bundle }: ViewProps) {
  const deltas = bundle.deltas;

  if (!deltas) {
    return <NothingToDifference bundle={bundle} />;
  }

  if (deltas.state !== "live") {
    return (
      <div className="view">
        <div className="view__head">
          <h2 className="view__title">Change</h2>
          <p className="view__lede">
            The difference payload reports state “{deltas.state}”. The pipeline
            wrote the file and then withheld its contents.
          </p>
        </div>
        <div className="state">
          <h3 className="state__title">The difference is unavailable</h3>
          <p className="state__body">
            This view reports no figure while the payload withholds one. It
            shows no zero in place of a withheld figure.
          </p>
        </div>
      </div>
    );
  }

  return <DayPair bundle={bundle} deltas={deltas} />;
}

/* ------------------------------------------- the one-date empty state */

function NothingToDifference({ bundle }: { bundle: Bundle }) {
  const { manifest } = bundle;
  const ledger = readLedger(panelData(bundle, "date_ledger"));

  return (
    <div className="view">
      <div className="view__head">
        <h2 className="view__title">Change</h2>
        <p className="view__lede">
          The store holds {fmt(manifest.n_dates)} snapshot. The pipeline needs
          two snapshots before it can difference anything.
        </p>
      </div>

      <section className="panel">
        <h3 className="panel__title">Nothing to difference yet</h3>
        <p className="panel__note">
          A listing can only be differenced when eBay showed a sold counter on
          both snapshots. The store holds one snapshot, so no listing qualifies.
          This view reports that as an absence. It reports no zero.
        </p>
      </section>

      <section className="panel">
        <h3 className="panel__title">What the second snapshot unlocks</h3>
        <div className="trends-locks">
          {manifest.unlocks.map((u) => (
            <Locked
              key={u.at_dates}
              title={`At ${u.at_dates} snapshots`}
              needs={u.at_dates}
              have={manifest.n_dates}
              shows={ledger?.unlocks.get(u.at_dates) ?? u.unlocks}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

/* --------------------------------------------------- the two-date view */

const MOVERS_HEAD = 8;
const PRODUCTS_HEAD = 6;

/* Whether the two nights swept the same population.
 *
 * This sits above every figure on the view because it governs all of them. A
 * count that moves between two unlike sweeps measures the sweep, and the
 * reader has no way to tell that apart from a market that moved — the numbers
 * look identical and healthy either way. That failure has a shape here: the
 * nightly job carried `--pages-per-shard 10` while the two snapshots in the
 * store were both swept at 3, and the first scheduled night would have booked
 * pages four to ten as thousands of overnight arrivals.
 *
 * A clean verdict prints one quiet line. It is not a badge and it does not
 * congratulate the pipeline. An unclean one owns the top of the view.
 */
function PopulationVerdict({ verdict }: { verdict: Comparability | null }) {
  if (verdict === null) {
    return (
      <div className="verdict verdict--unknown">
        <Stamp scale="figure">no verdict</Stamp>
        <p className="verdict__note">
          This build read no sweep footprint, so it cannot say whether the two
          nights swept the same population. Treat every figure below as a
          difference between two sweeps of unknown shape.
        </p>
      </div>
    );
  }

  if (verdict.comparable) {
    return (
      <p className="verdict verdict--clean">
        Both nights swept the same leaves to the same depth, under the same
        sweep settings. The difference below is a difference in the market.
      </p>
    );
  }

  /* Named, not counted. "3 leaves changed depth" is a number the reader can
   * do nothing with; the leaf and the two page numbers are what they can go
   * and check. Long lists are capped and the cap says what it hid. */
  const depth = Object.entries(verdict.depth_changed);
  const sweep = Object.entries(verdict.sweep_changed);

  return (
    <div className="verdict verdict--broken">
      <Stamp scale="record" tone="fail">
        not comparable
      </Stamp>
      <p className="verdict__note">{verdict.note}</p>

      {sweep.length > 0 ? (
        <dl className="verdict__list">
          {sweep.map(([field, [before, after]]) => (
            <div key={field} className="verdict__row">
              <dt>{field.replace(/_/g, " ")}</dt>
              <dd>
                {before === null ? "unrecorded" : String(before)} →{" "}
                {after === null ? "unrecorded" : String(after)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {depth.length > 0 ? (
        <dl className="verdict__list">
          {depth.slice(0, VERDICT_HEAD).map(([leaf, [before, after]]) => (
            <div key={leaf} className="verdict__row">
              <dt>leaf {leaf}</dt>
              <dd>
                page {before} → page {after}
              </dd>
            </div>
          ))}
          {depth.length > VERDICT_HEAD ? (
            <div className="verdict__row verdict__row--more">
              <dt>and {fmt(depth.length - VERDICT_HEAD)} more</dt>
              <dd>not listed</dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      {verdict.leaves_added.length > 0 || verdict.leaves_removed.length > 0 ? (
        <p className="verdict__note">
          {fmt(verdict.leaves_added.length)} leaves entered the sweep and{" "}
          {fmt(verdict.leaves_removed.length)} left it. A leaf that eBay blocked
          on one night looks exactly like a leaf that was dropped, so both are
          reported.
        </p>
      ) : null}
    </div>
  );
}

/* Enough rows to see the shape of the change, not enough to bury the note
 * that explains it. The count of what is hidden is always printed. */
const VERDICT_HEAD = 8;

function DayPair({ bundle, deltas }: { bundle: Bundle; deltas: DeltasPayload }) {
  const { manifest } = bundle;
  const [allNiches, setAllNiches] = useState(false);

  /* The per-listing arrays and the per-product aggregates. They are the two
   * parts of a difference that grow with the population, and the board loads
   * the deltas summary before it paints anything, so they live in their own
   * file and only this view fetches it.
   *
   * Every section below that needs them says so while it waits, and says so
   * if the fetch fails. None of them falls back to a zero: "not loaded yet"
   * and "measured, and it is nothing" are different claims, and this board
   * has spent its whole design keeping them apart. */
  const [detail, setDetail] = useState<DeltaDetailPayload | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setDetailError(null);
    loadDeltaDetail(deltas.detail_file).then(
      (d) => {
        if (live) setDetail(d);
      },
      (err: unknown) => {
        if (live) setDetailError(err instanceof Error ? err.message : String(err));
      },
    );
    return () => {
      live = false;
    };
  }, [deltas.detail_file]);

  const churn = readChurn(panelData(bundle, "population_churn"));
  const sellers = readSellers(panelData(bundle, "seller_entry_exit"));
  const drift = readDrift(panelData(bundle, "price_drift"));
  const soldColumn = readSoldColumn(panelData(bundle, "coverage_trend"));
  const sellThrough = readSellThrough(panelData(bundle, "sell_through"));
  const ledger = readLedger(panelData(bundle, "date_ledger"));

  const prunedNiches = readPruned(deltas.aggregates_omitted.niche);
  const prunedProducts = readPruned(deltas.aggregates_omitted.product);

  /* The elapsed gap the pipeline actually observed, taken over the listings it
   * could difference. Every listing it could not difference carries exactly
   * 1.0, which is the window label rather than a measurement. */
  const gap = useMemo(() => {
    if (detail === null) return null;
    const unknownIndex = deltas.buckets.indexOf("unknown");
    const { bucket, days } = detail.listings;
    const values: number[] = [];
    for (let i = 0; i < bucket.length; i += 1) {
      const b = bucket[i];
      if (b === undefined || b === unknownIndex) continue;
      const d = days[i];
      if (typeof d === "number" && Number.isFinite(d)) values.push(d);
    }
    if (values.length === 0) return null;
    values.sort((a, b) => a - b);
    const mid = values[Math.floor(values.length / 2)];
    return {
      n: values.length,
      min: values[0] ?? 0,
      max: values[values.length - 1] ?? 0,
      median: mid ?? 0,
    };
  }, [deltas, detail]);

  /* Two sums, never one. The view keeps them apart at every stage, including
   * the stage that computes them. */
  const sums = useMemo(() => {
    let up = 0;
    let down = 0;
    for (const a of deltas.by_niche) {
      up += a.sum_delta_known_positive;
      down += a.sum_delta_known_negative;
    }
    return { up, down };
  }, [deltas]);

  const increasing = useMemo(
    () =>
      deltas.by_niche
        .filter((a) => a.sum_delta_known_positive > 0)
        .sort((a, b) => b.sum_delta_known_positive - a.sum_delta_known_positive),
    [deltas],
  );

  const decreasing = useMemo(
    () =>
      deltas.by_niche
        .filter((a) => a.sum_delta_known_negative < 0)
        .sort((a, b) => a.sum_delta_known_negative - b.sum_delta_known_negative),
    [deltas],
  );

  /* 6,843 product keys resolve against the products payload the shell already
   * holds, so a mover can print its title instead of its cluster id. */
  const productLabel = useMemo(() => {
    const products = bundle.products;
    const keys = products.columns["key"] ?? [];
    const labels = products.columns["label"];
    const map = new Map<string, string>();
    for (let i = 0; i < keys.length; i += 1) {
      const key = keys[i];
      if (typeof key !== "string") continue;
      map.set(key, dictValue(products, "label", labels?.[i]) ?? key);
    }
    return map;
  }, [bundle.products]);

  const byProduct = detail?.by_product ?? [];

  const productsUp = useMemo(
    () =>
      byProduct
        .filter((a) => a.sum_delta_known_positive > 0)
        .sort((a, b) => b.sum_delta_known_positive - a.sum_delta_known_positive)
        .slice(0, PRODUCTS_HEAD),
    [byProduct],
  );

  const productsDown = useMemo(
    () =>
      byProduct
        .filter((a) => a.sum_delta_known_negative < 0)
        .sort((a, b) => a.sum_delta_known_negative - b.sum_delta_known_negative)
        .slice(0, PRODUCTS_HEAD),
    [deltas],
  );

  const nicheCovered = useMemo(
    () => deltas.by_niche.reduce((total, a) => total + bucketTotal(a), 0),
    [deltas],
  );

  const productCovered = useMemo(
    () => byProduct.reduce((total, a) => total + bucketTotal(a), 0),
    [byProduct],
  );

  const bucketSum =
    deltas.counts.unknown +
    deltas.counts.bounded_refused +
    deltas.counts.known_negative +
    deltas.counts.known_positive;

  const segments: Segment[] = [
    {
      key: "unknown",
      label: "Unknown",
      value: deltas.counts.unknown,
      color: "var(--withheld)",
      note: "The pipeline found no comparable pair. eBay showed the listing on one snapshot only, or showed no sold counter on one snapshot.",
    },
    {
      key: "bounded_refused",
      label: "Bounded, refused",
      value: deltas.counts.bounded_refused,
      color: "var(--bounded)",
      note: "A bound applied on one side, so the pipeline refuses the delta. The count is a real count within a disjoint split.",
    },
    {
      key: "known_negative",
      label: "Known decrease",
      value: deltas.counts.known_negative,
      color: "var(--fail-mark)",
      note: "eBay's sold counter decreased between the two reads. The pipeline keeps these observations and excludes them from metrics.",
    },
    {
      key: "known_positive",
      label: "Known increase",
      value: deltas.counts.known_positive,
      color: "var(--pass)",
      note: "eBay's sold counter increased between the two reads.",
    },
  ];

  const topSellThrough = sellThrough
    ? sellThrough.rows.reduce<SellThroughRow | null>(
        (best, row) => (best === null || row.windowBp > best.windowBp ? row : best),
        null,
      )
    : null;

  const window = deltas.sell_through_comparable;
  const gate = Math.ceil(window.min_comparable_days);
  const firstDate = manifest.dates[0] ?? deltas.date_a;
  const lastDate = manifest.dates[manifest.dates.length - 1] ?? deltas.date_b;
  const heldWindow = daysBetween(firstDate, lastDate);
  const comparableFrom = addDays(firstDate, gate);
  const targetFrom = addDays(firstDate, window.target_days);
  const nightsToGate = heldWindow === null ? null : Math.max(0, gate - heldWindow);
  const nightsToTarget =
    heldWindow === null ? null : Math.max(0, window.target_days - heldWindow);

  const nicheBars = (rows: DeltaAggregate[], negative: boolean): BarDatum[] =>
    rows.map((a) => {
      const value = negative
        ? Math.abs(a.sum_delta_known_negative)
        : a.sum_delta_known_positive;
      const denominator = negative ? a.counts.known_negative : a.counts.known_positive;
      const worst = worstNegative(a);
      return {
        label: nicheLabel(a.key),
        value,
        denominator,
        population: bucketTotal(a),
        flagged: negative,
        ...(negative
          ? {
              note: `Signed sum ${signed(a.sum_delta_known_negative)}. Worst single listing ${
                worst === null ? "not reported" : signed(worst)
              }.`,
            }
          : {}),
      };
    });

  const increasingShown = allNiches ? increasing : increasing.slice(0, MOVERS_HEAD);
  const decreasingShown = allNiches ? decreasing : decreasing.slice(0, MOVERS_HEAD);
  const barMax = Math.max(
    1,
    ...increasingShown.map((a) => a.sum_delta_known_positive),
    ...decreasingShown.map((a) => Math.abs(a.sum_delta_known_negative)),
  );

  return (
    <div className="view">
      <div className="view__head">
        <h2 className="view__title">Change</h2>
        <p className="view__lede">
          The store holds two snapshots. This view reports the one difference
          between them. It reports nothing about a direction over time.
        </p>
        <div className="trends__discipline">
          <p>Two points are not a trend.</p>
          <p>
            This view draws no line, no sparkline, no arrow and no
            percentage-change badge. Every figure below describes the pair{" "}
            {isoDay(deltas.date_a)} to {isoDay(deltas.date_b)}.
          </p>
        </div>
      </div>

      <PopulationVerdict verdict={deltas.population_comparable} />

      {/* ---------------------------------------------------- the pair */}

      <section className="panel">
        <h3 className="panel__title">The snapshot pair</h3>
        <p className="panel__note">
          The pipeline can difference a listing only when eBay showed a sold
          counter on both snapshots. That condition is the reason the
          differenced count is small.
        </p>

        <Figure
          scale="record"
          name="Listings differenced"
          value={fmt(deltas.n_known)}
          denominator={deltas.n_known}
          population={deltas.n_listings}
          coverage={deltas.coverage}
          against={`Window ${fmt(deltas.window_days)} day · ${isoDay(
            deltas.date_a,
          )} to ${isoDay(deltas.date_b)}`}
        />

        {churn ? (
          <Chain
            rows={[
              {
                label: `Listings read on ${isoDay(deltas.date_a)}`,
                value: fmt(counts_for(manifest, deltas.date_a)),
              },
              {
                label: `Listings read on ${isoDay(deltas.date_b)}`,
                value: fmt(counts_for(manifest, deltas.date_b)),
              },
              {
                label: "Listings present on both snapshots",
                value: fmt(churn.persisting),
                den: denominatorLine(churn.persisting, churn.soldListings),
              },
              {
                label: `Listings present only on ${isoDay(deltas.date_b)}`,
                value: fmt(churn.fresh),
                den: denominatorLine(churn.fresh, churn.soldListings),
              },
              {
                label: `Listings present only on ${isoDay(deltas.date_a)}`,
                value: fmt(churn.gone),
                den: denominatorLine(churn.gone, churn.soldListings),
              },
              {
                label: "Listings the pipeline considered",
                value: fmt(churn.soldListings),
                den: "the union of both snapshots",
              },
              {
                label: "Listings with a sold counter on both snapshots",
                value: fmt(churn.soldKnown),
                den: denominatorLine(churn.soldKnown, churn.persisting),
              },
            ]}
          />
        ) : (
          <p className="panel__note">
            The population churn panel is absent, so this view cannot show the
            arithmetic behind the differenced count.
          </p>
        )}

        {soldColumn ? (
          <>
            <h4 className="trends-sub">How often eBay published a sold counter</h4>
            <p className="panel__note">
              The store holds two readings of this column. It holds no third
              reading. Nothing here states whether coverage improves.
            </p>
            <div className="trends-pair">
              {soldColumn.dates.map((date, i) => {
                const fraction = soldColumn.fractions[i] ?? 0;
                const read = counts_for(manifest, date);
                return (
                  <div className="field" key={date}>
                    <Figure
                      name={isoDay(date)}
                      value={coveragePercent(fraction, 1)}
                      denominator={read}
                      population={0}
                    />
                  </div>
                );
              })}
            </div>
          </>
        ) : null}

        {gap ? (
          <>
            <h4 className="trends-sub">The gap the pipeline observed</h4>
            <p className="panel__note">
              The payload labels the window as {fmt(deltas.window_days)} day. The
              differenced pairs sit {gap.min.toFixed(2)} to {gap.max.toFixed(2)}{" "}
              days apart. The median gap is {gap.median.toFixed(2)} days. Every
              listing the pipeline could not difference carries exactly 1.00
              days, which is the window label and not a measurement.
            </p>
            <span className="den">
              {denominatorLine(gap.n, deltas.n_listings)}
            </span>
          </>
        ) : null}
      </section>

      {/* ------------------------------------------------- four buckets */}

      <section className="panel">
        <h3 className="panel__title">Four kinds of answer</h3>
        <p className="panel__note">
          The pipeline sorts every considered listing into one of four buckets.
          The buckets are disjoint. This view never adds two buckets together.
        </p>

        <Composition
          segments={segments}
          total={deltas.n_listings}
          caption="Each listing lands in exactly one bucket. Colour alone never names a bucket, so every segment carries a hatch and a written label."
        />

        <p className="den">
          {bucketSum === deltas.n_listings
            ? `The four counts sum to ${fmt(bucketSum)}, which matches the ${fmt(
                deltas.n_listings,
              )} listings considered`
            : `The four counts sum to ${fmt(
                bucketSum,
              )} and the payload reports ${fmt(
                deltas.n_listings,
              )} listings considered — the two disagree`}
        </p>

        <h4 className="trends-sub">The rule that keeps the buckets disjoint</h4>
        <Quote>
          <p>{deltas.bucket_precedence}</p>
        </Quote>

        <h4 className="trends-sub">
          Why the sold counter decreased for {fmt(deltas.counts.known_negative)}{" "}
          listings
        </h4>
        <p className="panel__note">
          A decrease is not a data error. The pipeline states the mechanism, and
          this view quotes it without change.
        </p>
        <Quote>
          <p>{deltas.note}</p>
        </Quote>
      </section>

      {/* ------------------------------------------- sell-through withheld */}

      <section className="panel">
        <h3 className="panel__title">Sell-through stays withheld</h3>
        <p className="panel__note">
          The pipeline measured sell-through. It refuses to compare that
          measurement to the threshold, because the window is too short. The
          refusal below is the pipeline's own output.
        </p>

        {topSellThrough && sellThrough ? (
          <div className="field field--withheld trends-refusal">
            <span className="field__name">
              Largest sell-through reading the pipeline measured
            </span>
            <span className="field__figure">
              {bpPercent(topSellThrough.windowBp, 0)}
            </span>
            <span className="den den--unknown">
              {denominatorLine(topSellThrough.nKnown, topSellThrough.nListings)}
            </span>
            <p className="trends-refusal__where">
              {nicheLabel(topSellThrough.niche)} · read across{" "}
              {fmt(sellThrough.total)} niches, of which{" "}
              {fmt(sellThrough.unreadable)} reported no window reading
            </p>
            <span className="trends-refusal__stamp">
              <Stamp title="The pipeline withheld this verdict.">withheld</Stamp>
            </span>
            <span className="trends-refusal__reason">
              the window is too short to compare against{" "}
              {sellThrough.thresholdBp === null
                ? "the threshold"
                : bpPercent(sellThrough.thresholdBp, 0)}
            </span>
          </div>
        ) : (
          <p className="panel__note">
            The sell-through panel is absent, so this view shows no reading here.
          </p>
        )}

        <Quote>
          <p>{window.note}</p>
        </Quote>

        <h4 className="trends-sub">When sell-through becomes comparable</h4>
        <p className="panel__note">
          This view derives the date rather than asserting it. Each step below
          is one line of that arithmetic.
        </p>
        <Steps
          steps={[
            {
              label: "Snapshots the store holds",
              value: fmt(manifest.dates.length),
              caveat: `${isoDay(firstDate)} and ${isoDay(lastDate)}.`,
            },
            {
              label: "Window the pipeline measured",
              value:
                heldWindow === null
                  ? "not derivable"
                  : `${fmt(heldWindow)} ${heldWindow === 1 ? "day" : "days"}`,
              caveat:
                "The pipeline subtracts the first snapshot date from the last snapshot date.",
            },
            {
              label: "Window the sell-through threshold targets",
              value: `${fmt(window.target_days)} days`,
            },
            {
              label: "Shortest window the pipeline will compare",
              value: `${fmt(gate)} days`,
              caveat: `The knob payload sets this at ${coveragePercent(
                bundle.knobs.sell_through_window.min_comparable_fraction,
                0,
              )} of the ${fmt(window.target_days)}-day target.`,
            },
            {
              label: "Earliest last snapshot that reaches the shortest window",
              value: comparableFrom === null ? "not derivable" : isoDay(comparableFrom),
              derived: true,
              caveat: `${isoDay(firstDate)} plus ${fmt(gate)} days.`,
            },
            {
              label: "Nights the store still needs to reach that window",
              value: nightsToGate === null ? "not derivable" : fmt(nightsToGate),
              derived: true,
              caveat:
                heldWindow === null
                  ? undefined
                  : `${fmt(gate)} days minus the ${fmt(heldWindow)} day already measured.`,
            },
            {
              label: `Earliest last snapshot that reaches the full ${fmt(
                window.target_days,
              )} days`,
              value: targetFrom === null ? "not derivable" : isoDay(targetFrom),
              caveat:
                nightsToTarget === null
                  ? undefined
                  : `${isoDay(firstDate)} plus ${fmt(
                      window.target_days,
                    )} days. That is ${fmt(nightsToTarget)} more nights.`,
            },
          ]}
        />
        <p className="panel__note">
          This arithmetic assumes one snapshot every night and no gap. The date
          ledger below reports what currently prevents that.
        </p>
      </section>

      {/* ------------------------------------------------------- locked */}

      <section className="panel">
        <h3 className="panel__title">Still locked</h3>
        <p className="panel__note">
          The manifest states what each zone needs and how far away it is. Each
          entry below states what it will show and when. None of them shows a
          placeholder chart.
        </p>

        <div className="trends-locks">
          {manifest.unlocks
            .filter((u) => u.state === "locked")
            .map((u) => (
              <Locked
                key={u.at_dates}
                title={`At ${u.at_dates} snapshots`}
                needs={u.at_dates}
                have={manifest.dates.length}
                shows={ledger?.unlocks.get(u.at_dates) ?? u.unlocks}
              />
            ))}
        </div>

        <h4 className="trends-sub">Already unlocked</h4>
        <ul className="trends-open">
          {manifest.unlocks
            .filter((u) => u.state === "live")
            .map((u) => (
              <li key={u.at_dates}>
                <span className="trends-open__at">
                  Live at {u.at_dates}{" "}
                  {u.at_dates === 1 ? "snapshot" : "snapshots"}
                </span>
                <p className="trends-open__what">
                  {ledger?.unlocks.get(u.at_dates) ?? u.unlocks}
                </p>
              </li>
            ))}
        </ul>
      </section>

      {/* ------------------------------------------------------- movers */}

      <section className="panel">
        <h3 className="panel__title">Where the sold counter moved</h3>
        <p className="panel__note">
          The pipeline reports two sums. One sum totals the increases. One sum
          totals the decreases. This view never subtracts one from the other,
          and it prints no net figure anywhere.
        </p>

        <div className="trends-pair">
          <div className="field">
            <Figure
              name="Units added across all increases"
              value={signed(sums.up)}
              denominator={deltas.counts.known_positive}
              population={deltas.n_listings}
            />
          </div>
          <div className="field field--fail">
            <Figure
              name="Units removed across all decreases"
              value={signed(sums.down)}
              denominator={deltas.counts.known_negative}
              population={deltas.n_listings}
            />
          </div>
        </div>

        <h4 className="trends-sub">Niches where the sold counter increased</h4>
        <Bars
          data={nicheBars(increasingShown, false)}
          max={barMax}
          asShare={false}
          caption="The bar shows the size of the increase. The count under each row states how many listings the pipeline differenced in that niche."
        />

        <h4 className="trends-sub">Niches where the sold counter decreased</h4>
        <Bars
          data={nicheBars(decreasingShown, true)}
          max={barMax}
          asShare={false}
          caption="Every figure in this list is negative. The bar shows the size of the decrease. The line under each row states the signed sum and the worst single listing."
        />

        <div className="trends-actions">
          <button
            type="button"
            className="btn btn--quiet"
            onClick={() => setAllNiches((v) => !v)}
          >
            {allNiches
              ? `Show the first ${MOVERS_HEAD} of each list`
              : `Show all ${fmt(increasing.length)} increasing and ${fmt(
                  decreasing.length,
                )} decreasing niches`}
          </button>
        </div>

        <h4 className="trends-sub">Products where the sold counter increased</h4>
        <p className="panel__note">
          The pipeline measured each of the largest increases over one listing.
          eBay's sold counter is not monotonic, so read the quoted mechanism
          above before you treat one of these as demand.
        </p>
        <Detail detail={detail} error={detailError} what="the product movers">
          {() => (
            <ol className="fanfold trends-movers">
              {productsUp.map((a, i) => (
                <MoverRow
                  key={a.key}
                  rank={i + 1}
                  title={productLabel.get(a.key) ?? a.key}
                  productKey={a.key}
                  value={signed(a.sum_delta_known_positive)}
                  denominator={a.counts.known_positive}
                  population={bucketTotal(a)}
                />
              ))}
            </ol>
          )}
        </Detail>

        <h4 className="trends-sub">Products where the sold counter decreased</h4>
        <Detail detail={detail} error={detailError} what="the product movers">
          {() => (
            <ol className="fanfold trends-movers trends-movers--decrease">
              {productsDown.map((a, i) => (
                <MoverRow
                  key={a.key}
                  rank={i + 1}
                  title={productLabel.get(a.key) ?? a.key}
                  productKey={a.key}
                  value={signed(a.sum_delta_known_negative)}
                  denominator={a.counts.known_negative}
                  population={bucketTotal(a)}
                />
              ))}
            </ol>
          )}
        </Detail>

        <h4 className="trends-sub">Groups the pipeline pruned</h4>
        <p className="panel__note">
          {prunedNiches
            ? `The pipeline pruned ${fmt(prunedNiches.omitted)} niche groups of ${fmt(
                prunedNiches.of,
              )}.`
            : "The payload reports no niche pruning count."}{" "}
          {prunedProducts
            ? `It pruned ${fmt(prunedProducts.omitted)} product groups of ${fmt(
                prunedProducts.of,
              )}.`
            : "It reports no product pruning count."}
        </p>
        <Quote>
          <p>{deltas.aggregates_omitted.reason}</p>
        </Quote>
        <p className="panel__note">
          The {fmt(deltas.by_niche.length)} niche groups shown above cover{" "}
          {fmt(nicheCovered)} of the {fmt(deltas.n_listings)} listings
          considered.{" "}
          {detail === null
            ? `The ${fmt(deltas.n_detail_products)} product groups are still loading.`
            : `The ${fmt(byProduct.length)} product groups cover ${fmt(
                productCovered,
              )}.`}{" "}
          The pipeline reports no delta for the remainder, and this view prints
          none.
        </p>
      </section>

      {/* -------------------------------------- other pair differences */}

      <section className="panel">
        <h3 className="panel__title">Other differences between the two snapshots</h3>

        {sellers ? (
          <>
            <h4 className="trends-sub">Sellers</h4>
            <p className="panel__note">
              The pipeline counts {fmt(sellers.total)} distinct sellers across
              both snapshots. This view reports the three counts separately. It
              never subtracts one count from another.
            </p>
            <div className="trends-pair trends-pair--three">
              <div className="field">
                <Figure
                  name={`Present only on ${isoDay(deltas.date_b)}`}
                  value={fmt(sellers.entered)}
                  denominator={sellers.entered}
                  population={sellers.total}
                />
              </div>
              <div className="field">
                <Figure
                  name={`Present only on ${isoDay(deltas.date_a)}`}
                  value={fmt(sellers.exited)}
                  denominator={sellers.exited}
                  population={sellers.total}
                />
              </div>
              <div className="field">
                <Figure
                  name="Present on both snapshots"
                  value={fmt(sellers.persisting)}
                  denominator={sellers.persisting}
                  population={sellers.total}
                />
              </div>
            </div>
          </>
        ) : null}

        {churn ? (
          <>
            <h4 className="trends-sub">Price on the listings present twice</h4>
            <div className="trends-pair">
              <div className="field">
                <Figure
                  name="Listings with a price on both snapshots"
                  value={fmt(churn.priceKnown)}
                  denominator={churn.priceKnown}
                  population={churn.soldListings}
                />
              </div>
              <div className="field">
                <Figure
                  name="Listings whose price changed"
                  value={fmt(churn.priceMoved)}
                  denominator={churn.priceMoved}
                  population={churn.priceKnown}
                />
              </div>
            </div>
          </>
        ) : null}

        {drift ? (
          <>
            <h4 className="trends-sub">Median price per leaf</h4>
            <p className="panel__note">
              The price drift panel reports a median price for each leaf. It
              reports no count behind that median. This view therefore draws no
              leaf price and no leaf move.
            </p>
            <p className="panel__note">
              The panel holds a median on both snapshots for{" "}
              {fmt(drift.comparable)} leaves of {fmt(drift.leaves)}. The median
              increased for {fmt(drift.up)} leaves. It decreased for{" "}
              {fmt(drift.down)} leaves. It did not change for {fmt(drift.flat)}{" "}
              leaves. The remaining {fmt(drift.leaves - drift.comparable)} leaves
              hold one reading only, which is unknown and not zero.
            </p>
            <span className="den">
              {denominatorLine(drift.comparable, drift.leaves)}
            </span>
          </>
        ) : null}
      </section>

      {/* -------------------------------------------------- date ledger */}

      <section className="panel">
        <h3 className="panel__title">The date ledger</h3>
        <p className="panel__note">
          The store holds {fmt(manifest.dates.length)} snapshots. The second
          snapshot is one night newer than the first. Every difference on this
          page uses that single night.
        </p>

        <div className="scroll-x">
          <table className="trends-ledger">
            <thead>
              <tr>
                <th scope="col">Snapshot</th>
                <th scope="col" className="is-num">
                  Listings
                </th>
                <th scope="col" className="is-num">
                  Rows
                </th>
              </tr>
            </thead>
            <tbody>
              {manifest.counts_by_date.map((row) => (
                <tr key={row.date}>
                  <th scope="row">{isoDay(row.date)}</th>
                  <td className="is-num">{fmt(row.listings)}</td>
                  <td className="is-num">{fmt(row.rows)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="panel__note">
          The ledger counts rows and distinct listings separately. The two
          numbers differ on both snapshots, so neither number can stand in for
          the other.
        </p>

        {ledger && ledger.gaps.length > 0 ? (
          <p className="panel__note">
            The gap between consecutive snapshots is{" "}
            {ledger.gaps.map((g) => fmt(g)).join(", ")} night.
          </p>
        ) : null}

        {ledger?.consecutiveness ? (
          <>
            <h4 className="trends-sub">Why a gap matters</h4>
            <Quote>
              <p>{ledger.consecutiveness}</p>
            </Quote>
          </>
        ) : null}

        {ledger && ledger.blockers.length > 0 ? (
          <>
            <h4 className="trends-sub">What stops the next snapshot</h4>
            <p className="panel__note">
              The countdown above assumes a snapshot every night. The ledger
              names {fmt(ledger.blockers.length)} reasons that no snapshot
              arrives on its own.
            </p>
            <ul className="trends-blockers">
              {ledger.blockers.map((b) => (
                <li key={b.what}>
                  <strong>{b.what}.</strong> {b.why}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </section>
    </div>
  );
}

/* --------------------------------------------------------- row pieces */

function MoverRow({
  rank,
  title,
  productKey,
  value,
  denominator,
  population,
}: {
  rank: number;
  title: string;
  productKey: string;
  value: string;
  denominator: number;
  population: number;
}) {
  return (
    <li className="entry">
      <div className="entry__head">
        <span className="entry__rank">{rank}</span>
        <div>
          <h5 className="entry__title">{title}</h5>
          <p className="entry__meta">{productKey}</p>
        </div>
        <div className="entry__score">
          <b>{value}</b>
          <span className="den">{denominatorLine(denominator, population)}</span>
        </div>
      </div>
    </li>
  );
}

/* ------------------------------------------------------------ helpers */

function bucketTotal(a: DeltaAggregate): number {
  return (
    a.counts.unknown +
    a.counts.bounded_refused +
    a.counts.known_negative +
    a.counts.known_positive
  );
}

/** `worst_negative` ships on every aggregate and is absent from the declared
 * type, so it is read through a guard rather than a cast. */
function worstNegative(a: DeltaAggregate): number | null {
  const raw: unknown = (a as unknown as Rec)["worst_negative"];
  return asNumber(raw);
}

/** How many listings one snapshot read, from the manifest's own ledger. */
function counts_for(manifest: Bundle["manifest"], date: string): number {
  const row = manifest.counts_by_date.find((r) => r.date === date);
  return row ? row.listings : 0;
}
