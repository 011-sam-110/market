/* The knob console. Six controls drawn as one load path.
 *
 * A settings screen usually shows six sliders in a column. Each one gets a
 * small readout. The operator then has to work out which control does
 * anything at all.
 *
 * This board cannot afford that. In this store the export withholds five of
 * the six knobs on almost every product. A console of six equal controls
 * invites the operator to drag the one that changes nothing.
 *
 * So the six knobs attach to one spine and share one load. The load is all
 * 27,206 products, applied to every knob at the same time. The knob that
 * fails the most products gets the only heavy rule on the view.
 *
 * Two rules in the arithmetic are load-bearing. Both are easy to break
 * quietly.
 *
 * 1. A knob's FAILURES and its WITHHOLDS are different quantities over
 *    different denominators. The board counts failures over the products the
 *    knob could judge. It counts withholds over every product examined. It
 *    never sums them, never stacks them in one bar, and never draws them on
 *    one scale. A knob withheld on 27,206 products is not strict. It is
 *    unmeasured, and a looser bar changes nothing at all.
 *
 * 2. A control changes the COMPARISON, never the measurement. The margin
 *    knob is the exception. Its figure is a function of the five margin and
 *    fee fields through `fees.max_cost`. A move to any of them invalidates
 *    the figure instead of re-comparing it. `rethresholdRow` reports that as
 *    stale, and this view shows the stale state. It never shows a number
 *    computed under a setting nobody looks at.
 */

import "../styles/views/console.css";

import {
  useCallback,
  useDeferredValue,
  useEffect,
  useId,
  useMemo,
  useState,
} from "react";

import { Figure } from "../components/Figure";
import { Stamp } from "../components/Stamp";
import { Composition } from "../components/charts";
import { decodeProduct } from "../lib/data";
import {
  bpPercent,
  count,
  coveragePercent,
  knobQuestion,
  knobTitle,
  money,
} from "../lib/format";
import type { Knobs } from "../lib/knobs";
import {
  MARGIN_INPUT_FIELDS,
  bindingConstraint,
  marginInputsMoved,
  rethresholdRow,
  type BindingReport,
} from "../lib/rethreshold";
import { buildHash } from "../lib/router";
import type {
  Bundle,
  ExportedKnob,
  KnobField,
  KnobName,
  ProductsPayload,
  Provenance,
} from "../lib/types";
import type { KnobState } from "../lib/useKnobs";
import type { ViewProps } from "../lib/viewProps";

/* ------------------------------------------------------------- decoding */

/** Only the knob block of a product decides how the product re-thresholds. */
type KnobRow = { knobs: ExportedKnob[] };

/* The decode of 27,206 products costs a few hundred milliseconds. The answer
 * never changes. So the cache hangs off the payload object, not off a
 * component that unmounts each time the operator taps another view. */
const ROWS = new WeakMap<ProductsPayload, KnobRow[]>();

function knobRows(payload: ProductsPayload): KnobRow[] {
  const cached = ROWS.get(payload);
  if (cached) return cached;
  const out: KnobRow[] = new Array(payload.n);
  for (let i = 0; i < payload.n; i += 1) {
    out[i] = { knobs: decodeProduct(payload, i).knobs };
  }
  ROWS.set(payload, out);
  return out;
}

/* --------------------------------------------------------------- report */

interface Report {
  products: number;
  /** Products that clear all six. No failure and nothing withheld. */
  clears: number;
  failsAny: number;
  /** Fails nothing. The board still cannot call it a pass. */
  noFailButWithheld: number;
  /** The six failure counts summed. This double-counts a product that fails
   * more than one knob. The view prints it only to say that out loud. */
  sumFailed: number;
  binding: BindingReport;
  marginStale: boolean;
}

function buildReport(
  rows: KnobRow[],
  knobs: Knobs,
  defaults: Knobs,
  knobNames: KnobName[],
): Report {
  const scored = rows.map((r) => rethresholdRow(r, knobs, defaults));

  let clears = 0;
  let failsAny = 0;
  let noFailButWithheld = 0;
  for (const s of scored) {
    if (s.failed > 0) failsAny += 1;
    else if (s.unknown > 0) noFailButWithheld += 1;
    else clears += 1;
  }

  const binding = bindingConstraint(scored, knobNames);
  const sumFailed = knobNames.reduce((a, n) => a + binding.failed[n], 0);

  return {
    products: rows.length,
    clears,
    failsAny,
    noFailButWithheld,
    sumFailed,
    binding,
    marginStale: marginInputsMoved(knobs, defaults),
  };
}

/* ------------------------------------------------------ one path member */

interface PathKnob {
  name: KnobName;
  number: number;
  fields: KnobField[];
  /** Products this knob fails, out of the products it could judge. */
  failed: number;
  /** Products it could not read at all. The board never adds this to
   * `failed`. */
  withheld: number;
  judged: number;
}

/* ----------------------------------------------------------- provenance */

/** What each provenance word means to a reader who has not read the spec.
 * The tag alone does not carry it. A guess and a measurement must not look
 * alike on this board. */
const PROVENANCE_GLOSS: Record<Provenance, string> = {
  measured:
    "The sweep measured this. A move re-compares a real observation against a new bar.",
  chosen: "A person picked this number. No measurement stands under the value.",
  unverified:
    "Nobody established this. The value marks an open question. It is not a finding.",
  decision:
    "This states a live business decision. It is not a measurement. It changes the fee arithmetic itself.",
};

const UNIT_WORD: Record<KnobField["unit"], string> = {
  basis_points: "basis points",
  pence: "pence",
  days: "days",
  count: "count",
};

/** One field's value in its own units. Basis points read as the percentage
 * the rest of the board uses. The line under the slider keeps the raw
 * integer, so the unit the config stores stays visible. */
function reading(field: KnobField, value: number): string {
  switch (field.unit) {
    case "pence":
      return money(value) ?? String(value);
    case "days":
      return `${value} ${value === 1 ? "day" : "days"}`;
    case "count":
      return count(value) ?? String(value);
    case "basis_points":
      return bpPercent(value, 2) ?? String(value);
    default:
      return String(value);
  }
}

/* ------------------------------------------------------------- the view */

export function Console({ bundle, knobState }: ViewProps) {
  const { knobs, defaults } = knobState;
  const fields = bundle.knobs.fields;
  const knobNames = bundle.knobs.knob_names;

  const rows = useMemo(() => knobRows(bundle.products), [bundle.products]);

  /* A recount of 27,206 products across six knobs on every drag frame makes
   * the slider itself lag. The controls take the new setting at once. The
   * counts follow one beat later, and the setting bar says so. */
  const settled = useDeferredValue(knobs);
  const counting = settled !== knobs;

  const report = useMemo(
    () => buildReport(rows, settled, defaults, knobNames),
    [rows, settled, defaults, knobNames],
  );

  const path = useMemo<PathKnob[]>(() => {
    const byNumber = new Map<number, KnobField[]>();
    for (const f of fields) {
      if (f.knob === null) continue;
      const list = byNumber.get(f.knob);
      if (list) list.push(f);
      else byNumber.set(f.knob, [f]);
    }
    return knobNames.map((name, i) => {
      const withheld = report.binding.withheld[name];
      return {
        name,
        number: i + 1,
        fields: byNumber.get(i + 1) ?? [],
        failed: report.binding.failed[name],
        withheld,
        judged: Math.max(0, report.products - withheld),
      };
    });
  }, [fields, knobNames, report]);

  const feeFields = useMemo(() => fields.filter((f) => f.knob === null), [fields]);

  /* A field numbered outside the exported knob list has no place on the
   * path. It has never happened. If the exporter ever writes one, the
   * console must name it rather than drop the control. */
  const orphans = useMemo(
    () =>
      fields.filter(
        (f) => f.knob !== null && (f.knob < 1 || f.knob > knobNames.length),
      ),
    [fields, knobNames],
  );

  const heaviest = Math.max(...path.map((k) => k.failed), 0);
  const binding = report.binding.knob;
  const bindingKnob = path.find((k) => k.name === binding) ?? null;
  const runnerUp = path
    .filter((k) => k.name !== binding && k.failed > 0)
    .sort((a, b) => b.failed - a.failed)[0];

  /* A knob withheld on every product makes a clearing product impossible at
   * any setting. A withheld knob never becomes a pass. The board derives
   * this. If the store ever measures sell-through, the sentence disappears
   * on its own. */
  const blockers = path.filter(
    (k) => report.products > 0 && k.withheld === report.products,
  );
  const blockerNames = blockers.map((k) => knobTitle(k.name)).join(" and ");

  const empty = report.products === 0;

  return (
    <div className="view console">
      <div className="view__head">
        <h2 className="view__title">Knob console</h2>
        <p className="view__lede">
          A control changes the comparison. No control changes the measurement.
          The export shipped each product&rsquo;s measured inputs. The browser
          asks those inputs a new question.
        </p>
      </div>

      <SettingBar
        knobState={knobState}
        counting={counting}
        products={report.products}
        fieldCount={fields.length}
      />

      <section className="panel console-load">
        <h3 className="panel__title">The load</h3>
        <p className="panel__note">
          The board tests all {count(report.products)} exported products against
          all six knobs. The six knobs run at the same time. They are not
          stages. One product can fail more than one knob.
        </p>

        {empty ? (
          <div className="state">
            <p className="state__title">Nothing to count</p>
            <p className="state__body">
              The export wrote no products. The board cannot draw a figure from
              an empty store. The controls below still set a preset.
            </p>
          </div>
        ) : (
          <>
            <div className="console-load__record">
              <span className="field__name">Products that clear all six</span>
              <span className="console-load__figure">
                {count(report.clears)}
              </span>
              <span className="den">
                over {count(report.products)} products examined
              </span>
            </div>

            {report.clears === 0 ? (
              <p className="console-load__why">
                This is a measured zero. It is not a missing figure. The board
                tested all {count(report.products)} products at this setting.
                None of them cleared all six knobs.
              </p>
            ) : null}

            {blockers.length > 0 ? (
              <p className="console-load__blocked">
                The board withholds {blockerNames} on all{" "}
                {count(report.products)} products. A withheld knob never becomes
                a pass at any threshold. So no position of these controls
                produces a product that clears. This states a fact about the
                store. It does not state a fault in the setting.
              </p>
            ) : null}

            <Composition
              total={report.products}
              caption="Each product falls in exactly one of these three groups. The three counts sum to the products examined. The per-knob counts further down do not sum, and you must never add them."
              segments={[
                {
                  key: "clears",
                  label: "Clears all six",
                  value: report.clears,
                  color: "var(--pass)",
                  note: "The board measured every knob and every knob is satisfied.",
                },
                {
                  key: "fails",
                  label: "Fails at least one",
                  value: report.failsAny,
                  color: "var(--fail-mark)",
                  note: "The board measured at least one knob and the product sits outside it.",
                },
                {
                  key: "withheld",
                  label: "No failure, but withheld somewhere",
                  value: report.noFailButWithheld,
                  color: "var(--withheld)",
                  note: "Nothing measured says no. Nothing measured says yes either.",
                },
              ]}
            />

            <p className="console-load__arith">
              {count(report.failsAny)} products fail at least one knob. The six
              failure counts below sum to {count(report.sumFailed)}. That total
              is larger, because it counts a product once inside every knob the
              product fails.
            </p>
          </>
        )}
      </section>

      <section className="panel">
        <h3 className="panel__title">The load path</h3>
        <p className="panel__note">
          Six knobs attach to one spine and share one load. Each knob carries
          two counts, and the board never joins them into one number. It counts
          failures over the products the knob could judge. It counts withholds
          over every product examined. The two counts use different
          denominators, so you must not add them. A console that adds them
          tells you to turn the wrong control.
        </p>

        <div className="console-tie">
          <span className="console-tie__load">
            Load applied · {count(report.products)} products · all six knobs
          </span>
          <p className="console-tie__binding">
            {empty ? (
              "The export holds no products, so no knob binds."
            ) : bindingKnob ? (
              <>
                <strong>
                  Binding knob: {knobTitle(bindingKnob.name)} (knob{" "}
                  {bindingKnob.number}).
                </strong>{" "}
                It fails {count(bindingKnob.failed)} of the{" "}
                {count(bindingKnob.judged)} products it could judge. No other
                knob fails more.{" "}
                {runnerUp
                  ? `The next heaviest knob is ${knobTitle(
                      runnerUp.name,
                    )} at ${count(runnerUp.failed)} products.`
                  : "No other knob fails a single product."}
              </>
            ) : (
              <>
                <strong>No knob binds.</strong> Not one knob fails a single
                product at this setting. Every product the board rejects fails
                because the board could not measure something. No control
                changes that.
              </>
            )}
          </p>
        </div>

        <ol className="console-path">
          {path.map((k) => (
            <KnobBlock
              key={k.name}
              knob={k}
              products={report.products}
              heaviest={heaviest}
              binding={k.name === binding}
              knobState={knobState}
              bundle={bundle}
              marginStale={report.marginStale}
            />
          ))}
        </ol>

        {orphans.length > 0 ? (
          <p className="console-outofrange">
            {count(orphans.length)} exported field
            {orphans.length === 1 ? " carries" : "s carry"} a knob number
            outside the six knobs the export names (
            {orphans.map((f) => f.field).join(", ")}). They have no place on the
            path. The board does not show them as controls.
          </p>
        ) : null}
      </section>

      <FeeTerms fields={feeFields} knobState={knobState} bundle={bundle} />

      <Preset knobState={knobState} fieldCount={fields.length} />
    </div>
  );
}

/* ---------------------------------------------------------- setting bar */

function SettingBar({
  knobState,
  counting,
  products,
  fieldCount,
}: {
  knobState: KnobState;
  counting: boolean;
  products: number;
  fieldCount: number;
}) {
  const moved = knobState.overridden.length;
  return (
    <div className="console-setting">
      <span className="console-setting__count">
        {fieldCount} fields ·{" "}
        <span className={moved ? "console-setting__moved" : undefined}>
          {moved} moved from the export
        </span>
      </span>
      <button
        type="button"
        className="btn btn--quiet"
        onClick={knobState.reset}
        disabled={moved === 0}
      >
        Reset every field
      </button>
      <span className="console-setting__pending" aria-live="polite">
        {counting
          ? `The board recounts ${count(products)} products at the new setting. The figures below still show the previous setting.`
          : "The figures below use the setting on the controls."}
      </span>
    </div>
  );
}

/* --------------------------------------------------------- one knob box */

function KnobBlock({
  knob,
  products,
  heaviest,
  binding,
  knobState,
  bundle,
  marginStale,
}: {
  knob: PathKnob;
  products: number;
  heaviest: number;
  binding: boolean;
  knobState: KnobState;
  bundle: Bundle;
  marginStale: boolean;
}) {
  const empty = products === 0;
  const failWidth = heaviest > 0 ? (knob.failed / heaviest) * 100 : 0;
  const withheldWidth = products > 0 ? (knob.withheld / products) * 100 : 0;

  /* The sentence that stops the operator turning a control that cannot move
   * anything. Three cases, and the view prints only the true one. */
  let readingLine: string | null = null;
  if (!empty && knob.judged === 0) {
    readingLine =
      `The board withheld this knob on all ${count(products)} products. ` +
      `No position of this control changes a verdict. The knob is not ` +
      `strict. It is unmeasured.`;
  } else if (!empty && knob.withheld > knob.failed) {
    readingLine =
      `The board withheld this knob on ${count(knob.withheld)} products. ` +
      `It fails ${count(knob.failed)} of the ${count(knob.judged)} products ` +
      `it could judge. A looser bar moves at most ${count(knob.failed)} ` +
      `products. It does not touch the ${count(knob.withheld)} products the ` +
      `board could not read.`;
  } else if (!empty && knob.failed > 0) {
    readingLine =
      `The board judged this knob on ${count(knob.judged)} of ` +
      `${count(products)} products. ${count(knob.failed)} of them fail ` +
      `because of this knob.`;
  }

  const stale = knob.name === "min_margin" && marginStale;

  return (
    <li className={`console-knob ${binding ? "console-knob--binding" : ""}`}>
      <div className="console-knob__head">
        <h4 className="console-knob__title">
          <span className="console-knob__number">Knob {knob.number} · </span>
          {knobTitle(knob.name)}
        </h4>
        {binding ? (
          <span className="console-knob__carrying">carries the load</span>
        ) : null}
        {stale ? <Stamp>figure stale</Stamp> : null}
      </div>

      <p className="console-knob__question">{knobQuestion(knob.name)}</p>

      {empty ? (
        <p className="console-knob__reading">
          The export holds no products. This knob has judged nothing.
        </p>
      ) : (
        <>
          <div className="console-counts">
            <Figure
              name="Products it fails"
              value={count(knob.failed)}
              denominator={knob.judged}
              population={products}
              note={`The board judged no product here. It withheld this knob on all ${count(
                products,
              )} products. No population exists to measure a failure rate over.`}
              against={
                knob.judged > 0 ? (
                  <span>
                    {coveragePercent(
                      knob.failed / knob.judged,
                      knob.failed / knob.judged < 0.001 ? 2 : 1,
                    )}{" "}
                    of the products it could judge
                  </span>
                ) : undefined
              }
            />
            {/* The population is the denominator here, so there is no
             * fraction to print. A "100%" on every withheld figure trains
             * the eye to skip the line that carries the count. */}
            <Figure
              name="Products it cannot read"
              value={count(knob.withheld)}
              denominator={products}
              population={0}
              against={
                <span>
                  {coveragePercent(knob.withheld / products, 1)} of every
                  product examined
                </span>
              }
            />
            <p className="console-counts__rule">
              Two counts. Two denominators. {count(knob.failed)} and{" "}
              {count(knob.withheld)} do not add. This board never prints their
              sum.
            </p>
          </div>

          <div className="console-struts">
            <div className="console-strut console-strut--fail">
              <span className="console-strut__label">
                <span>Load carried · products failed</span>
                <span>{count(knob.failed)}</span>
              </span>
              <span className="console-strut__track" aria-hidden="true">
                <span
                  className="console-strut__fill"
                  style={{ width: `${failWidth.toFixed(2)}%` }}
                />
              </span>
              <span className="console-strut__scale">
                {heaviest > 0
                  ? `Scaled against the heaviest knob at ${count(
                      heaviest,
                    )} products.`
                  : "No knob fails a product at this setting."}
              </span>
            </div>

            <div className="console-strut console-strut--withheld">
              <span className="console-strut__label">
                <span>Not read · products withheld</span>
                <span>{count(knob.withheld)}</span>
              </span>
              <span className="console-strut__track" aria-hidden="true">
                <span
                  className="console-strut__fill"
                  style={{ width: `${withheldWidth.toFixed(2)}%` }}
                />
              </span>
              <span className="console-strut__scale">
                Scaled against all {count(products)} products examined. This is
                a different scale from the bar above.
              </span>
            </div>
          </div>
        </>
      )}

      {readingLine ? (
        <p
          className={`console-knob__reading ${
            binding ? "console-knob__reading--carrying" : ""
          }`}
        >
          {readingLine}
        </p>
      ) : null}

      {stale ? (
        <p className="console-knob__stale">
          The margin figure is stale. The board did not re-compare it. The
          value <span className="console-term">max_cost</span> is a function of
          the five margin and fee fields. A move to any of them changes the
          figure itself. It does not change the bar. A new figure needs each
          product&rsquo;s own price, and that price lives in the metrics file
          this view does not read. The board withholds every product here until
          the five fields return to the exported values.
        </p>
      ) : null}

      {knob.name === "sell_through" ? (
        <SellThroughWindow
          bundle={bundle}
          withheld={knob.withheld}
          products={products}
        />
      ) : null}

      {knob.fields.length === 0 ? (
        <p className="console-outofrange">
          The export names this knob. It ships no field for it, so you cannot
          turn it. The counts above still come from the export.
        </p>
      ) : (
        knob.fields.map((f) => (
          <Control key={f.field} field={f} knobState={knobState} />
        ))
      )}
    </li>
  );
}

/* ------------------------------------------------- the sell-through case */

/** Why this one control does nothing, said where the control is.
 *
 * Without this the operator drags the slider, watches every count stay
 * still, and decides the console is broken. The window measures 1 day
 * against a bar set for 30 days. The export withholds, because a short
 * window returns a confident FAIL. */
function SellThroughWindow({
  bundle,
  withheld,
  products,
}: {
  bundle: Bundle;
  withheld: number;
  products: number;
}) {
  const spec = bundle.knobs.sell_through_window;
  const live = bundle.deltas?.sell_through_comparable ?? null;

  return (
    <div className="console-window">
      <p className="console-window__title">Why this control changes nothing</p>
      <p className="console-window__body">
        {live ? (
          <>
            The board measured a {live.window_days}-day window between the two
            snapshots. The threshold is set for {spec.target_days} days. Below{" "}
            {spec.min_comparable_days} days the export refuses the comparison.
            So the board withholds sell-through on {count(withheld)} of{" "}
            {count(products)} products. It stays withheld at every position of
            this control.
          </>
        ) : (
          <>
            The store holds no second snapshot yet. Sell-through is a lifetime
            average, and a lifetime average is not comparable to a{" "}
            {spec.target_days}-day threshold. So the board withholds it on{" "}
            {count(withheld)} of {count(products)} products. It stays withheld
            at every position of this control.
          </>
        )}
      </p>
      <dl className="console-window__terms">
        <div>
          <dt>Window measured</dt>
          <dd>{live ? `${live.window_days}d` : "none"}</dd>
        </div>
        <div>
          <dt>Threshold set for</dt>
          <dd>{spec.target_days}d</dd>
        </div>
        <div>
          <dt>Comparable at</dt>
          <dd>{spec.min_comparable_days}d</dd>
        </div>
      </dl>
      <p className="console-gloss">
        {spec.note} That bar is {spec.min_comparable_fraction} of the target
        window.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------- controls */

function Control({
  field,
  knobState,
}: {
  field: KnobField;
  knobState: KnobState;
}) {
  const id = useId();
  const noteId = `${id}-note`;
  const value = knobState.knobs[field.field];
  const moved = value !== field.default;
  const outOfRange = value < field.min || value > field.max;

  return (
    <div className="control">
      <div className="control__row">
        <label className="control__label" htmlFor={id}>
          {field.label}
        </label>
        <span
          className={`control__value ${moved ? "control__value--moved" : ""}`}
        >
          {reading(field, value)}
        </span>
      </div>

      <input
        id={id}
        type="range"
        min={field.min}
        max={field.max}
        step={field.step}
        value={value}
        aria-describedby={noteId}
        onChange={(e) =>
          knobState.set(field.field, Number(e.currentTarget.value))
        }
      />

      <div className="console-scale">
        <span>{reading(field, field.min)}</span>
        <span>step {reading(field, field.step)}</span>
        <span>{reading(field, field.max)}</span>
      </div>

      <span className="console-raw">
        {count(value)} {UNIT_WORD[field.unit]} · {field.env_var}
      </span>

      {outOfRange ? (
        <p className="console-outofrange">
          The setting holds {count(value)} for this field. The export allows{" "}
          {count(field.min)} to {count(field.max)}. The comparison still runs at{" "}
          {count(value)}. The slider cannot show it.
        </p>
      ) : null}

      {moved ? (
        <p className="console-moved">
          <span className="console-moved__word">Moved</span>
          <span>the export set this field to {reading(field, field.default)}</span>
          <button
            type="button"
            className="btn btn--quiet"
            onClick={() => knobState.resetField(field.field)}
          >
            Reset this field
          </button>
        </p>
      ) : null}

      <p
        className={`console-gloss console-gloss--${field.provenance}`}
        id={noteId}
      >
        <span className={`provenance-tag provenance-tag--${field.provenance}`}>
          {field.provenance}
        </span>{" "}
        {PROVENANCE_GLOSS[field.provenance]} {field.note}
      </p>
    </div>
  );
}

/* ------------------------------------------------------------ fee terms */

/** The three fields that are not knobs.
 *
 * They sit behind a disclosure and off the load path, because no comparison
 * uses them. They are terms inside `fees.max_cost`. A move to one of them
 * changes what the margin figure is. */
function FeeTerms({
  fields,
  knobState,
  bundle,
}: {
  fields: KnobField[];
  knobState: KnobState;
  bundle: Bundle;
}) {
  const movedHere = fields.filter(
    (f) => knobState.knobs[f.field] !== f.default,
  ).length;

  if (fields.length === 0) {
    return (
      <section className="panel">
        <h3 className="panel__title">Fee terms</h3>
        <p className="panel__note">
          The export shipped no fee terms. The pipeline still ran the fee
          arithmetic with the values it held. This console cannot show them.
        </p>
      </section>
    );
  }

  return (
    <details className="console-fees">
      <summary>
        Fee terms, not knobs ({fields.length})
        {movedHere > 0 ? ` · ${movedHere} moved` : ""}
      </summary>
      <div className="console-fees__body">
        <p className="panel__note">
          The board compares nothing against these three fields. They are terms
          inside the fee arithmetic. A move changes what the margin figure is.
          It does not change the bar. The margin knob becomes stale the moment
          one of them moves. The value{" "}
          <span className="console-term">max_cost</span> reads five fields:{" "}
          <span className="console-term">{MARGIN_INPUT_FIELDS.join(", ")}</span>.
        </p>
        <p className="panel__note">
          These terms multiply against rates the export shipped, and this
          console does not edit those rates. The export resolved a final value
          fee for {count(bundle.fees.n_leaves)} leaves. The remaining{" "}
          {count(bundle.fees.n_leaves_unresolved)} unresolved leaves take the
          everything-else rate of {bpPercent(bundle.fees.everything_else_bp, 2)}.
          A regulatory operating fee adds{" "}
          {bpPercent(bundle.fees.regulatory_bp, 2)}. A per-order fee adds{" "}
          {money(bundle.fees.per_order_pence_low)} at or below{" "}
          {money(bundle.fees.per_order_threshold_pence)}, and{" "}
          {money(bundle.fees.per_order_pence_high)} above it.
        </p>
        <p className="console-gloss">{bundle.fees.note}</p>

        {fields.map((f) => (
          <Control key={f.field} field={f} knobState={knobState} />
        ))}
      </div>
    </details>
  );
}

/* --------------------------------------------------------------- preset */

type Copy =
  | { kind: "idle" }
  | { kind: "copied" }
  | { kind: "refused"; detail: string };

type Publish =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "accepted"; setting: string }
  | { kind: "not-wired"; status: number; statusText: string }
  | { kind: "unreachable"; detail: string };

function Preset({
  knobState,
  fieldCount,
}: {
  knobState: KnobState;
  fieldCount: number;
}) {
  const [copy, setCopy] = useState<Copy>({ kind: "idle" });
  const [publish, setPublish] = useState<Publish>({ kind: "idle" });

  const moved = knobState.overridden.length;
  const setting = useMemo(
    () => JSON.stringify(knobState.knobs),
    [knobState.knobs],
  );

  const link = useMemo(() => {
    const { origin, pathname, search } = window.location;
    return `${origin}${pathname}${search}${buildHash("console", knobState.query)}`;
  }, [knobState.query]);

  // A copy result, or a failed publish, describes the setting it was made
  // at. Once a control moves it describes nothing. So the view clears it
  // rather than let the operator read it as current.
  useEffect(() => {
    setCopy({ kind: "idle" });
    setPublish((p) => (p.kind === "accepted" ? p : { kind: "idle" }));
  }, [setting]);

  const onCopy = useCallback(() => {
    void (async () => {
      try {
        if (!navigator.clipboard?.writeText) {
          throw new Error("this browser gives the page no clipboard here");
        }
        await navigator.clipboard.writeText(link);
        setCopy({ kind: "copied" });
      } catch (e) {
        setCopy({
          kind: "refused",
          detail: e instanceof Error ? e.message : String(e),
        });
      }
    })();
  }, [link]);

  const onPublish = useCallback(() => {
    setPublish({ kind: "sending" });
    void (async () => {
      try {
        const res = await fetch("/api/preset", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ schema_version: 1, knobs: knobState.knobs }),
        });
        if (!res.ok) {
          setPublish({
            kind: "not-wired",
            status: res.status,
            statusText: res.statusText,
          });
          return;
        }
        setPublish({ kind: "accepted", setting });
      } catch (e) {
        setPublish({
          kind: "unreachable",
          detail: e instanceof Error ? e.message : String(e),
        });
      }
    })();
  }, [knobState.knobs, setting]);

  const publishedStale =
    publish.kind === "accepted" && publish.setting !== setting;

  return (
    <section className="panel">
      <h3 className="panel__title">This setting</h3>
      <p className="panel__note">
        {moved === 0
          ? "Nothing moved. This is the setting the export itself ran at."
          : `${moved} of the ${fieldCount} fields differ from the export. This browser remembers them. The link carries them to a second reader.`}
      </p>

      <div className="console-actions">
        <button type="button" className="btn" onClick={onCopy}>
          Copy link
        </button>
        <button
          type="button"
          className="btn"
          onClick={onPublish}
          disabled={publish.kind === "sending"}
        >
          {publish.kind === "sending" ? "Publishing" : "Publish preset"}
        </button>
        <button
          type="button"
          className="btn btn--quiet"
          onClick={knobState.reset}
          disabled={moved === 0}
        >
          Reset every field
        </button>
      </div>

      <input
        className="console-link"
        type="text"
        readOnly
        value={link}
        aria-label="Link to this setting"
        onFocus={(e) => e.currentTarget.select()}
      />

      <p className="console-status" aria-live="polite">
        {copy.kind === "copied" ? (
          <>
            <span className="console-status__word">Copied.</span> The link
            carries the {moved} field{moved === 1 ? "" : "s"} that differ from
            the export.
          </>
        ) : copy.kind === "refused" ? (
          <>
            <span className="console-status__word">Clipboard refused.</span> The
            browser did not let the page write to it ({copy.detail}). The link
            sits in the box above. Touch it and it selects itself.
          </>
        ) : (
          <>
            The link carries only the fields you moved. It never carries all{" "}
            {fieldCount}. A link you send today does not keep asserting
            today&rsquo;s guess at the two undecided fee terms.
          </>
        )}
      </p>

      <p className="console-status" aria-live="polite">
        {publishedStale ? (
          <>
            <span className="console-status__word">Published, then moved.</span>{" "}
            A control changed after the publish. The screen no longer shows what
            the board sent. Publish again to send this setting.
          </>
        ) : publish.kind === "accepted" ? (
          <>
            <span className="console-status__word">Published.</span> The
            endpoint accepted this setting as schema version 1.
          </>
        ) : publish.kind === "not-wired" ? (
          <>
            <span className="console-status__word">
              The endpoint is not wired yet.
            </span>{" "}
            <span className="console-term">POST /api/preset</span> answered{" "}
            {publish.status}
            {publish.statusText ? ` ${publish.statusText}` : ""}. Nothing broke
            and nothing was lost. This board is a set of static files, and no
            preset service serves it yet. The setting stays in this browser and
            in the link above.
          </>
        ) : publish.kind === "unreachable" ? (
          <>
            <span className="console-status__word">
              The endpoint did not answer.
            </span>{" "}
            The request to <span className="console-term">POST /api/preset</span>{" "}
            did not complete ({publish.detail}). Expect this answer until
            someone deploys a preset service beside these files. The setting
            stays in this browser and in the link above.
          </>
        ) : publish.kind === "sending" ? (
          <>
            The board posts all {fieldCount} values to{" "}
            <span className="console-term">POST /api/preset</span>.
          </>
        ) : (
          <>
            Publish posts all {fieldCount} values. It includes the two fee terms
            nobody has settled. A preset without them drifts on the day a
            default changes. No preset service serves this board yet, so expect
            the board to say so.
          </>
        )}
      </p>
    </section>
  );
}
