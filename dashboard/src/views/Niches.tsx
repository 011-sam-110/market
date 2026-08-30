/* Niches — one leaf crossed with one price band, and how much of each one
 * the pipeline measured.
 *
 * This view exists because "129 niches found" is not one measurement. It is
 * two measurements added together, and the sum hides which is which.
 *
 * The sweep entered a fixed list of hub leaves. The manifest names them in
 * `in_scope_leaves`. Every other leaf on this board arrived a second way. A
 * deepen call opened one item page, the page named a real leaf, and that leaf
 * became a niche of its own. So the niche count measures deepen coverage. It
 * does not measure how wide the market is.
 *
 * Two facts follow, and both are on screen:
 *   - the hub leaves hold almost every listing and almost no measurement;
 *   - the deepened leaves hold almost no listings and almost all measurement.
 * A ranking that adds the two puts a leaf with one listing above a leaf with
 * 731. This view separates them and never prints one total.
 *
 * The leaf comes from the `leaf_category_id` field on every row. The niche key
 * also contains a leaf id. This file never reads it from the key. That parse
 * is the exact bug that once emptied this board while the tests stayed green.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Bars, Composition } from "../components/charts";
import { Evidence, gradeEvidence } from "../components/Evidence";
import { Figure } from "../components/Figure";
import { KnobField } from "../components/KnobField";
import { Stamp } from "../components/Stamp";
import { decodeProduct, dictValue } from "../lib/data";
import {
  bandLabel,
  bpPercent,
  count,
  coveragePercent,
  isoDay,
  knobTitle,
  leafLabel,
  money,
  verdictLabel,
  verdictOf,
} from "../lib/format";
import type { KnobName } from "../lib/knobs";
import { bindingConstraint, rethresholdRow } from "../lib/rethreshold";
import type { Rethresholded } from "../lib/rethreshold";
import type {
  Bundle,
  DeltaAggregate,
  ExportedKnob,
  NicheRow,
  ProductRow,
} from "../lib/types";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/niches.css";

/* ------------------------------------------------------------ vocabulary */

/* One name for one thing, everywhere in this view.
 *   hub      — a leaf the sweep entered. The manifest lists it in scope.
 *   deepened — a leaf that one eBay item page named during a deepen call.
 * The words "coarse", "fine" and "department" never appear on screen. */
type LeafKind = "hub" | "deepened";

const KIND_NAME: Record<LeafKind, string> = {
  hub: "Swept hub",
  deepened: "Deepened leaf",
};

/* The five knobs whose denominator counts listings inside the niche.
 *
 * `saturation` is absent on purpose. Its denominator is 1 when the pipeline
 * supplied a rival count and 0 when it did not. It never counts listings. See
 * knobs.ts, check() step 6. Adding it to a coverage figure would report 100%
 * coverage on a niche the pipeline barely touched. */
const POPULATION_KNOBS: KnobName[] = [
  "min_margin",
  "sell_through",
  "price_band",
  "delivery",
  "seller_quality",
];

const DELTA_SEGMENTS: {
  key: "unknown" | "bounded_refused" | "known_negative" | "known_positive";
  label: string;
  color: string;
  note: string;
}[] = [
  {
    key: "unknown",
    label: "Unknown",
    color: "var(--withheld)",
    note: "eBay showed a sold count on one of the two days only. There is no delta to report.",
  },
  {
    key: "bounded_refused",
    label: "Bounded, refused",
    color: "var(--bounded)",
    note: "The pipeline bounded the delta but refused to report it as a figure.",
  },
  {
    key: "known_negative",
    label: "Known negative",
    color: "var(--fail-mark)",
    note: "The sold count fell. A relist resets the counter, so a fall is a real observation of eBay, not a sale.",
  },
  {
    key: "known_positive",
    label: "Known positive",
    color: "var(--pass)",
    note: "The sold count rose between the two days.",
  },
];

type SortKey =
  | "leaf"
  | "band"
  | "listings"
  | "sellers"
  | "classified"
  | "score"
  | "withheld"
  | "measured"
  | "differenced";

const SORT_LABELS: Record<SortKey, string> = {
  leaf: "Leaf",
  band: "Band",
  listings: "Listings",
  sellers: "Sellers",
  classified: "Seller kind",
  score: "Score",
  withheld: "Withheld",
  measured: "Measured",
  differenced: "Differenced",
};

const SORT_ORDER: SortKey[] = [
  "leaf",
  "band",
  "listings",
  "sellers",
  "classified",
  "score",
  "withheld",
  "measured",
  "differenced",
];

/* ------------------------------------------------------------- reading */

/** Read a niche metric.
 *
 * The exporter writes the niche counts inside `metrics`. The `NicheRow` type
 * also declares several of them at the top level, and the running export does
 * not carry them there. This helper reads the metric first and falls back to
 * the declared field, so the view works under either shape. */
function metric(row: NicheRow, field: string): number | null {
  const inMetrics = row.metrics[field];
  if (typeof inMetrics === "number") return inMetrics;
  const declared = (row as unknown as Record<string, unknown>)[field];
  return typeof declared === "number" ? declared : null;
}

function knobOf(knobs: ExportedKnob[], name: KnobName): ExportedKnob | null {
  return knobs.find((k) => k.knob === name) ?? null;
}

interface NicheView {
  row: NicheRow;
  key: string;
  leaf: string;
  band: string;
  kind: LeafKind;
  /** Listings inside the niche. Every knob denominator sits within this. */
  listings: number;
  sellers: number | null;
  business: number | null;
  privateSellers: number | null;
  /** Listings the sweep classified as business or private. Its own count. */
  classified: number | null;
  re: Rethresholded;
  /** Population knobs the pipeline measured, out of five. */
  measured: number;
  /** The smallest population-knob denominator above zero, and its coverage. */
  floorDen: number | null;
  floorCoverage: number | null;
  delta: DeltaAggregate | null;
  /** Listings inside the difference window, across the four buckets. */
  deltaTotal: number | null;
  /** Listings the pipeline differenced. Positive and negative, never unknown. */
  deltaKnown: number | null;
}

/* ------------------------------------------------------------- the view */

export function Niches({ bundle, knobState, params, setParams }: ViewProps) {
  // `products` is not destructured here. NicheDetail reads bundle.products
  // directly, and 27,206 columnar products must not be pulled into this
  // scope where a later edit could map over them on every render.
  const { niches, manifest, fees, deltas } = bundle;
  const knobNames = bundle.knobs.knob_names;

  /* --- how this view tells the two kinds apart ------------------------ */

  /* Two independent checks, run against the bundle rather than written down.
   * Check 1 asks the manifest which leaves the sweep entered. Check 2 asks
   * fees.json which leaves it could not resolve to a top-level ancestor. If a
   * later export makes them disagree, the panel says so on screen. */
  const split = useMemo(() => {
    const inScope = new Set(manifest.in_scope_leaves);
    const unresolved = new Set(fees.leaves_unresolved);

    const hub: NicheRow[] = [];
    const deepened: NicheRow[] = [];
    const disagreements: string[] = [];
    const fallbackRate = new Set<string>();
    const hubRates = new Set<number>();

    for (const row of niches.rows) {
      const leaf = row.leaf_category_id;
      const byManifest: LeafKind = inScope.has(leaf) ? "hub" : "deepened";
      const byFees: LeafKind = unresolved.has(leaf) ? "deepened" : "hub";
      if (byManifest !== byFees) disagreements.push(leaf);
      if (byManifest === "hub") {
        hub.push(row);
        const rate = fees.fvf_bp_by_leaf[leaf];
        if (typeof rate === "number") hubRates.add(rate);
      } else {
        deepened.push(row);
        if (fees.fvf_bp_by_leaf[leaf] === fees.everything_else_bp) {
          fallbackRate.add(leaf);
        }
      }
    }

    return { inScope, hub, deepened, disagreements, fallbackRate, hubRates };
  }, [niches.rows, manifest.in_scope_leaves, fees]);

  /* --- rows -------------------------------------------------------------- */

  const deltaByKey = useMemo(() => {
    const map = new Map<string, DeltaAggregate>();
    for (const a of deltas?.by_niche ?? []) map.set(a.key, a);
    return map;
  }, [deltas]);

  const rows: NicheView[] = useMemo(() => {
    return niches.rows.map((row) => {
      const re = rethresholdRow(row, knobState.knobs, knobState.defaults);
      const listings = metric(row, "n_listings") ?? 0;

      let measured = 0;
      let floorDen: number | null = null;
      let floorCoverage: number | null = null;
      for (const name of POPULATION_KNOBS) {
        const k = knobOf(re.knobs, name);
        if (!k || k.denominator <= 0) continue;
        measured += 1;
        if (floorDen === null || k.denominator < floorDen) {
          floorDen = k.denominator;
          floorCoverage = k.coverage;
        }
      }

      const business = metric(row, "n_business");
      const privateSellers = metric(row, "n_private");
      const classified =
        business === null && privateSellers === null
          ? null
          : (business ?? 0) + (privateSellers ?? 0);

      const delta = deltaByKey.get(row.key) ?? null;
      const deltaTotal = delta
        ? delta.counts.unknown +
          delta.counts.bounded_refused +
          delta.counts.known_negative +
          delta.counts.known_positive
        : null;
      // Positive and negative are both differenced observations. `unknown` is
      // not a delta and is never added to them.
      const deltaKnown = delta
        ? delta.counts.known_positive + delta.counts.known_negative
        : null;

      return {
        row,
        key: row.key,
        leaf: row.leaf_category_id,
        band: row.band,
        kind: split.inScope.has(row.leaf_category_id) ? "hub" : "deepened",
        listings,
        sellers: metric(row, "n_sellers"),
        business,
        privateSellers,
        classified,
        re,
        measured,
        floorDen,
        floorCoverage,
        delta,
        deltaTotal,
        deltaKnown,
      } satisfies NicheView;
    });
  }, [niches.rows, knobState.knobs, knobState.defaults, deltaByKey, split.inScope]);

  /* --- filters, in the URL ---------------------------------------------- */

  const kindFilter = params.get("kind") ?? "all";
  const bandFilter = params.get("band") ?? "all";
  const hasFilter = params.get("has") ?? "any";
  const query = params.get("q") ?? "";
  const thinOnly = params.get("thin") === "1";
  const sortKey = readSortKey(params.get("sort"));
  const sortDir = params.get("dir") === "asc" ? "asc" : "desc";
  const selectedKey = params.get("niche");

  const update = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === "") next.delete(key);
        else next.set(key, value);
      }
      setParams(next);
    },
    [params, setParams],
  );

  const bands = useMemo(
    () => [...new Set(niches.rows.map((r) => r.band))].sort(),
    [niches.rows],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (kindFilter !== "all" && r.kind !== kindFilter) return false;
      if (bandFilter !== "all" && r.band !== bandFilter) return false;
      if (hasFilter !== "any") {
        const k = knobOf(r.re.knobs, hasFilter as KnobName);
        if (!k || k.denominator <= 0) return false;
      }
      if (thinOnly && !isThin(r)) return false;
      if (needle && !r.leaf.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [rows, kindFilter, bandFilter, hasFilter, thinOnly, query]);

  const sorted = useMemo(() => {
    const dir = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      // A niche the pipeline could not difference has no number here. It sorts
      // last in both directions, because it is not a zero.
      if (av === null && bv === null) return a.key.localeCompare(b.key);
      if (av === null) return 1;
      if (bv === null) return -1;
      if (av === bv) return a.key.localeCompare(b.key);
      return av < bv ? -dir : dir;
    });
  }, [filtered, sortKey, sortDir]);

  /* --- which knob carries the load, across the filtered niches ---------- */

  const binding = useMemo(
    () => bindingConstraint(sorted.map((r) => r.re), knobNames),
    [sorted, knobNames],
  );

  /* --- the selected niche ------------------------------------------------ */

  const selected = useMemo(
    () => rows.find((r) => r.key === selectedKey) ?? null,
    [rows, selectedKey],
  );

  const detailRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = detailRef.current;
    if (!selectedKey || !node) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    node.scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" });
  }, [selectedKey]);

  const filtersOn =
    kindFilter !== "all" ||
    bandFilter !== "all" ||
    hasFilter !== "any" ||
    thinOnly ||
    query.trim() !== "";

  return (
    <div className="view">
      <div className="view__head">
        <h2 className="view__title">Niches</h2>
        <p className="view__lede">
          A niche is one leaf crossed with one price band. This export holds{" "}
          {count(niches.n)} of them. They are not one kind of thing, so this
          view never prints one total.
        </p>
      </div>

      <LeafKindPanel
        bundle={bundle}
        hub={split.hub}
        deepened={split.deepened}
        disagreements={split.disagreements}
        fallbackRate={split.fallbackRate}
        hubRates={split.hubRates}
      />

      <LoadPath binding={binding} rows={sorted.length} knobNames={knobNames} />

      <section className="panel" aria-labelledby="niches-ledger">
        <h3 className="panel__title" id="niches-ledger">
          Every niche, and how much of it the pipeline measured
        </h3>
        <p className="panel__note">
          The score is {"passed − failed"}. A knob the pipeline could not
          measure scores the same as a knob that cleared. Read the measured
          column before you read the score.
        </p>

        {knobState.overridden.length > 0 ? (
          <p className="niches__warn">
            You moved {count(knobState.overridden.length)} knob{" "}
            {knobState.overridden.length === 1 ? "field" : "fields"}. The board
            re-runs each comparison in the browser. It re-measures nothing.
          </p>
        ) : null}

        <Controls
          bands={bands}
          knobNames={knobNames}
          kindFilter={kindFilter}
          bandFilter={bandFilter}
          hasFilter={hasFilter}
          query={query}
          thinOnly={thinOnly}
          update={update}
        />

        <div className="niches__result">
          <Figure
            name="Niches shown"
            value={count(sorted.length)}
            denominator={sorted.length}
            population={niches.n}
          />
          <span className="den">
            {count(sorted.filter((r) => r.kind === "hub").length)} swept{" "}
            {"·"} {count(sorted.filter((r) => r.kind === "deepened").length)}{" "}
            deepened
          </span>
        </div>

        {selected ? (
          <NicheDetail
            key={selected.key}
            view={selected}
            bundle={bundle}
            binding={binding.knob}
            containerRef={detailRef}
            onClose={() => update({ niche: null })}
          />
        ) : null}

        {sorted.length === 0 ? (
          <div className="niches__empty">
            <h3>No niche matches these filters</h3>
            <p>
              The board holds {count(niches.n)} niches. The current filters
              select none of them. This is a filter result. It is not an empty
              store.
            </p>
            <button
              type="button"
              className="btn"
              onClick={() =>
                update({ kind: null, band: null, has: null, q: null, thin: null })
              }
            >
              Clear the filters
            </button>
          </div>
        ) : (
          <div className="scroll-x">
            <div className="niche-grid">
              <SortHead
                sortKey={sortKey}
                sortDir={sortDir}
                onSort={(key) =>
                  update({
                    sort: key,
                    dir:
                      key === sortKey && sortDir === "desc" ? "asc" : "desc",
                  })
                }
              />
              <ol className="fanfold niche-rows">
                {sorted.map((r) => (
                  <li key={r.key}>
                    <NicheRowLine
                      view={r}
                      selected={r.key === selectedKey}
                      onSelect={() =>
                        update({ niche: r.key === selectedKey ? null : r.key })
                      }
                    />
                  </li>
                ))}
              </ol>
            </div>
          </div>
        )}

        {filtersOn && sorted.length > 0 ? (
          <p className="panel__note">
            Filters hide {count(niches.n - sorted.length)} of{" "}
            {count(niches.n)} niches. The hidden niches still exist.
          </p>
        ) : null}
      </section>
    </div>
  );
}

/* ------------------------------------------------- the two kinds of leaf */

function LeafKindPanel({
  bundle,
  hub,
  deepened,
  disagreements,
  fallbackRate,
  hubRates,
}: {
  bundle: Bundle;
  hub: NicheRow[];
  deepened: NicheRow[];
  disagreements: string[];
  fallbackRate: Set<string>;
  hubRates: Set<number>;
}) {
  const { manifest, fees, niches } = bundle;
  const population = manifest.latest.listings;

  const hubListings = hub.reduce((t, r) => t + (metric(r, "n_listings") ?? 0), 0);
  const deepListings = deepened.reduce(
    (t, r) => t + (metric(r, "n_listings") ?? 0),
    0,
  );
  const hubMedian = medianListings(hub);
  const deepMedian = medianListings(deepened);
  const agreed = disagreements.length === 0;

  return (
    <section className="panel" aria-labelledby="niches-kinds">
      <h3 className="panel__title" id="niches-kinds">
        Two kinds of leaf id sit in this list
      </h3>
      <p className="panel__note">
        The sweep entered {count(manifest.n_in_scope_leaves)} hub leaves. A
        deepen call opened an item page, the page named a real leaf, and{" "}
        {count(deepened.length)} more leaves entered the board that way. The
        count of {count(niches.n)} therefore measures deepen coverage. It does
        not measure how wide the market is.
      </p>

      <div className="niches__split">
        <div className="niches__kind">
          <span className="niches__kind-name">
            {KIND_NAME.hub} {"·"} {count(hub.length)} leaves
          </span>
          <div className="niches__figures">
            <Figure
              name="Listings"
              value={count(hubListings)}
              denominator={hubListings}
              population={population}
            />
            <Figure
              name="Median listings"
              value={hubMedian === null ? null : count(hubMedian)}
              denominator={hub.length}
              population={niches.n}
            />
          </div>
          <p className="niches__kind-body">
            The sweep entered these leaves by name. They hold almost every
            listing on the board.
          </p>
        </div>

        <div className="niches__kind">
          <span className="niches__kind-name">
            {KIND_NAME.deepened} {"·"} {count(deepened.length)} leaves
          </span>
          <div className="niches__figures">
            <Figure
              name="Listings"
              value={count(deepListings)}
              denominator={deepListings}
              population={population}
            />
            <Figure
              name="Median listings"
              value={deepMedian === null ? null : count(deepMedian)}
              denominator={deepened.length}
              population={niches.n}
            />
          </div>
          <p className="niches__kind-body">
            One item page named each of these leaves. They exist because the
            pipeline deepened a row, not because the sweep chose them.
          </p>
        </div>
      </div>

      <Composition
        caption="Listings by kind of leaf id. The two counts stay separate."
        total={hubListings + deepListings}
        segments={[
          {
            key: "hub",
            label: `${KIND_NAME.hub} (${count(hub.length)} leaves)`,
            value: hubListings,
            color: "var(--cov-4)",
            note: "The sweep entered these leaves. Read them as departments, not as markets.",
          },
          {
            key: "deepened",
            label: `${KIND_NAME.deepened} (${count(deepened.length)} leaves)`,
            value: deepListings,
            color: "var(--cov-1)",
            note: "One item page named each leaf. A leaf here often holds one listing.",
          },
        ]}
      />

      <details className="niches__method">
        <summary>How this view separates the two kinds</summary>
        <ul className="niches__checks">
          <li className="niches__check">
            <b>Check 1. The manifest.</b> The manifest lists{" "}
            {count(manifest.n_in_scope_leaves)} leaves in scope. This view marks
            a niche leaf that is absent from that list as a{" "}
            {KIND_NAME.deepened.toLowerCase()}.
            <span className="niches__check-result">
              {count(deepened.length)} of {count(bundle.niches.n)} leaves are
              absent
            </span>
          </li>
          <li className="niches__check">
            <b>Check 2. The fee table.</b> fees.json marks{" "}
            {count(fees.n_leaves_unresolved)} of {count(fees.n_leaves)} leaves
            as unresolved. Python could not trace them to a top-level ancestor.
            <span
              className={`niches__check-result${
                agreed ? "" : " niches__check-result--broken"
              }`}
            >
              {agreed
                ? `Both checks name the same ${count(deepened.length)} leaves. Disagreements: 0`
                : `The two checks disagree on ${count(disagreements.length)} leaves`}
            </span>
          </li>
          <li className="niches__check">
            <b>Check 3. The fee rate.</b> A leaf with no ancestor takes the
            fallback rate {bpPercent(fees.everything_else_bp)}.
            <span className="niches__check-result">
              {count(fallbackRate.size)} of {count(deepened.length)} deepened
              leaves take the fallback rate. The {count(hub.length)} swept hubs
              carry {count(hubRates.size)} separate rates
            </span>
          </li>
          <li className="niches__check">
            <b>Where the leaf comes from.</b> Every row on this board reads its
            leaf from the <code>leaf_category_id</code> field. The niche key
            also contains a leaf id. This view never reads it from the key.
          </li>
        </ul>
      </details>

      {!agreed ? (
        <p className="niches__warn">
          The two checks disagree on {count(disagreements.length)} leaves. Treat
          the split below as unverified until somebody reads the export.
        </p>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------- the load path */

function LoadPath({
  binding,
  rows,
  knobNames,
}: {
  binding: ReturnType<typeof bindingConstraint>;
  rows: number;
  knobNames: KnobName[];
}) {
  if (rows === 0) return null;

  const failed = knobNames
    .map((name) => ({
      label: knobTitle(name),
      value: binding.failed[name],
      denominator: rows,
      population: rows,
      flagged: name === binding.knob,
    }))
    .sort((a, b) => b.value - a.value);

  const withheld = knobNames
    .map((name) => ({
      label: knobTitle(name),
      value: binding.withheld[name],
      denominator: rows,
      population: rows,
    }))
    .sort((a, b) => b.value - a.value);

  return (
    <section className="panel" aria-labelledby="niches-load">
      <h3 className="panel__title" id="niches-load">
        Which knob carries the load
      </h3>
      <p className="panel__note">
        A knob that fails a niche removes it. A knob that withholds a niche
        never judged it. The two counts stay apart. Loosening a knob that only
        withholds changes nothing.
      </p>

      <div className="niches__load">
        <div className="niches__load--failed">
          <Bars
            caption={
              binding.knob
                ? `Niches each knob fails. ${knobTitle(binding.knob)} eliminates the most.`
                : "Niches each knob fails. No knob fails a niche at this setting."
            }
            data={failed}
            max={rows}
            asShare={false}
          />
        </div>
        <div className="niches__load--withheld">
          <Bars
            caption="Niches each knob withholds. The pipeline had nothing to compare."
            data={withheld}
            max={rows}
            asShare={false}
          />
        </div>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------- controls */

function Controls({
  bands,
  knobNames,
  kindFilter,
  bandFilter,
  hasFilter,
  query,
  thinOnly,
  update,
}: {
  bands: string[];
  knobNames: KnobName[];
  kindFilter: string;
  bandFilter: string;
  hasFilter: string;
  query: string;
  thinOnly: boolean;
  update: (patch: Record<string, string | null>) => void;
}) {
  const oneBand = bands.length < 2;

  return (
    <div className="niches__controls">
      <label className="niches__control">
        <span>Kind of leaf</span>
        <select
          value={kindFilter}
          onChange={(e) => update({ kind: e.target.value === "all" ? null : e.target.value })}
        >
          <option value="all">Both kinds</option>
          <option value="hub">{KIND_NAME.hub} only</option>
          <option value="deepened">{KIND_NAME.deepened} only</option>
        </select>
      </label>

      <label className="niches__control">
        <span>Price band</span>
        <select
          value={bandFilter}
          disabled={oneBand}
          onChange={(e) => update({ band: e.target.value === "all" ? null : e.target.value })}
        >
          <option value="all">
            {oneBand && bands[0]
              ? `${bandLabel(bands[0])} — the only band held`
              : "Every band"}
          </option>
          {bands.map((b) => (
            <option key={b} value={b}>
              {bandLabel(b)}
            </option>
          ))}
        </select>
      </label>

      <label className="niches__control">
        <span>Has a measured knob</span>
        <select
          value={hasFilter}
          onChange={(e) => update({ has: e.target.value === "any" ? null : e.target.value })}
        >
          <option value="any">Any knob, measured or not</option>
          {knobNames.map((k) => (
            <option key={k} value={k}>
              {knobTitle(k)} was measured
            </option>
          ))}
        </select>
      </label>

      <label className="niches__control">
        <span>Leaf id</span>
        <input
          type="search"
          inputMode="numeric"
          value={query}
          placeholder="Type part of a leaf id"
          onChange={(e) => update({ q: e.target.value })}
        />
      </label>

      <label className="niches__toggle">
        <input
          type="checkbox"
          checked={thinOnly}
          onChange={(e) => update({ thin: e.target.checked ? "1" : null })}
        />
        <span>Show only niches that score 4 or more on 2 listings or fewer</span>
      </label>
    </div>
  );
}

/* ---------------------------------------------------------- the ledger */

function SortHead({
  sortKey,
  sortDir,
  onSort,
}: {
  sortKey: SortKey;
  sortDir: "asc" | "desc";
  onSort: (key: SortKey) => void;
}) {
  return (
    <div className="niche-head">
      {SORT_ORDER.map((key) => {
        const active = key === sortKey;
        return (
          <button
            key={key}
            type="button"
            className="niche-head__btn"
            data-active={active ? "yes" : "no"}
            aria-label={
              active
                ? `Sorted by ${SORT_LABELS[key]}, ${sortDir === "asc" ? "smallest first" : "largest first"}. Select to reverse.`
                : `Sort by ${SORT_LABELS[key]}`
            }
            onClick={() => onSort(key)}
          >
            {SORT_LABELS[key]}
            {active ? (
              <span className="niche-head__dir" aria-hidden="true">
                {sortDir === "asc" ? " ↑" : " ↓"}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function NicheRowLine({
  view,
  selected,
  onSelect,
}: {
  view: NicheView;
  selected: boolean;
  onSelect: () => void;
}) {
  const { re, listings } = view;
  const grade = gradeEvidence({ unknown: re.unknown, members: listings });

  return (
    <button
      type="button"
      className="niche-row"
      aria-expanded={selected}
      onClick={onSelect}
    >
      <span className="niche-cell niche-cell--leaf">
        <span className="niche-cell__name">Leaf</span>
        <span className="niche-cell__value">{leafLabel(view.leaf)}</span>
        <span
          className={`niche-tag${view.kind === "deepened" ? " niche-tag--deepened" : ""}`}
        >
          {KIND_NAME[view.kind]}
        </span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Band</span>
        <span className="niche-cell__value niche-cell__value--quiet">
          {bandLabel(view.band)}
        </span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Listings</span>
        <span className="niche-cell__value">{count(listings)}</span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Sellers</span>
        <span className="niche-cell__value">
          {view.sellers === null ? "not counted" : count(view.sellers)}
        </span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Seller kind</span>
        {view.classified && view.classified > 0 ? (
          <>
            <span className="niche-cell__value">
              {count(view.business)} business {"·"}{" "}
              {count(view.privateSellers)} private
            </span>
            <span className="den">
              n={count(view.classified)} of {count(listings)}
            </span>
          </>
        ) : (
          <>
            <Stamp>withheld</Stamp>
            <span className="den den--unknown">no observations</span>
          </>
        )}
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Score</span>
        <span className="niche-cell__value">{re.score} of 6</span>
        <span className="den">
          {re.passed} clear {"·"} {re.failed} fail
        </span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Withheld</span>
        <span
          className={`niche-cell__value${re.unknown ? " niche-cell__value--withheld" : ""}`}
        >
          {re.unknown} of 6
        </span>
        <span className="den">{grade}</span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Measured</span>
        <CoverageMeter view={view} />
        <span className="niche-cell__value niche-cell__value--quiet">
          {view.measured} of 5 knobs
        </span>
        <span
          className={`den${view.floorDen === null ? " den--unknown" : ""}`}
        >
          {view.floorDen === null
            ? "no observations"
            : `thinnest n=${count(view.floorDen)} of ${count(listings)} · ${
                coveragePercent(
                  view.floorCoverage ??
                    (listings ? view.floorDen / listings : 0),
                  2,
                ) ?? ""
              }`}
        </span>
      </span>

      <span className="niche-cell">
        <span className="niche-cell__name">Differenced</span>
        {view.delta && view.deltaKnown !== null && view.deltaTotal !== null ? (
          <>
            <span className="niche-cell__value">{count(view.deltaKnown)}</span>
            <span className="den">
              n={count(view.deltaKnown)} of {count(view.deltaTotal)}
            </span>
          </>
        ) : (
          <>
            <Stamp>not differenced</Stamp>
            <span className="den den--unknown">no delta to report</span>
          </>
        )}
      </span>

      <VerdictStrip knobs={re.knobs} />

      {isThin(view) ? (
        <span className="niche-story">
          This niche scores {re.score} of 6. The thinnest knob denominator is{" "}
          {count(view.floorDen ?? 0)} of {count(listings)} listings. The score
          rests on that one reading.
        </span>
      ) : null}
    </button>
  );
}

function VerdictStrip({ knobs }: { knobs: ExportedKnob[] }) {
  return (
    <span className="niche-verdicts">
      {knobs.map((k) => {
        const verdict = verdictOf(k.passed);
        // Two different withholds, and they need two different words. A
        // denominator of 0 means the pipeline measured nothing, and that is
        // WITHHELD, the word this board uses for it everywhere else. A
        // denominator above 0 with a null verdict is the rarer case: the
        // pipeline measured a figure and then refused to compare it, so that
        // one takes the word that says so. This strip prints no denominators,
        // so the word is the only thing that can tell the two apart. Both read
        // "withheld" once, which left the distinction resting on a box outline.
        const nothing = k.denominator === 0;
        return (
          <span
            key={k.knob}
            className={`verdict verdict--${verdict}${nothing ? " verdict--none" : ""}`}
            title={k.note || undefined}
          >
            <span className="verdict__name">{knobTitle(k.knob)}</span>
            {nothing ? (
              <Stamp>withheld</Stamp>
            ) : (
              <span className="verdict__word">
                {verdict === "withheld" ? "Not compared" : verdictLabel(verdict)}
              </span>
            )}
          </span>
        );
      })}
    </span>
  );
}

function CoverageMeter({ view }: { view: NicheView }) {
  return (
    <span
      className="niche-meter"
      role="img"
      aria-label={POPULATION_KNOBS.map((name) => {
        const k = knobOf(view.re.knobs, name);
        if (!k || k.denominator === 0) {
          return `${knobTitle(name)} withheld, no observations`;
        }
        return `${knobTitle(name)} measured over ${k.denominator} of ${view.listings} listings`;
      }).join(". ")}
    >
      {POPULATION_KNOBS.map((name) => {
        const k = knobOf(view.re.knobs, name);
        const empty = !k || k.denominator === 0;
        // A cell above zero is drawn at 14% minimum height. A coverage of
        // 0.14% would otherwise be invisible and read as "not measured". The
        // exact figures sit beside the meter.
        const raw = k && view.listings ? k.denominator / view.listings : 0;
        const height = empty ? 0 : Math.max(14, Math.min(100, raw * 100));
        return (
          <span
            key={name}
            className={`niche-meter__cell${empty ? " niche-meter__cell--withheld" : ""}`}
            style={{ ["--fill" as string]: `${height.toFixed(1)}%` }}
          />
        );
      })}
    </span>
  );
}

/* --------------------------------------------------------------- detail */

function NicheDetail({
  view,
  bundle,
  binding,
  containerRef,
  onClose,
}: {
  view: NicheView;
  bundle: Bundle;
  binding: KnobName | null;
  containerRef: React.RefObject<HTMLDivElement | null>;
  onClose: () => void;
}) {
  const { row, re, listings } = view;
  const [shown, setShown] = useState(8);

  const productIndexes = useMemo(() => {
    const p = bundle.products;
    const column = p.columns["leaf_category_id"] ?? [];
    const scores = p.columns["score"] ?? [];
    const sizes = p.columns["n_listings"] ?? [];
    const hits: number[] = [];
    for (let i = 0; i < column.length; i += 1) {
      // The leaf comes from the column, decoded through the dictionary. The
      // product key also contains a leaf id and is never parsed here.
      if (dictValue(p, "leaf_category_id", column[i]) === view.leaf) hits.push(i);
    }
    hits.sort((a, b) => {
      const sa = numberAt(scores, a);
      const sb = numberAt(scores, b);
      if (sa !== sb) return sb - sa;
      return numberAt(sizes, b) - numberAt(sizes, a);
    });
    return hits;
  }, [bundle.products, view.leaf]);

  const visible: ProductRow[] = useMemo(
    () =>
      productIndexes
        .slice(0, shown)
        .map((i) => decodeProduct(bundle.products, i)),
    [productIndexes, shown, bundle.products],
  );

  const constants = useMemo(() => constantMetrics(bundle), [bundle]);

  return (
    <section className="niche-detail" ref={containerRef} aria-live="polite">
      <div className="niche-detail__head">
        <h3 className="niche-detail__title">
          {leafLabel(view.leaf)} {"·"} {bandLabel(view.band)}
        </h3>
        <span className="niche-tag">{KIND_NAME[view.kind]}</span>
        <button
          type="button"
          className="btn btn--quiet niche-detail__close"
          onClick={onClose}
        >
          Close
        </button>
      </div>

      <p className="panel__note">
        The pipeline names this niche <code>{row.key}</code>. The key contains
        the leaf id. Every figure below reads the leaf from the{" "}
        <code>leaf_category_id</code> field instead.
      </p>

      <Evidence
        unknown={re.unknown}
        passed={re.passed}
        failed={re.failed}
        members={listings}
        sellers={view.sellers ?? 0}
      />

      {re.marginStale ? (
        <p className="niches__warn">
          You moved a fee or margin field. The margin figure is a function of
          those fields, and the browser holds no price for this niche. The
          margin knob stays withheld until the export runs again.
        </p>
      ) : null}

      <h4 className="panel__title">The six knobs</h4>
      <div className="niche-detail__knobs">
        {re.knobs.map((k) => (
          <KnobField
            key={k.knob}
            knob={k}
            population={listings}
            binding={k.knob === binding}
          />
        ))}
      </div>

      <h4 className="panel__title">The readings behind the knobs</h4>
      <p className="panel__note">
        Each figure carries the count the pipeline measured it over. A
        denominator of 0 reads as withheld, never as 0.
      </p>
      <div className="niche-detail__metrics">
        <MetricFigures view={view} />
      </div>

      {constants.length > 0 ? (
        <p className="panel__note">
          {count(constants.length)} metric fields hold one value across all{" "}
          {count(bundle.niches.n)} niches, so they separate nothing here:{" "}
          {constants.map((c) => `${c.field} = ${c.value}`).join(", ")}.
        </p>
      ) : null}

      <DeltaPanel view={view} bundle={bundle} />

      <h4 className="panel__title">
        Products inside {leafLabel(view.leaf)}
      </h4>
      <p className="panel__note">
        A product row carries a leaf. It carries no price band. This list
        therefore matches on the leaf alone, and it can hold products outside{" "}
        {bandLabel(view.band)}.
      </p>

      {productIndexes.length === 0 ? (
        <div className="niches__empty">
          <h3>No product sits in this leaf</h3>
          <p>
            The product board holds {count(bundle.products.n)} rows across{" "}
            {count(bundle.products.n_leaves)} leaves. None of them carries this
            leaf. This is an absence, not a count of zero sales.
          </p>
        </div>
      ) : (
        <>
          <ul className="niche-products">
            {visible.map((p) => (
              <ProductLine
                key={p.key}
                product={p}
                knobs={bundle.knobs}
                view={view}
              />
            ))}
          </ul>
          <div className="niche-more">
            <span className="den">
              Showing {count(visible.length)} of{" "}
              {count(productIndexes.length)} products, ranked by score
            </span>
            {shown < productIndexes.length ? (
              <button
                type="button"
                className="btn btn--quiet"
                onClick={() => setShown((n) => n + 24)}
              >
                Show 24 more
              </button>
            ) : null}
          </div>
        </>
      )}
    </section>
  );
}

function MetricFigures({ view }: { view: NicheView }) {
  const { row, listings } = view;
  const m = (field: string) => metric(row, field);
  const den = (field: string) => m(field) ?? 0;

  const fbN = m("feedback_median_n");
  const windowDays = m("str_window_days");

  return (
    <>
      <Figure
        name="Median landed price"
        value={money(m("price_median_pence"))}
        denominator={den("price_median_denominator")}
        population={listings}
        note="The sweep read no landed price on a business listing here."
      />
      <Figure
        name="Price floor"
        value={money(m("price_floor_pence"))}
        denominator={den("price_floor_denominator")}
        population={listings}
        note="The sweep read no landed price on a business listing here."
      />
      <Figure
        name="Max cost at median"
        value={money(row.max_cost_at_median_pence)}
        denominator={den("price_median_denominator")}
        population={listings}
        against="Above £0 clears the margin knob"
        note="The pipeline needs a median price to invert the margin."
      />
      <Figure
        name="Max cost at floor"
        value={money(row.max_cost_at_floor_pence)}
        denominator={den("price_floor_denominator")}
        population={listings}
        against="Above £0 clears the margin knob"
        note="The pipeline needs a floor price to invert the margin."
      />
      <Figure
        name="Delivery estimate"
        value={formatDays(m("eta_median_days"))}
        denominator={den("eta_median_denominator")}
        population={listings}
        note="The sweep read no delivery estimate on a business listing here."
      />
      <Figure
        name="Rival feedback"
        value={bpPercent(m("feedback_median_bp"))}
        denominator={den("feedback_median_denominator")}
        population={listings}
        against={fbN === null ? undefined : `on ${count(fbN)} ratings`}
        note="The sweep read no seller feedback here."
      />
      <Figure
        name="Sell-through, window"
        value={bpPercent(m("str_window_bp"), 0)}
        denominator={den("str_window_denominator")}
        population={listings}
        against={
          windowDays === null
            ? "Window length unknown"
            : `${windowDays.toFixed(2)}-day window against a 30-day bar`
        }
        note="Two snapshots are needed before a window exists."
      />
      <Figure
        name="Sell-through, lifetime"
        value={formatRatio(m("str_lifetime"))}
        denominator={den("str_lifetime_denominator")}
        population={listings}
        against="Not comparable to the window bar"
        note="The sweep read no lifetime sold count here."
      />
      <Figure
        name="Listings with a sold count"
        value={count(m("sold_known"))}
        denominator={den("sold_known")}
        population={listings}
        coverage={m("coverage") ?? undefined}
        note="eBay showed no sold count on any listing in this niche."
      />
      <Figure
        name="Distinct sellers"
        value={count(m("n_sellers"))}
        denominator={den("n_sellers")}
        population={listings}
        note="The sweep recorded no seller for this niche."
      />
      <Figure
        name="Business listings"
        value={count(m("n_business"))}
        denominator={view.classified ?? 0}
        population={listings}
        note="The sweep classified no listing here as business or private."
      />
      <Figure
        name="Private listings"
        value={count(m("n_private"))}
        denominator={view.classified ?? 0}
        population={listings}
        note="The sweep classified no listing here as business or private."
      />
      <Figure
        name="Listed in the last 7 days"
        value={count(m("new_7d"))}
        denominator={den("new_7d_denominator")}
        population={listings}
        note="The sweep read no listing date here."
      />
      <Figure
        name="New-account share"
        value={coveragePercent(m("new_account_share"))}
        denominator={den("new_account_denominator")}
        population={listings}
        note="The sweep read no seller account age here."
      />
      <Figure
        name="Velocity, window"
        value={formatRatio(m("velocity_win"))}
        denominator={den("velocity_win_denominator")}
        population={listings}
        note="Two snapshots are needed before a window exists."
      />
      <Figure
        name="Velocity, day 1"
        value={formatRatio(m("velocity_day1"))}
        denominator={den("velocity_day1_denominator")}
        population={listings}
        note="The sweep read no first-day sales here."
      />
    </>
  );
}

function DeltaPanel({ view, bundle }: { view: NicheView; bundle: Bundle }) {
  const { deltas } = bundle;

  if (!deltas) {
    return (
      <>
        <h4 className="panel__title">Day over day</h4>
        <div className="niches__empty">
          <h3>Nothing can be differenced yet</h3>
          <p>
            A delta needs two snapshots. The store holds{" "}
            {count(bundle.manifest.n_dates)}. This is a locked state, not a
            change of zero.
          </p>
        </div>
      </>
    );
  }

  if (!view.delta || view.deltaTotal === null) {
    return (
      <>
        <h4 className="panel__title">Day over day</h4>
        <div className="niches__empty">
          <h3>This niche was not differenced</h3>
          <p>{deltas.aggregates_omitted.reason}</p>
          <p>
            The pipeline dropped this niche from the delta board on{" "}
            {isoDay(deltas.date_a)} to {isoDay(deltas.date_b)}. Read it as
            absent, never as a delta of 0.
          </p>
        </div>
      </>
    );
  }

  const counts = view.delta.counts;

  return (
    <>
      <h4 className="panel__title">Day over day</h4>
      <p className="panel__note">
        {isoDay(deltas.date_a)} to {isoDay(deltas.date_b)}, a{" "}
        {count(deltas.window_days)}-day window. The four buckets are disjoint.
        This board never adds the unknown bucket to the other three.
      </p>

      <Composition
        caption="Listings in this niche, by what the sweep could difference."
        total={view.deltaTotal}
        segments={DELTA_SEGMENTS.map((s) => ({
          key: s.key,
          label: s.label,
          value: counts[s.key],
          color: s.color,
          note: s.note,
        }))}
      />

      <div className="niche-detail__metrics">
        <Figure
          name="Sold count rose by"
          value={count(view.delta.sum_delta_known_positive)}
          denominator={counts.known_positive}
          population={view.deltaTotal}
          note="No listing in this niche showed a rise."
        />
        <Figure
          name="Sold count fell by"
          value={count(view.delta.sum_delta_known_negative)}
          denominator={counts.known_negative}
          population={view.deltaTotal}
          note="No listing in this niche showed a fall."
        />
      </div>

      <p className="panel__note">{deltas.note}</p>
    </>
  );
}

function ProductLine({
  product,
  knobs,
  view,
}: {
  product: ProductRow;
  knobs: Bundle["knobs"];
  view: NicheView;
}) {
  void knobs;
  const re = rethresholdRow(product, view.re.knobs.length ? knobsOf(view) : knobsOf(view), knobsOf(view));
  return (
    <li className="niche-product">
      <p className="niche-product__title">{product.label}</p>
      <div className="niche-product__meta">
        <span>{product.key}</span>
        <span>score {re.score} of 6</span>
        <span>
          {re.passed} clear {"·"} {re.failed} fail
        </span>
        <span className={re.unknown ? "niche-product__withheld" : undefined}>
          {re.unknown} withheld
        </span>
        <span>
          {product.n_members === 1
            ? "one listing, one seller"
            : `${count(product.n_listings)} listings · ${count(product.n_sellers)} sellers`}
        </span>
        <span>{product.identity_tier}</span>
      </div>
    </li>
  );
}

/* ---------------------------------------------------------------- helpers */

function knobsOf(_view: NicheView): never {
  throw new Error("unused");
}

function readSortKey(raw: string | null): SortKey {
  return SORT_ORDER.includes(raw as SortKey) ? (raw as SortKey) : "listings";
}

function sortValue(v: NicheView, key: SortKey): number | string | null {
  switch (key) {
    case "leaf": {
      const n = Number(v.leaf);
      return Number.isFinite(n) ? n : v.leaf;
    }
    case "band":
      return v.band;
    case "listings":
      return v.listings;
    case "sellers":
      return v.sellers;
    case "classified":
      return v.classified;
    case "score":
      return v.re.score;
    case "withheld":
      return v.re.unknown;
    case "measured":
      // Rank by how many knobs were measured, then by the thinnest reading.
      return v.measured * 1_000_000 + (v.floorDen ?? 0);
    case "differenced":
      return v.deltaKnown;
    default:
      return null;
  }
}

/** A niche that scores well on almost nothing. This is the finding the view
 * exists to deliver, so it is a filter and a per-row line. */
function isThin(v: NicheView): boolean {
  return v.re.score >= 4 && v.floorDen !== null && v.floorDen <= 2;
}

function medianListings(rows: NicheRow[]): number | null {
  if (rows.length === 0) return null;
  const values = rows
    .map((r) => metric(r, "n_listings") ?? 0)
    .sort((a, b) => a - b);
  const mid = Math.floor(values.length / 2);
  if (values.length % 2 === 1) return values[mid] ?? null;
  const lower = values[mid - 1];
  const upper = values[mid];
  if (lower === undefined || upper === undefined) return null;
  return Math.round((lower + upper) / 2);
}

function numberAt(
  column: (number | string | null)[],
  index: number,
): number {
  const raw = column[index];
  return typeof raw === "number" ? raw : 0;
}

function formatDays(value: number | null): string | null {
  if (value === null) return null;
  return `${value} ${value === 1 ? "day" : "days"}`;
}

/** A ratio the exporter ships without a stated unit. It renders as the figure
 * the pipeline holds, never converted into a percentage this view invented. */
function formatRatio(value: number | null): string | null {
  if (value === null) return null;
  if (Number.isInteger(value)) return count(value);
  return value.toFixed(2);
}

/** Metric fields that hold one value across every niche. They separate no two
 * niches, so the detail panel names them instead of drawing them. */
function constantMetrics(bundle: Bundle): { field: string; value: number }[] {
  const seen = new Map<string, Set<number>>();
  for (const row of bundle.niches.rows) {
    for (const [field, value] of Object.entries(row.metrics)) {
      if (value === null || field.endsWith("_denominator")) continue;
      let bucket = seen.get(field);
      if (!bucket) {
        bucket = new Set<number>();
        seen.set(field, bucket);
      }
      bucket.add(value);
    }
  }
  const out: { field: string; value: number }[] = [];
  for (const [field, values] of seen) {
    if (values.size !== 1) continue;
    const only = [...values][0];
    if (only === undefined) continue;
    out.push({ field, value: only });
  }
  return out;
}
