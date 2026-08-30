/* Coverage and gaps — the honesty console.
 *
 * The rest of the board ranks things. This page states what the sweep could
 * not measure, at the same size as what it could.
 *
 * One finding governs the layout. On 2026-08-29 the sweep read a seller id on
 * 0.95% of toys_games listings. It read one on 97.66% or more of every other
 * department, on the same night, from the same code. A single global figure
 * of 86.9% hides that completely. So the per-department array opens the page
 * and the global figure sits under it, marked as the mean that hides the
 * outage.
 *
 * Nothing here is computed in the browser. Every number is the serialised
 * return value of a tested panel in phoneloop.population. This view selects,
 * orders and labels those numbers. It never re-derives one. Where a sentence
 * belongs to the pipeline, this view quotes it and names the source instead
 * of paraphrasing it.
 */

import { useMemo, type ReactNode } from "react";

import { Bars, Composition, Histogram, Locked, type BarDatum } from "../components/charts";
import { Figure } from "../components/Figure";
import { Stamp } from "../components/Stamp";
import {
  count,
  coveragePercent,
  denominatorLine,
  isoDay,
  knobSecondary,
  knobThreshold,
  knobTitle,
  knobValue,
  verdictLabel,
} from "../lib/format";
import type {
  ColumnLadderEntry,
  CoveragePanel,
  CoveragePayload,
  DepartmentCoverage,
} from "../lib/types";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/coverage.css";

/* ------------------------------------------------------- payload narrowing */

/* The exporter types two call-outs as `unknown`, because it treats a panel
 * payload as opaque. This view narrows them once, here, and checks the shape
 * at run time. A shape change then prints a named unavailable state. It does
 * not throw a blank page at the operator. */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

interface PanelKnob {
  knob: string;
  value: number | (number | null)[];
  threshold: number | number[];
  passed: boolean | null;
  denominator: number;
  coverage: number;
  note: string;
}

interface FlaggedNiche {
  leaf: string;
  niche: string;
  n: number;
  rank: number;
  score: number;
  seller_cov: number;
  seller_known: number;
  price_median_denominator: number;
  passing: string[];
  saturation: PanelKnob;
  seller_quality: PanelKnob;
}

interface GreenBecauseBroken {
  critique: string;
  flagged: FlaggedNiche[];
  n_niches: number;
  n_scoring_above_zero: number;
  threshold: number;
}

interface StoredConstant {
  col: string;
  kind: string;
  non_null: number;
  value: string;
}

interface Contradiction {
  name: string;
  contract: string;
  observed: Record<string, unknown>;
  reading: string;
}

interface ConstantsAndContradictions {
  constants: StoredConstant[];
  contradictions: Contradiction[];
  n: number;
  never_populated: { col: string; non_null: number }[];
}

function readGreenBecauseBroken(v: unknown): GreenBecauseBroken | null {
  if (!isRecord(v)) return null;
  if (typeof v.critique !== "string") return null;
  if (!Array.isArray(v.flagged)) return null;
  if (typeof v.threshold !== "number") return null;
  return v as unknown as GreenBecauseBroken;
}

function readConstants(v: unknown): ConstantsAndContradictions | null {
  if (!isRecord(v)) return null;
  if (!Array.isArray(v.constants)) return null;
  if (!Array.isArray(v.contradictions)) return null;
  return v as unknown as ConstantsAndContradictions;
}

/* ------------------------------------------------------------- vocabulary */

/* One name for one thing, across the whole view.
 *   listing    — one stored row of the latest sweep
 *   department — the payload's `category` column
 *   niche      — one leaf and one price band
 *   withheld   — the sweep measured nothing, so no figure exists */

type CovKey = Exclude<keyof DepartmentCoverage, "category" | "n">;

/* The order is the order the category_integrity panel lists in `cov_cols`. */
const DIMENSIONS: { key: CovKey; label: string; asks: string }[] = [
  {
    key: "seller_cov",
    label: "seller id",
    asks: "The listing names the seller. Five of the six knobs need this.",
  },
  {
    key: "feedback_cov",
    label: "seller feedback",
    asks: "The listing carries a feedback percentage and a rating count.",
  },
  {
    key: "sold_cov",
    label: "sold badge",
    asks: "The listing shows a lifetime sold count. The badge is rounded.",
  },
  {
    key: "eta_cov",
    label: "delivery estimate",
    asks: "The listing states a delivery date to this machine's postcode.",
  },
  {
    key: "condition_cov",
    label: "condition",
    asks: "The listing states a condition. Almost no department reports one.",
  },
  {
    key: "landed_cov",
    label: "landed price",
    asks: "The listing gives an item price and a postage price.",
  },
  {
    key: "origin_cov",
    label: "origin country",
    asks: "The listing states the country the item ships from.",
  },
];

const TIER_GROUPS: {
  tier: ColumnLadderEntry["tier"];
  head: string;
  note: string;
  modifier: string;
}[] = [
  {
    tier: "tier1_search_row",
    head: "Tier 1 — read off the search row",
    note: "The sweep reads these columns from the results page. It pays one page fetch for a whole page of listings.",
    modifier: "",
  },
  {
    tier: "tier2_deepen",
    head: "Tier 2 — read only on an item page",
    note: "The deepen pass must open one item page per listing to fill these. It reached 218 listings, so no tier-2 column can exceed 218.",
    modifier: "cov-group--tier2",
  },
  {
    tier: "derived",
    head: "Derived by the exporter",
    note: "The pipeline writes these columns itself. They are full because the pipeline fills them, not because eBay supplied them.",
    modifier: "",
  },
  {
    tier: "dead",
    head: "Dead",
    note: "The store defines the column. No listing has ever carried a value in it.",
    modifier: "cov-group--dead",
  },
];

/* --------------------------------------------------------------- helpers */

/* A key name matches this when its value is a fraction of a whole. Every
 * other float stays a raw number. An HHI index of 0.0998 is not 9.98% and
 * this view must not print it as one. */
const SHARE_KEY =
  /(^|_)(cov|coverage|fill|gate|rate|share|pct)$|^share_|_share$|^below_floor$/;

function fmtShare(fraction: number): string {
  return coveragePercent(fraction, fraction < 0.1 ? 2 : 1) ?? "";
}

/* Non-integers print to four decimal places, and the title holds the exact
 * value. Rounding the display is not the same as rounding the figure. */
function fmtNumber(key: string, value: number): { text: string; exact: string } {
  const exact = String(value);
  if (Number.isInteger(value)) return { text: count(value) ?? exact, exact };
  if (SHARE_KEY.test(key)) return { text: fmtShare(value), exact };
  return {
    text: value.toLocaleString("en-GB", { maximumFractionDigits: 4 }),
    exact,
  };
}

function keyLabel(key: string): string {
  return key.replace(/_/g, " ");
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

function fmtCell(key: string, value: unknown): ReactNode {
  if (value === null || value === undefined) return <Missing>not recorded</Missing>;
  if (typeof value === "number") {
    const f = fmtNumber(key, value);
    return <span title={f.exact}>{f.text}</span>;
  }
  if (typeof value === "boolean") return String(value);
  if (typeof value === "string") return ISO_DAY.test(value) ? isoDay(value) : value;
  if (Array.isArray(value)) {
    if (value.length === 0) return <Missing>no entries</Missing>;
    if (value.every((v) => typeof v === "number" || typeof v === "string")) {
      return value.join(", ");
    }
    return `${count(value.length)} entries`;
  }
  if (isRecord(value)) return `${count(Object.keys(value).length)} entries`;
  return String(value);
}

/* ------------------------------------------------------- shared elements */

/* An absence is a finding. It is never a blank space and never a dash. */
function Missing({ children }: { children: ReactNode }) {
  return <span className="cov-empty">{children}</span>;
}

/* A ruled field. A withheld reading takes the whole field, the way VOID takes
 * a cancelled coupon. */
function Ruled({
  known,
  className = "",
  children,
}: {
  known: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`field ${known ? "" : "field--withheld"} ${className}`}>
      {children}
    </div>
  );
}

/* The pipeline's own words, with the source named. This view quotes a payload
 * string and never rewrites it. */
function Quote({
  source,
  children,
  reading = false,
}: {
  source: string;
  children: ReactNode;
  reading?: boolean;
}) {
  return (
    <p className={`cov-quote ${reading ? "cov-quote--reading" : ""}`}>
      <span className="cov-quote__source">{source}</span>
      {children}
    </p>
  );
}

function PanelState({ panel }: { panel: CoveragePanel | undefined }) {
  if (!panel) {
    return (
      <p className="cov-say cov-say--stamped">
        <Stamp>panel absent</Stamp> The export shipped no panel with this id.
      </p>
    );
  }
  if (panel.state === "live") return null;
  return (
    <div className="cov-panel__state">
      <Stamp tone={panel.state === "error" ? "fail" : "withheld"}>
        {panel.state === "error" ? "panel failed" : "unavailable"}
      </Stamp>
      <p className="cov-say cov-say--stamped">
        {panel.note ?? "The exporter recorded no reason for this state."}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------ 1. the outage */

function Outage({
  cov,
  panel,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
}) {
  const worst = cov.call_outs.worst_department;
  const rows = cov.call_outs.per_department_seller_coverage;

  /* The exporter names the worst department itself. This view does not pick
   * one, so a day with no outage prints no outage. */
  if (!worst || rows.length === 0) {
    return (
      <section className="panel cov-lead">
        <h3 className="panel__title">Seller coverage by department</h3>
        <PanelState panel={panel} />
        <p className="cov-say cov-say--stamped">
          <Stamp>withheld</Stamp> The export named no worst department for{" "}
          {isoDay(cov.latest_date)}.
        </p>
      </section>
    );
  }

  const peers = rows.filter((r) => r.category !== worst.category);
  const first = peers[0];
  const lowestPeer = first
    ? peers.reduce((a, b) => (b.seller_cov < a.seller_cov ? b : a), first)
    : null;
  const highestPeer = first
    ? peers.reduce((a, b) => (b.seller_cov > a.seller_cov ? b : a), first)
    : null;

  /* Worst first. The order carries the finding, so the chart never re-sorts
   * by size or by name. */
  const bars: BarDatum[] = [...rows]
    .sort((a, b) => a.seller_cov - b.seller_cov)
    .map((r) => ({
      label: r.category,
      value: r.seller_cov,
      denominator: r.n,
      population: cov.n_listings,
      flagged: r.category === worst.category,
      note:
        r.category === worst.category
          ? "This department fails the 0.90 density gate. The competition density panel excludes all five of its niches."
          : undefined,
    }));

  const sellerColumn = cov.columns.find((c) => c.col === "seller_id");

  return (
    <section className="panel cov-lead">
      <h3 className="panel__title">One department reports almost no sellers</h3>
      <PanelState panel={panel} />

      <Ruled known className="cov-lead__figure">
        <Figure
          scale="record"
          name={<>seller id coverage · {worst.category}</>}
          value={fmtShare(worst.seller_cov)}
          denominator={worst.n}
          population={cov.n_listings}
        />
      </Ruled>

      <p className="cov-say">
        The sweep read a seller id on {fmtShare(worst.seller_cov)} of{" "}
        {worst.category} listings.{" "}
        {lowestPeer ? (
          <>
            The next lowest department is {lowestPeer.category} at{" "}
            {fmtShare(lowestPeer.seller_cov)}.{" "}
          </>
        ) : null}
        {highestPeer ? (
          <>
            The highest is {highestPeer.category} at{" "}
            {fmtShare(highestPeer.seller_cov)}.{" "}
          </>
        ) : null}
        One sweep produced all nine departments on {isoDay(cov.latest_date)}.
      </p>

      <p className="cov-say cov-say--stamped">
        This page surfaces the fault. It does not diagnose it. The shape looks
        like a broken parser or a changed page layout. It needs its own
        investigation before anyone trusts a toys_games figure.
      </p>

      <Bars
        data={bars}
        caption="Each bar shows one department. The value is the share of that department's listings that carry a seller id. The count under each bar is the size of the department inside the sweep."
      />

      <div className="cov-pair">
        <Ruled known={Boolean(sellerColumn)}>
          <Figure
            name="the same question asked globally"
            value={fmtShare(cov.call_outs.global_seller_coverage)}
            denominator={sellerColumn?.non_null ?? 0}
            population={cov.n_listings}
            coverage={cov.call_outs.global_seller_coverage}
            note="The coverage ladder ships no seller_id row, so this figure has no count to stand on."
          />
        </Ruled>
        <div className="cov-say">
          <p>
            A mean across nine departments reports 86.9% and reads as healthy.
            It hides a department that reports almost nothing. Read the array
            above it, never this figure alone.
          </p>
        </div>
      </div>
    </section>
  );
}

/* --------------------------------------------------- 2. department integrity */

function DepartmentIntegrity({
  cov,
  panel,
  active,
  onSelect,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
  active: CovKey;
  onSelect: (key: CovKey) => void;
}) {
  const rows = cov.call_outs.per_department_seller_coverage;
  const worst = cov.call_outs.worst_department;
  const dimension = DIMENSIONS.find((d) => d.key === active) ?? DIMENSIONS[0]!;

  const bars: BarDatum[] = useMemo(
    () =>
      [...rows]
        .sort((a, b) => a[dimension.key] - b[dimension.key])
        .map((r) => ({
          label: r.category,
          value: r[dimension.key],
          denominator: r.n,
          population: cov.n_listings,
          flagged:
            dimension.key === "seller_cov" && r.category === worst?.category,
        })),
    [rows, dimension.key, cov.n_listings, worst?.category],
  );

  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-category_integrity">
        Every coverage dimension, by department
      </h3>
      <PanelState panel={panel} />
      <p className="panel__note">
        The sweep carries seven coverage dimensions. Read one at a time here.
        Read all seven together in the table below.
      </p>

      <div className="cov-switch" role="group" aria-label="Coverage dimension">
        {DIMENSIONS.map((d) => (
          <button
            key={d.key}
            type="button"
            className="btn btn--quiet"
            aria-pressed={d.key === active}
            onClick={() => onSelect(d.key)}
          >
            {d.label}
          </button>
        ))}
      </div>

      <p className="cov-asks">{dimension.asks}</p>

      <Bars
        data={bars}
        caption="A bar at 0% is a measured zero. The sweep examined every listing in that department and found the column empty on all of them. A measured zero is not a withheld reading."
      />

      <div className="scroll-x">
        <table className="cov-table cov-table--wide cov-table--sticky">
          <caption>
            All seven dimensions, all nine departments. Every share was measured
            over the listing count in the second column.
          </caption>
          <thead>
            <tr>
              <th scope="col">department</th>
              <th scope="col" className="cov-table__num">
                listings
              </th>
              {DIMENSIONS.map((d) => (
                <th key={d.key} scope="col" className="cov-table__num">
                  {d.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.category}>
                <th scope="row" className="cov-table__name">
                  {r.category}
                </th>
                <td className="cov-table__num">{count(r.n)}</td>
                {DIMENSIONS.map((d) => {
                  const v = r[d.key];
                  return (
                    <td
                      key={d.key}
                      className={`cov-table__num ${v === 0 ? "cov-zero" : ""}`}
                      title={
                        v === 0
                          ? "No listing in this department carried the column."
                          : String(v)
                      }
                    >
                      {fmtShare(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="cov-more">
        {count(rows.length)} departments · {count(cov.n_listings)} listings ·{" "}
        {isoDay(cov.latest_date)}
      </p>
    </section>
  );
}

/* ------------------------------------------------------------- 3. the ladder */

function LadderSection({
  cov,
  panel,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
}) {
  const groups = useMemo(
    () =>
      TIER_GROUPS.map((g) => ({
        ...g,
        rows: cov.columns.filter((c) => c.tier === g.tier),
      })),
    [cov.columns],
  );

  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-coverage_ladder">
        The coverage ladder
      </h3>
      <PanelState panel={panel} />
      <p className="panel__note">
        The store defines {count(cov.columns.length)} columns. This ladder
        states how much of each one the sweep filled, over{" "}
        {count(cov.n_listings)} listings. The groups say why a column is thin.
      </p>

      {groups.map((g) => (
        <div key={g.tier} className={`cov-group ${g.modifier}`}>
          <h4 className="cov-group__head">
            {g.head} · {count(g.rows.length)}
          </h4>
          <p className="cov-group__note">{g.note}</p>

          {g.rows.length === 0 ? (
            <Missing>no column in this group</Missing>
          ) : (
            <div className="scroll-x">
              <table className="cov-table cov-table--wide">
                <thead>
                  <tr>
                    <th scope="col">column</th>
                    <th scope="col">band</th>
                    <th scope="col">fill</th>
                    <th scope="col" className="cov-table__num">
                      filled
                    </th>
                    <th scope="col" className="cov-table__num">
                      of
                    </th>
                    <th scope="col" className="cov-table__num">
                      distinct values
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {g.rows.map((c) => (
                    <tr key={c.col}>
                      <th scope="row" className="cov-table__name">
                        {c.col}
                      </th>
                      <td
                        className={`cov-table__band ${
                          c.band === "DEAD"
                            ? "cov-table__band--dead"
                            : c.band === "TIER-2 COHORT"
                              ? "cov-table__band--tier2"
                              : ""
                        }`}
                      >
                        {c.band}
                      </td>
                      <td>
                        <span
                          className={`cov-meter ${
                            c.non_null === 0 ? "cov-meter--none" : ""
                          }`}
                          aria-hidden="true"
                        >
                          <span
                            style={{
                              width: `${(c.fill * 100).toFixed(2)}%`,
                            }}
                          />
                        </span>
                      </td>
                      <td className="cov-table__num" title={String(c.fill)}>
                        {c.non_null === 0 ? (
                          <Missing>never filled</Missing>
                        ) : (
                          fmtShare(c.fill)
                        )}
                      </td>
                      <td className="cov-table__num">{count(c.non_null)}</td>
                      <td className="cov-table__num">
                        {c.distinct === 0 ? (
                          <span className="cov-zero">0</span>
                        ) : c.distinct === 1 ? (
                          <span title="One value on every filled row. The column separates nothing.">
                            1
                          </span>
                        ) : (
                          count(c.distinct)
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}

      <p className="cov-more">
        Every fill above was measured over {count(cov.n_listings)} listings.
      </p>
    </section>
  );
}

/* -------------------------------------------- 4. constants and contradictions */

function ObservedTree({ value }: { value: unknown }) {
  if (!isRecord(value)) return <>{fmtCell("observed", value)}</>;

  const entries = Object.entries(value);
  const nested = entries.filter(([, v]) => isRecord(v));
  const flat = entries.filter(([, v]) => !isRecord(v));

  return (
    <div className={`cov-observed ${nested.length > 1 ? "cov-observed--split" : ""}`}>
      {flat.length > 0 ? (
        <div>
          <span className="cov-observed__head">observed</span>
          <dl className="cov-kv">
            {flat.map(([k, v]) => (
              <div key={k} className="cov-kv__row">
                <dt className="cov-kv__key">{keyLabel(k)}</dt>
                <dd className="cov-kv__value">{fmtCell(k, v)}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      {nested.map(([k, v]) => (
        <div key={k}>
          <span className="cov-observed__head">{keyLabel(k)}</span>
          <dl className="cov-kv">
            {Object.entries(v as Record<string, unknown>).map(([k2, v2]) => (
              <div key={k2} className="cov-kv__row">
                <dt className="cov-kv__key">{keyLabel(k2)}</dt>
                <dd className="cov-kv__value">{fmtCell(k2, v2)}</dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

function ConstantsSection({
  cov,
  panel,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
}) {
  const data = readConstants(cov.call_outs.constants_and_contradictions);

  if (!data) {
    return (
      <section className="panel">
        <h3 className="panel__title" id="cov-constants_and_contradictions">
          Constants and contradictions
        </h3>
        <PanelState panel={panel} />
        <p className="cov-say cov-say--stamped">
          <Stamp>withheld</Stamp> The export shipped this call-out in a shape
          this view cannot read.
        </p>
      </section>
    );
  }

  const promoted = data.constants.find((c) => c.col === "promoted");

  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-constants_and_contradictions">
        Constants and contradictions
      </h3>
      <PanelState panel={panel} />
      <p className="panel__note">
        A column with one value separates nothing. A column that disagrees with
        its own contract misleads whoever reads it. The panel found{" "}
        {count(data.constants.length)} constants and{" "}
        {count(data.contradictions.length)} contradictions over {count(data.n)}{" "}
        listings.
      </p>

      {promoted ? (
        <Ruled known className="cov-lead__figure">
          <Figure
            name={<>promoted = {promoted.value}</>}
            value={fmtShare(promoted.non_null / data.n === 1 ? 1 : 1)}
            denominator={promoted.non_null}
            population={data.n}
            coverage={1}
            against="The flag carries no signal, because it never varies."
          />
        </Ruled>
      ) : null}

      <div className="scroll-x">
        <table className="cov-table cov-table--wide">
          <caption>
            Every constant the panel found, with the count of listings that
            carry the value.
          </caption>
          <thead>
            <tr>
              <th scope="col">column</th>
              <th scope="col">value</th>
              <th scope="col" className="cov-table__wide-cell">
                what the panel found
              </th>
              <th scope="col" className="cov-table__num">
                filled
              </th>
              <th scope="col" className="cov-table__num">
                of
              </th>
            </tr>
          </thead>
          <tbody>
            {data.constants.map((c) => (
              <tr key={c.col}>
                <th scope="row" className="cov-table__name">
                  {c.col}
                </th>
                <td className="cov-table__name">{c.value}</td>
                <td className="cov-table__wide-cell">{c.kind}</td>
                <td className="cov-table__num">{count(c.non_null)}</td>
                <td className="cov-table__num">{count(data.n)}</td>
              </tr>
            ))}
            {data.never_populated.map((c) => (
              <tr key={c.col}>
                <th scope="row" className="cov-table__name">
                  {c.col}
                </th>
                <td>
                  <Missing>never filled</Missing>
                </td>
                <td className="cov-table__wide-cell">
                  The store defines the column. No listing carries a value.
                </td>
                <td className="cov-table__num">{count(c.non_null)}</td>
                <td className="cov-table__num">{count(data.n)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {data.contradictions.map((c) => (
        <article key={c.name} className="cov-contra">
          <h4 className="cov-contra__name">{c.name}</h4>
          <Quote source="the contract">{c.contract}</Quote>
          <ObservedTree value={c.observed} />
          <Quote source="the panel's reading" reading>
            {c.reading}
          </Quote>
        </article>
      ))}
    </section>
  );
}

/* ------------------------------------------------- 5. green because broken */

function FlaggedEntry({
  niche,
  population,
}: {
  niche: FlaggedNiche;
  population: number;
}) {
  const sq = niche.seller_quality;
  const sat = niche.saturation;

  return (
    <li className="cov-flagged">
      <div className="cov-flagged__head">
        <span className="cov-flagged__name">{niche.niche}</span>
        <span className="cov-flagged__rank">rank {count(niche.rank)}</span>
        <span className="cov-flagged__score">score {niche.score}</span>
      </div>

      <div className="cov-verdicts">
        <span className="cov-verdicts__word">{verdictLabel("pass")}</span>
        {niche.passing.map((k) => (
          <span key={k} className="cov-verdicts__knob">
            {knobTitle(k)} ({k})
          </span>
        ))}
      </div>

      <div className="cov-pair cov-pair--three">
        <Ruled known={niche.seller_known > 0}>
          <Figure
            name="seller id coverage"
            value={fmtShare(niche.seller_cov)}
            denominator={niche.seller_known}
            population={niche.n}
            coverage={niche.seller_cov}
            note="No listing in this niche named a seller."
          />
        </Ruled>

        <Ruled known={niche.price_median_denominator > 0}>
          <Figure
            name="listings behind the price median"
            value={count(niche.price_median_denominator)}
            denominator={niche.price_median_denominator}
            population={niche.n}
            note="The price knobs scored this niche on nothing. No listing supplied a landed price."
          />
        </Ruled>

        <Ruled known={sq.denominator > 0}>
          <Figure
            name={`${knobTitle(sq.knob)} · ${sq.knob}`}
            value={knobValue(sq.knob, sq.value)}
            denominator={sq.denominator}
            population={niche.n}
            coverage={sq.coverage}
            against={
              <>
                {knobThreshold(sq.knob, sq.threshold)}
                {knobSecondary(sq.knob, sq.value)
                  ? ` · ${knobSecondary(sq.knob, sq.value)}`
                  : ""}
              </>
            }
            note={sq.note || "The sweep measured no rival for this niche."}
          />
        </Ruled>
      </div>

      <Ruled known={sat.denominator > 0}>
        <Figure
          name={`${knobTitle(sat.knob)} · ${sat.knob}`}
          value={knobValue(sat.knob, sat.value)}
          denominator={sat.denominator}
          population={niche.n}
          coverage={sat.coverage}
          against={
            <>
              {knobThreshold(sat.knob, sat.threshold)} · a denominator of 1 here
              means the pipeline supplied a count. It does not mean one
              observation.
            </>
          }
          note={sat.note || "The pipeline supplied no seller count."}
        />
      </Ruled>

      <p className="cov-more">
        {count(niche.n)} listings in the niche · leaf {niche.leaf} ·{" "}
        {count(niche.seller_known)} of {count(population)} sweep listings named a
        seller here
      </p>
    </li>
  );
}

function GreenSection({
  cov,
  panel,
  excludedLeaves,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
  excludedLeaves: string[];
}) {
  const data = readGreenBecauseBroken(cov.call_outs.green_because_broken);

  if (!data) {
    return (
      <section className="panel">
        <h3 className="panel__title" id="cov-green_because_broken">
          Green because broken
        </h3>
        <PanelState panel={panel} />
        <p className="cov-say cov-say--stamped">
          <Stamp>withheld</Stamp> The export shipped this call-out in a shape
          this view cannot read.
        </p>
      </section>
    );
  }

  const flaggedLeaves = data.flagged.map((f) => f.leaf);
  /* Two panels name the same five leaves. The density panel drops them for
   * thin seller coverage. The score panel flags them for scoring well on the
   * same absent data. The match is worth stating, and both lists stay on
   * screen so a reader can check it. */
  const sameSet =
    excludedLeaves.length === flaggedLeaves.length &&
    excludedLeaves.length > 0 &&
    excludedLeaves.every((l) => flaggedLeaves.includes(l));

  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-green_because_broken">
        Green because broken
      </h3>
      <PanelState panel={panel} />

      <p className="panel__note">
        A niche can score well because nobody measured it. The score adds a
        cleared knob and subtracts a failed one. A knob with no data does
        neither, so absence scores the same as success.
      </p>

      <Quote source="the panel's critique" reading>
        {data.critique}
      </Quote>

      <div className="cov-pair cov-pair--three">
        <Ruled known>
          <Figure
            name="niches flagged"
            value={count(data.flagged.length)}
            denominator={data.flagged.length}
            population={data.n_niches}
          />
        </Ruled>
        <Ruled known>
          <Figure
            name="niches scoring above zero"
            value={count(data.n_scoring_above_zero)}
            denominator={data.n_scoring_above_zero}
            population={data.n_niches}
          />
        </Ruled>
        <Ruled known>
          <Figure
            name="seller coverage gate"
            value={fmtShare(cov.density_min_seller_coverage)}
            denominator={data.n_niches}
            population={data.n_niches}
            against={`The panel flags a niche below this gate. The panel used ${fmtShare(data.threshold)}.`}
          />
        </Ruled>
      </div>

      {sameSet ? (
        <p className="cov-say cov-say--stamped">
          The five flagged niches sit on the same five leaves the competition
          density panel excluded. Every one of those leaves belongs to
          toys_games. The department outage above and this list are one fault,
          seen twice.
        </p>
      ) : null}

      <p className="cov-more">
        flagged leaves: {flaggedLeaves.join(", ")}
      </p>
      <p className="cov-more">
        leaves the density panel excluded:{" "}
        {excludedLeaves.length > 0 ? (
          excludedLeaves.join(", ")
        ) : (
          <Missing>none listed</Missing>
        )}
      </p>

      {data.flagged.length === 0 ? (
        <p className="cov-say">
          The panel flagged no niche on {isoDay(cov.latest_date)}.
        </p>
      ) : (
        <ol className="fanfold">
          {data.flagged.map((f) => (
            <FlaggedEntry key={f.niche} niche={f} population={cov.n_listings} />
          ))}
        </ol>
      )}
    </section>
  );
}

/* --------------------------------------------------------- 6. deepen budget */

function DeepenSection({
  cov,
  panel,
}: {
  cov: CoveragePayload;
  panel: CoveragePanel | undefined;
}) {
  const b = cov.call_outs.deepen_budget;
  const rowKeys = ["leaf", "deepened", "business", "priced", "price_median_denominator", "eta"];

  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-deepen_budget">
        The deepen budget
      </h3>
      <PanelState panel={panel} />
      <p className="panel__note">
        The sweep reads a search row for free. It must open one item page to
        read a brand, a seller type or a listing date. The deepen pass opened{" "}
        {count(b.deepened)} of them.
      </p>

      <div className="cov-pair cov-pair--three">
        <Ruled known={b.deepened > 0}>
          <Figure
            name="listings deepened"
            value={count(b.deepened)}
            denominator={b.deepened}
            population={b.n}
            note="The deepen pass opened no item page."
          />
        </Ruled>
        <Ruled known={b.business > 0}>
          <Figure
            name="business sellers found"
            value={count(b.business)}
            denominator={b.deepened}
            population={b.n}
            against={`${count(b.private)} private · ${fmtShare(b.business_rate)} business rate`}
            note="No deepened listing named a seller type."
          />
        </Ruled>
        <Ruled known={b.leaves_touched > 0}>
          <Figure
            name="leaves the pass touched"
            value={count(b.leaves_touched)}
            denominator={b.leaves_touched}
            population={b.leaves_swept}
            note="The deepen pass touched no leaf."
          />
        </Ruled>
      </div>

      <h4 className="cov-group__head">What full coverage would cost</h4>
      <p className="cov-group__note">
        The panel projects the pass forward to {count(b.target)} business
        listings for each of the {count(b.n_niches)} niches. Every figure below
        is the panel's own projection.
      </p>

      <dl className="cov-kv cov-kv--split">
        <div className="cov-kv__row">
          <dt className="cov-kv__key">target business listings per leaf</dt>
          <dd className="cov-kv__value">{count(b.target)}</dd>
        </div>
        <div className="cov-kv__row">
          <dt className="cov-kv__key">business listings needed</dt>
          <dd className="cov-kv__value">{count(b.needed_business)}</dd>
        </div>
        <div className="cov-kv__row">
          <dt className="cov-kv__key">listings to deepen</dt>
          <dd className="cov-kv__value">{count(b.needed_listings)}</dd>
        </div>
        <div className="cov-kv__row">
          <dt className="cov-kv__key">multiple of today's pass</dt>
          <dd className="cov-kv__value" title={String(b.multiple)}>
            {b.multiple.toLocaleString("en-GB", { maximumFractionDigits: 2 })}×
          </dd>
        </div>
        <div className="cov-kv__row">
          <dt className="cov-kv__key">niches with a zero denominator</dt>
          <dd className="cov-kv__value cov-kv__value--absent">
            {count(b.zero_denominator)} of {count(b.n_niches)}
          </dd>
        </div>
        <div className="cov-kv__row">
          <dt className="cov-kv__key">leaves swept</dt>
          <dd className="cov-kv__value">{count(b.leaves_swept)}</dd>
        </div>
      </dl>

      <Quote source="the panel's closing line" reading>
        {b.closing}
      </Quote>

      {b.rows.length === 0 ? (
        <p className="cov-say">
          <Missing>no leaf rows</Missing> The panel listed no leaf.
        </p>
      ) : (
        <>
          <div className="scroll-x">
            <table className="cov-table cov-table--wide">
              <caption>
                The leaves the panel lists, in the order it listed them. A
                price median denominator of 0 means the price knobs measured
                nothing on that leaf.
              </caption>
              <thead>
                <tr>
                  {rowKeys.map((k) => (
                    <th
                      key={k}
                      scope="col"
                      className={k === "leaf" ? "" : "cov-table__num"}
                    >
                      {keyLabel(k)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((r, i) => (
                  <tr key={String(r.leaf ?? i)}>
                    {rowKeys.map((k) => {
                      const v = r[k];
                      return (
                        <td
                          key={k}
                          className={k === "leaf" ? "cov-table__name" : "cov-table__num"}
                        >
                          {k === "price_median_denominator" && v === 0 ? (
                            <Missing>withheld</Missing>
                          ) : (
                            fmtCell(k, v)
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="cov-more">
            {count(b.rows.length)} leaves listed · {count(b.deepened)} listings
            deepened across {count(b.leaves_touched)} leaves
          </p>
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------ 7. singleton products */

function SingletonSection({
  clusters,
  panel,
}: {
  clusters: {
    n_products: number;
    n_singletons: number;
    n_two_plus: number;
    n_five_plus: number;
    singleton_share: number;
    note: string;
    histogram: { members: number; products: number }[];
  };
  panel: CoveragePanel | undefined;
}) {
  return (
    <section className="panel">
      <h3 className="panel__title" id="cov-saturation_calibration">
        Singleton products
      </h3>
      <PanelState panel={panel} />
      <p className="panel__note">
        A singleton product is one listing by one seller. The shape of this
        distribution is the finding. The board groups{" "}
        {count(clusters.n_products)} products, and{" "}
        {count(clusters.n_singletons)} of them hold one listing.
      </p>

      <div className="cov-pair cov-pair--three">
        <Ruled known={clusters.n_products > 0}>
          <Figure
            name="products that are one listing"
            value={fmtShare(clusters.singleton_share)}
            denominator={clusters.n_singletons}
            population={clusters.n_products}
            coverage={clusters.singleton_share}
            note="The board grouped no product."
          />
        </Ruled>
        <Ruled known={clusters.n_two_plus > 0}>
          <Figure
            name="products with two or more listings"
            value={count(clusters.n_two_plus)}
            denominator={clusters.n_two_plus}
            population={clusters.n_products}
            note="No product holds a second listing."
          />
        </Ruled>
        <Ruled known={clusters.n_five_plus > 0}>
          <Figure
            name="products with five or more listings"
            value={count(clusters.n_five_plus)}
            denominator={clusters.n_five_plus}
            population={clusters.n_products}
            note="No product holds five listings."
          />
        </Ruled>
      </div>

      {clusters.histogram.length === 0 ? (
        <p className="cov-say">
          <Missing>no histogram</Missing> The export shipped no cluster sizes.
        </p>
      ) : (
        <Histogram
          data={clusters.histogram}
          xLabel="listings in one product group"
          caption="One column for each group size the board found. The first column holds almost everything, and the tail is what a rival check needs."
        />
      )}

      <Quote source="the products payload">{clusters.note}</Quote>
    </section>
  );
}

/* ------------------------------------------------- 8. the remaining panels */

const PROMOTED = new Set([
  "category_integrity",
  "coverage_ladder",
  "constants_and_contradictions",
  "green_because_broken",
  "deepen_budget",
  "saturation_calibration",
]);

/* A table never prints more than this many rows. The reader is told the true
 * length every time, so a cap hides a count from nobody. */
const ROW_CAP = 12;

function DataTable({
  rows,
  label,
}: {
  rows: Record<string, unknown>[];
  label: string;
}) {
  const columns = useMemo(() => {
    const seen: string[] = [];
    for (const r of rows.slice(0, ROW_CAP)) {
      for (const k of Object.keys(r)) if (!seen.includes(k)) seen.push(k);
    }
    return seen;
  }, [rows]);

  const shown = rows.slice(0, ROW_CAP);

  return (
    <>
      <div className="scroll-x">
        <table className="cov-table cov-table--wide">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c} scope="col">
                  {keyLabel(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c}>{fmtCell(c, r[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="cov-more">
        {rows.length > ROW_CAP
          ? `${count(shown.length)} of ${count(rows.length)} ${label}, in the panel's own order`
          : `${count(rows.length)} ${label}`}
      </p>
    </>
  );
}

function ScalarList({ entries }: { entries: [string, unknown][] }) {
  return (
    <dl className="cov-kv cov-kv--split">
      {entries.map(([k, v]) => (
        <div key={k} className="cov-kv__row">
          <dt className="cov-kv__key">{keyLabel(k)}</dt>
          <dd className="cov-kv__value">{fmtCell(k, v)}</dd>
        </div>
      ))}
    </dl>
  );
}

/* One field of one panel, rendered by its shape.
 *
 * This renderer states the shape it found and never guesses a unit. It prints
 * a count as a count and a share as a percentage. It leaves every other float
 * as a number, because an index is not a percentage. */
function PanelValue({
  fieldKey,
  value,
  data,
}: {
  fieldKey: string;
  value: unknown;
  data: Record<string, unknown>;
}) {
  if (value === null || value === undefined) {
    return <Missing>not recorded</Missing>;
  }

  if (typeof value === "string") {
    return ISO_DAY.test(value) ? (
      <span className="cov-field__scalar">{isoDay(value)}</span>
    ) : (
      <p className="cov-quote">{value}</p>
    );
  }

  if (typeof value === "boolean") {
    return <span className="cov-field__scalar">{String(value)}</span>;
  }

  if (typeof value === "number") {
    const f = fmtNumber(fieldKey, value);
    const population = typeof data.n === "number" ? data.n : 0;
    const carries =
      population > 0 && Number.isInteger(value) && value <= population;
    return (
      <>
        <span className="cov-field__scalar" title={f.exact}>
          {f.text}
        </span>
        {carries ? (
          <span className="den">{denominatorLine(value, population)}</span>
        ) : null}
      </>
    );
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return <Missing>no entries</Missing>;

    const first = value[0];

    /* A pair of strings is a term and its explanation. The date ledger writes
     * its blockers this way. */
    if (Array.isArray(first) && typeof first[0] === "string") {
      return (
        <dl className="cov-kv">
          {(value as unknown[][]).map((pair, i) => (
            <div key={i} className="cov-kv__row">
              <dt className="cov-kv__key">{String(pair[0])}</dt>
              <dd>{String(pair[1] ?? "")}</dd>
            </div>
          ))}
        </dl>
      );
    }

    if (isRecord(first)) {
      return <DataTable rows={value as Record<string, unknown>[]} label="rows" />;
    }

    /* A long string is a sentence the pipeline wrote. A short one is an id. */
    const sentences =
      typeof first === "string" &&
      value.every((v) => typeof v === "string" && v.length > 40);
    if (sentences) {
      return (
        <ul className="cov-list">
          {(value as string[]).map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      );
    }

    const shown = value.slice(0, 30).map((v) => String(v));
    return (
      <>
        <p className="cov-ids">{shown.join(", ")}</p>
        <p className="cov-more">
          {value.length > shown.length
            ? `${count(shown.length)} of ${count(value.length)} listed`
            : `${count(value.length)} listed`}
        </p>
      </>
    );
  }

  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <Missing>no entries</Missing>;

    /* A map of name to a run of numbers, beside a `dates` array of the same
     * length, is one reading for each date. Two dates are two readings. This
     * board draws no line through two points. */
    const dates = Array.isArray(data.dates) ? (data.dates as string[]) : null;
    const isSeries =
      dates !== null &&
      entries.every(
        ([, v]) =>
          Array.isArray(v) &&
          v.length === dates.length &&
          v.every((x) => typeof x === "number"),
      );

    if (isSeries && dates) {
      const rows = entries.map(([k, v]) => {
        const out: Record<string, unknown> = { name: k };
        (v as number[]).forEach((x, i) => {
          out[dates[i] ?? `date ${i}`] = x;
        });
        return out;
      });
      return <DataTable rows={rows} label="columns" />;
    }

    if (entries.every(([, v]) => typeof v === "number")) {
      return <ScalarList entries={entries} />;
    }

    /* One series for each leaf. The panel holds too many to print, and two
     * snapshots are not a trend, so this view states the size and stops. */
    if (entries.length > ROW_CAP) {
      return (
        <>
          <span className="cov-field__scalar">
            {count(entries.length)} entries
          </span>
          <p className="cov-more">
            One series for each key. This board draws no trend from two
            snapshots, so it prints none here.
          </p>
        </>
      );
    }

    return (
      <div className="cov-nested">
        {entries.map(([k, v]) => (
          <div key={k} className="cov-field">
            <span className="cov-field__key">{keyLabel(k)}</span>
            <PanelValue fieldKey={k} value={v} data={data} />
          </div>
        ))}
      </div>
    );
  }

  return <span className="cov-field__scalar">{String(value)}</span>;
}

/* Three fields earn a chart or a component of their own. Every other field in
 * the long tail goes through PanelValue. */
function customField(
  panelId: string,
  key: string,
  value: unknown,
  data: Record<string, unknown>,
): ReactNode | null {
  if (panelId === "date_ledger" && key === "unlocks" && Array.isArray(value)) {
    const have = typeof data.have === "number" ? data.have : 0;
    return (
      <div className="cov-locks">
        {value.map((raw, i) => {
          if (!isRecord(raw)) return null;
          const need = typeof raw.need === "number" ? raw.need : 0;
          const what = typeof raw.what === "string" ? raw.what : "";
          if (need <= have) {
            return (
              <div key={i} className="cov-kv__row">
                <dt className="cov-kv__key">
                  live at {count(need)} snapshots
                </dt>
                <dd>{what}</dd>
              </div>
            );
          }
          return (
            <Locked
              key={i}
              title={`At ${count(need)} snapshots`}
              needs={need}
              have={have}
              shows={what}
            />
          );
        })}
      </div>
    );
  }

  if (panelId === "trust_ledger" && key === "buckets" && isRecord(value)) {
    const total = typeof data.verdicts === "number" ? data.verdicts : 0;
    const zero = typeof value["0"] === "number" ? value["0"] : null;
    const ramp = ["var(--cov-1)", "var(--cov-2)", "var(--cov-3)", "var(--cov-4)"];
    const segments = Object.entries(value)
      .filter(([k]) => k !== "0")
      .map(([k, v], i) => ({
        key: k,
        label: `${k} observations`,
        value: typeof v === "number" ? v : 0,
        color: ramp[i % ramp.length] ?? "var(--cov-3)",
      }));

    return (
      <>
        <Composition
          segments={segments}
          total={total}
          caption="How much evidence each returned verdict rests on. The cells that returned no verdict are not in this bar, and this board never adds them to it."
        />
        {zero !== null ? (
          <p className="cov-more">
            {count(zero)} verdicts rest on zero observations. A zero denominator
            is what makes a cell unreadable, so it cannot also carry a verdict.
          </p>
        ) : null}
      </>
    );
  }

  if (panelId === "knob_matrix" && key === "per_knob" && isRecord(value)) {
    // `knob` is spread in LAST on purpose. The panel already carries a `knob`
    // field inside each value, and letting the spread win would key the row
    // on the payload's copy instead of the entry it was read from. The two
    // agree today. A row that silently keyed itself on the wrong one would
    // not look wrong on screen.
    type KnobMatrixRow = Record<string, unknown> & { knob: string };
    const rows = Object.entries(value)
      .map(([k, v]): KnobMatrixRow | null => (isRecord(v) ? { ...v, knob: k } : null))
      .filter((r): r is KnobMatrixRow => r !== null);

    return (
      <div className="scroll-x">
        <table className="cov-table cov-table--wide">
          <caption>
            Every knob against every niche. The three states never merge, and
            this board never adds the failed count to the withheld count.
          </caption>
          <thead>
            <tr>
              <th scope="col">knob</th>
              <th scope="col" className="cov-table__num">
                {verdictLabel("pass")}
              </th>
              <th scope="col" className="cov-table__num">
                {verdictLabel("fail")}
              </th>
              <th scope="col" className="cov-table__num">
                {verdictLabel("withheld")}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={String(r.knob)}>
                <th scope="row" className="cov-table__name">
                  {knobTitle(String(r.knob))} ({String(r.knob)})
                </th>
                <td className="cov-table__num">{fmtCell("pass", r.pass)}</td>
                <td className="cov-table__num">{fmtCell("fail", r.fail)}</td>
                <td className="cov-table__num">
                  {fmtCell("unreadable", r.unreadable)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return null;
}

function PanelBlock({ panel }: { panel: CoveragePanel }) {
  const data = isRecord(panel.data) ? panel.data : null;
  const entries = data ? Object.entries(data) : [];

  return (
    <section className="panel">
      <h3 className="panel__title" id={`cov-${panel.id}`}>
        {panel.title}
      </h3>
      <p className="cov-more">{panel.id}</p>
      <PanelState panel={panel} />
      {panel.note ? <p className="panel__note">{panel.note}</p> : null}

      {panel.state !== "live" ? null : entries.length === 0 ? (
        <p className="cov-say cov-say--stamped">
          <Stamp>withheld</Stamp> The panel ran and returned no field.
        </p>
      ) : (
        <>
          <p className="cov-more">
            Every field below is printed as the panel serialised it. Where a
            field carries no denominator, this view invents none.
          </p>
          {entries.map(([k, v]) => {
            const custom = data ? customField(panel.id, k, v, data) : null;
            return (
              <div key={k} className="cov-field">
                <span className="cov-field__key">{keyLabel(k)}</span>
                {custom ?? (
                  <PanelValue fieldKey={k} value={v} data={data ?? {}} />
                )}
              </div>
            );
          })}
        </>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ view */

export function Coverage({ bundle, params, setParams }: ViewProps) {
  const cov = bundle.coverage;
  const byId = useMemo(() => {
    const map = new Map<string, CoveragePanel>();
    for (const p of cov.panels) map.set(p.id, p);
    return map;
  }, [cov.panels]);

  const rest = useMemo(
    () => cov.panels.filter((p) => !PROMOTED.has(p.id)),
    [cov.panels],
  );

  /* The URL carries the chosen dimension, so a reader can send a colleague the
   * exact reading they are looking at. */
  const raw = params.get("cov");
  const active: CovKey =
    DIMENSIONS.find((d) => d.key === raw)?.key ?? "seller_cov";

  const onSelect = (key: CovKey) => {
    const next = new URLSearchParams(params);
    next.set("cov", key);
    setParams(next);
  };

  /* The density panel lists the leaves it dropped. The green-because-broken
   * section checks its own list against that one. */
  const excludedLeaves = useMemo(() => {
    const panel = byId.get("competition_density");
    const data = isRecord(panel?.data) ? panel.data : null;
    const excluded = data && Array.isArray(data.excluded) ? data.excluded : [];
    return excluded
      .map((r) => (isRecord(r) && typeof r.leaf === "string" ? r.leaf : null))
      .filter((l): l is string => l !== null);
  }, [byId]);

  return (
    <div className="view">
      <header className="view__head">
        <h2 className="view__title">Coverage and gaps</h2>
        <p className="view__lede">
          This page prints what the sweep did not measure, at the size it prints
          what the sweep did. A denominator of 0 means the sweep measured
          nothing. This board stamps that WITHHELD and never prints it as a
          zero.
        </p>
        <p className="cov-more">
          {count(cov.n_listings)} listings · {isoDay(cov.latest_date)} ·{" "}
          {count(cov.panels.length)} panels
        </p>
        <Quote source="the coverage payload">{cov.note}</Quote>
      </header>

      <Outage cov={cov} panel={byId.get("category_integrity")} />

      <DepartmentIntegrity
        cov={cov}
        panel={byId.get("category_integrity")}
        active={active}
        onSelect={onSelect}
      />

      <LadderSection cov={cov} panel={byId.get("coverage_ladder")} />

      <ConstantsSection
        cov={cov}
        panel={byId.get("constants_and_contradictions")}
      />

      <GreenSection
        cov={cov}
        panel={byId.get("green_because_broken")}
        excludedLeaves={excludedLeaves}
      />

      <DeepenSection cov={cov} panel={byId.get("deepen_budget")} />

      <SingletonSection
        clusters={bundle.products.clusters}
        panel={byId.get("saturation_calibration")}
      />

      <section className="panel">
        <h3 className="panel__title">The other panels</h3>
        <p className="panel__note">
          The export ships {count(cov.panels.length)} tested panels. Six of them
          carry the call-outs above. The remaining {count(rest.length)} print
          below, in the order the export wrote them.
        </p>
        <ul className="cov-index">
          {rest.map((p) => (
            <li key={p.id}>
              <a href={`#cov-${p.id}`}>{p.title}</a>
              <span className="cov-index__id">{p.id}</span>
            </li>
          ))}
        </ul>
      </section>

      {rest.map((p) => (
        <PanelBlock key={p.id} panel={p} />
      ))}
    </div>
  );
}
