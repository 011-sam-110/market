/* The shortlist. This is the landing view, and it must not become a shopping
 * list.
 *
 * Every competing product-research tool answers "what should I buy". This
 * board cannot answer that yet, and it says so. The sweep read 218 of 29,863
 * listings past the search row, so margin, price and delivery measure nothing
 * on 27,012 of the 27,206 products. Sell-through is withheld on every row,
 * because the window is 1.19 days against a bar set for 30 days.
 *
 * A row therefore has one job. It must expose its own evidence. The ranking
 * is a calibration instrument. It exists so the operator can see which knob
 * carries the load, and how much of a high score is absence.
 *
 * The demotion gate makes that difference visible. It is on by default, and
 * every entry it moves states so in words. `score = passed - failed` counts a
 * knob nobody measured the same as a knob that cleared. The gate does not
 * change that arithmetic, because the board reproduces the pipeline rather
 * than correcting it. The gate changes the ORDER, and names the count.
 */

import { useMemo, useState } from "react";

import {
  Evidence,
  gradeEvidence,
  type EvidenceGrade,
} from "../components/Evidence";
import { Figure } from "../components/Figure";
import { KnobField } from "../components/KnobField";
import { decodeProduct } from "../lib/data";
import { count, coveragePercent, knobTitle, leafLabel } from "../lib/format";
import {
  bindingConstraint,
  marginInputsMoved,
  rethresholdRow,
  type Recomputed,
  type Rethresholded,
} from "../lib/rethreshold";
import type { ExportedKnob, KnobName, ProductRow } from "../lib/types";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/shortlist.css";

/* How many entries one "show more" adds, and the hard ceiling on the page.
 * Each entry draws six ruled knob fields. 200 entries is already 1,200
 * fields. The page states this cap and the count below it, because a silent
 * truncation is what every other ranked board ships. */
const PAGE = 20;
const MAX_ENTRIES = 200;

const GRADE_ORDER: Record<EvidenceGrade, number> = {
  measured: 0,
  thin: 1,
  absent: 2,
};

/* The payload names what a product is: "a catalogued id (gtin/epid), an
 * inferred epid, or a title cluster within ONE leaf". Neither tier is better
 * evidence alone. A catalogued id is a firmer identity. A cluster can still
 * be one listing by one seller. */
const TIER_WORDS: Record<string, string> = {
  catalogued: "catalogued id",
  cluster: "title cluster",
};

/* Why a margin figure disappears when the operator moves a fee field.
 * The margin knob is not a comparison. Its value IS a function of the fee and
 * margin fields, so moving one changes the figure and not the bar. The
 * browser needs each product's own price to recompute it, and that price
 * lives in the 4.1 MB metrics payload this view does not load. The exported
 * figure under a new setting would be a confident wrong answer. */
const STALE_MARGIN_NOTE =
  "You moved a fee or margin field. Those fields change this figure itself, " +
  "not only the bar it meets. The browser needs this product's own price to " +
  "recompute the figure, and this view does not load that price. The board " +
  "withholds the reading rather than print it under a setting nobody reads.";

interface Ranked {
  row: ProductRow;
  re: Rethresholded;
  grade: EvidenceGrade;
}

/* Score, then evidence, then size. Index decides the last tie, so the order
 * stays the same across renders and does not depend on the engine. */
function compareMerit(a: Ranked, b: Ranked): number {
  if (b.re.score !== a.re.score) return b.re.score - a.re.score;
  const ga = GRADE_ORDER[a.grade];
  const gb = GRADE_ORDER[b.grade];
  if (ga !== gb) return ga - gb;
  if (b.row.n_members !== a.row.n_members) return b.row.n_members - a.row.n_members;
  return a.row.index - b.row.index;
}

/* The knob as the page must present it, not as the browser recomputed it.
 * The only edit is the note on a stale margin knob. Its exported note
 * describes a figure the current setting no longer supports. */
function forDisplay(k: Recomputed): ExportedKnob {
  return k.stale ? { ...k, note: STALE_MARGIN_NOTE } : k;
}

export function Shortlist({
  bundle,
  knobState,
  params,
  setParams,
  go,
}: ViewProps) {
  const products = bundle.products;
  const knobNames = bundle.knobs.knob_names;
  const total = products.n;

  const [shown, setShown] = useState(PAGE);

  const gateOn = params.get("gate") !== "off";

  /* Ranking is the one operation that cannot read one page at a time. The
   * rank of the top row depends on all 27,205 rows under it. So the decode
   * runs once per payload, not once per render and not once per knob move.
   * Only the visible slice reaches the DOM. */
  const rows = useMemo(() => {
    const out: ProductRow[] = [];
    for (let i = 0; i < products.n; i += 1) out.push(decodeProduct(products, i));
    return out;
  }, [products]);

  const scored = useMemo<Ranked[]>(
    () =>
      rows.map((row) => {
        const re = rethresholdRow(row, knobState.knobs, knobState.defaults);
        return {
          row,
          re,
          grade: gradeEvidence({ unknown: re.unknown, members: row.n_members }),
        };
      }),
    [rows, knobState.knobs, knobState.defaults],
  );

  const binding = useMemo(
    () => bindingConstraint(scored.map((s) => s.re), knobNames),
    [scored, knobNames],
  );

  /* This pass counts cleared directly. `rows - failed - withheld` would add
   * failed and withheld together, and those two counts must never meet in one
   * number. A knob that withholds on 27,206 rows is not strict. It is
   * unmeasured, and a looser bar changes nothing. */
  const stats = useMemo(() => {
    const cleared = Object.fromEntries(knobNames.map((k) => [k, 0])) as Record<
      KnobName,
      number
    >;
    let marginMeasured = 0;
    let sellThroughComparable = 0;
    let judgedTwoOrFewer = 0;
    let clearsAny = 0;
    let moved = 0;
    let absent = 0;
    let best = Number.NEGATIVE_INFINITY;

    for (const s of scored) {
      for (const k of s.re.knobs) {
        if (k.passed === true) cleared[k.knob] += 1;
        if (k.knob === "sell_through" && k.passed !== null) {
          sellThroughComparable += 1;
        }
        if (k.knob === "min_margin" && k.denominator > 0) marginMeasured += 1;
      }
      if (s.re.passed + s.re.failed <= 2) judgedTwoOrFewer += 1;
      if (s.re.passed > 0) clearsAny += 1;
      if (s.re.score !== s.row.score) moved += 1;
      if (s.grade === "absent") absent += 1;
      if (s.re.score > best) best = s.re.score;
    }

    return {
      cleared,
      marginMeasured,
      sellThroughComparable,
      judgedTwoOrFewer,
      clearsAny,
      moved,
      absent,
      best: Number.isFinite(best) ? best : 0,
    };
  }, [scored, knobNames]);

  /* The page computes both orders. An entry that only says "demoted" tells
   * the operator nothing they can check. The rank it would hold without the
   * gate is the part that makes the control legible.
   *
   * The gated order re-sorts the merit order on the absent flag alone.
   * Array#sort is stable, so merit order survives inside each group. */
  const order = useMemo(() => {
    const byMerit = [...scored].sort(compareMerit);

    const rawRank = new Int32Array(total);
    const gatedRank = new Int32Array(total);
    byMerit.forEach((item, i) => {
      rawRank[item.row.index] = i + 1;
    });

    const gated = [...byMerit].sort(
      (a, b) => (a.grade === "absent" ? 1 : 0) - (b.grade === "absent" ? 1 : 0),
    );
    gated.forEach((item, i) => {
      gatedRank[item.row.index] = i + 1;
    });

    /* The gate DEMOTES a row only when it moves that row under a
     * better-measured row. So this counts the rows scored on absence that
     * outrank the worst better-measured row when the gate is off. It does not
     * simply count the rows scored on absence. */
    let lastMeasured = -1;
    for (let i = byMerit.length - 1; i >= 0; i -= 1) {
      if (byMerit[i]?.grade !== "absent") {
        lastMeasured = i;
        break;
      }
    }
    let demoted = 0;
    for (let i = 0; i < lastMeasured; i += 1) {
      if (byMerit[i]?.grade === "absent") demoted += 1;
    }

    return {
      byMerit,
      gated,
      rawRank,
      gatedRank,
      demoted,
      everyRowAbsent: total > 0 && lastMeasured === -1,
    };
  }, [scored, total]);

  const ordered = gateOn ? order.gated : order.byMerit;
  const ceiling = Math.min(MAX_ENTRIES, ordered.length);
  const drawn = Math.min(shown, ceiling);
  const visible = ordered.slice(0, drawn);

  const marginStale = marginInputsMoved(knobState.knobs, knobState.defaults);
  const movedFields = knobState.overridden
    .map((f) => bundle.knobs.fields.find((x) => x.field === f)?.label ?? f)
    .join(", ");

  const deepen = bundle.coverage.call_outs.deepen_budget;
  const sellThroughNote =
    bundle.deltas?.sell_through_comparable.note ??
    bundle.knobs.sell_through_window.note;

  const setGate = (on: boolean) => {
    const next = new URLSearchParams(params);
    if (on) next.delete("gate");
    else next.set("gate", "off");
    setParams(next);
  };

  const drill = (leaf: string) => {
    const next = new URLSearchParams(knobState.query);
    next.set("leaf", leaf);
    go("niches", next);
  };

  if (total === 0) {
    return (
      <div className="view shortlist">
        <header className="view__head">
          <h2 className="view__title">Shortlist</h2>
        </header>
        <div className="state">
          <h3 className="state__title">This export holds no products</h3>
          <p className="state__body">
            The sweep read {count(bundle.manifest.latest.listings)} listings.
            None of them resolved into a product. This is a state of the
            pipeline. It is not a ranking with nothing in it.
          </p>
          <p className="state__body">{products.note}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="view shortlist">
      <header className="view__head">
        <h2 className="view__title">Shortlist</h2>
        <p className="view__lede">
          This board ranks {count(total)} products by knobs cleared minus knobs
          failed. The sweep read {count(deepen.deepened)} of{" "}
          {count(deepen.n)} listings past the search row, so margin, price and
          delivery measure nothing on most rows.{" "}
          {stats.sellThroughComparable === 0
            ? "Sell-through meets the threshold on no row at all."
            : `Sell-through meets the threshold on ${count(stats.sellThroughComparable)} rows.`}{" "}
          {count(stats.judgedTwoOrFewer)} of {count(total)} rows are judged on
          two or fewer of the six knobs.
        </p>
        <p className="view__lede">
          A high score here means few knobs could disagree with it. It is not a
          recommendation. This view prints the evidence under every row.
        </p>
      </header>

      <section
        className="panel shortlist__readings"
        aria-label="What the ranking rests on"
      >
        <div className="reading">
          <Figure
            name="Rows with a margin figure"
            value={coveragePercent(stats.marginMeasured / total, 2)}
            denominator={stats.marginMeasured}
            population={total}
            note="No row holds a landed business price, so the pipeline cannot invert a sale price into a maximum supplier cost."
          />
          {stats.marginMeasured > 0 ? (
            <p className="reading__why">
              The pipeline needs a landed business price to compute the most a
              supplier may charge.
            </p>
          ) : null}
        </div>

        <div className="reading">
          <Figure
            name="Listings read past the search row"
            value={coveragePercent(deepen.deepened / deepen.n, 2)}
            denominator={deepen.deepened}
            population={deepen.n}
            note="The sweep opened no item pages on this run, so it read nothing past the search row."
          />
          {deepen.deepened > 0 ? (
            <p className="reading__why">
              A deepened listing is one the sweep opened. Margin, price and
              delivery read only those listings.
            </p>
          ) : null}
        </div>

        <div className="reading">
          <Figure
            name="Rows with a comparable window"
            value={coveragePercent(stats.sellThroughComparable / total, 2)}
            denominator={stats.sellThroughComparable}
            population={total}
            note={sellThroughNote}
          />
          {stats.sellThroughComparable > 0 ? (
            <p className="reading__why">
              A window shorter than{" "}
              {bundle.knobs.sell_through_window.min_comparable_days} days never
              meets the {bundle.knobs.sell_through_window.target_days}-day
              threshold.
            </p>
          ) : null}
        </div>
      </section>

      <section className="panel gate" aria-labelledby="gate-title">
        <h3 className="panel__title" id="gate-title">
          The demotion gate
        </h3>

        <label className="gate__control">
          <input
            type="checkbox"
            checked={gateOn}
            onChange={(e) => setGate(e.target.checked)}
          />
          <span className="gate__label">
            Rank rows scored on absence below better-measured rows
          </span>
        </label>

        {order.everyRowAbsent ? (
          <p className="notice">
            All {count(total)} rows score on absence. No better-measured row
            exists to rank them under. The gate changes nothing at this
            setting.
          </p>
        ) : gateOn ? (
          <p className="gate__state">
            <b>On.</b> The gate ranks {count(order.demoted)} of {count(total)}{" "}
            rows below better-measured rows. Each of those rows has three or
            more of its six knobs unmeasured. An unmeasured knob scores the
            same as a knob that cleared. Every demoted entry states the rank it
            would hold without the gate.
          </p>
        ) : (
          <p className="gate__state">
            <b>Off.</b> The board uses raw score order, which is the order the
            export produces. {count(order.demoted)} of {count(total)} rows rank
            above better-measured rows on knobs nobody measured. This board
            prints that arithmetic. It does not correct it.
          </p>
        )}

        <p className="gate__basis">
          {stats.absent === total
            ? `All ${count(total)} rows carry the grade “scored on absence”.`
            : `${count(stats.absent)} of ${count(total)} rows carry the grade “scored on absence”. ${count(total - stats.absent)} rows are measured better than that.`}
        </p>
      </section>

      <section className="panel basis-panel" aria-labelledby="basis-title">
        <h3 className="panel__title" id="basis-title">
          What each knob could judge
        </h3>
        <p className="panel__note">
          This board counts cleared, failed and withheld separately. It never
          adds them. A knob that withholds on most rows is not strict. It is
          unmeasured, and a looser bar changes nothing.
        </p>

        <ul className="basis">
          {knobNames.map((name) => {
            const isBinding = binding.knob === name;
            return (
              <li
                key={name}
                className={`basis__row ${isBinding ? "basis__row--binding" : ""}`}
              >
                <span className="basis__knob">
                  {knobTitle(name)}
                  {isBinding ? (
                    <span className="basis__binding">binding</span>
                  ) : null}
                </span>
                <span className="basis__cells">
                  <span className="basis__cell basis__cell--pass">
                    <b>{count(stats.cleared[name])}</b> cleared
                  </span>
                  <span className="basis__cell basis__cell--fail">
                    <b>{count(binding.failed[name])}</b> failed
                  </span>
                  <span className="basis__cell basis__cell--withheld">
                    <b>{count(binding.withheld[name])}</b> withheld
                  </span>
                </span>
                <span className="den basis__den">
                  n={count(total)} ranked products
                </span>
              </li>
            );
          })}
        </ul>

        {binding.knob === null ? (
          <p className="notice">
            No knob fails a single row at this setting. Nothing is binding. The
            order below depends only on which knobs the pipeline could measure.
          </p>
        ) : null}

        {stats.clearsAny === 0 ? (
          <p className="notice">
            This setting clears nothing. Not one of the {count(total)} rows
            satisfies a single knob. Every row below fails or withholds. The
            ranking separates degrees of that, and nothing else.
          </p>
        ) : stats.best <= 0 ? (
          <p className="notice">
            No row clears more knobs than it fails. The best score on the board
            is {stats.best}. The order below ranks which rows fail least.
          </p>
        ) : null}

        <p className="shortlist__moved">
          {knobState.overridden.length === 0 ? (
            <>
              You moved no knob from the exported setting. This is the ranking
              the export produced.
            </>
          ) : stats.moved === 0 ? (
            <>
              You moved {movedFields}. No row scores differently from the
              export. The setting is real. It binds nothing the sweep measured.
            </>
          ) : (
            <>
              <b>{count(stats.moved)}</b> of {count(total)} rows now score
              differently from the export. You moved these fields:{" "}
              {movedFields}.
            </>
          )}
        </p>

        {marginStale ? (
          <p className="notice">
            A fee or margin field sits off its exported default. Those fields
            change the margin figure itself, not only the bar it meets. The
            browser needs each product's own price to recompute the figure, and
            this view does not load that price. The board withholds the margin
            knob on all {count(total)} rows.
          </p>
        ) : null}
      </section>

      <p className="shortlist__drill-hint">
        Open an entry title to read that leaf on the Niches board. The board
        keeps the setting you hold.
      </p>

      <ol className="fanfold shortlist__entries">
        {visible.map((item, i) => {
          const { row, re, grade } = item;
          const rank = i + 1;
          const raw = order.rawRank[row.index] ?? rank;
          const gated = order.gatedRank[row.index] ?? rank;
          const isDemoted = gateOn && grade === "absent" && gated !== raw;
          const wouldDemote = !gateOn && grade === "absent" && gated !== raw;
          const tier = TIER_WORDS[row.identity_tier] ?? row.identity_tier;

          return (
            <li key={row.key}>
              <article className="entry">
                <div className="entry__head">
                  <span className="entry__rank" aria-hidden="true">
                    {rank}
                  </span>

                  <div className="entry__identity">
                    <h3 className="entry__title">
                      <button
                        type="button"
                        className="entry__drill"
                        onClick={() => drill(row.leaf_category_id)}
                        aria-label={`Rank ${rank}. ${row.label}. Open ${leafLabel(
                          row.leaf_category_id,
                        )} on the Niches board.`}
                      >
                        {row.label}
                      </button>
                    </h3>
                    <p className="entry__meta">
                      {leafLabel(row.leaf_category_id)} · {tier} ·{" "}
                      {row.n_members === 1
                        ? "1 listing"
                        : `${count(row.n_members)} listings`}
                    </p>
                  </div>

                  <div className="entry__score">
                    <span className="field__name">Score</span>
                    <b>{re.score > 0 ? `+${re.score}` : re.score}</b>
                    <span className="entry__arith">
                      {re.passed} cleared − {re.failed} failed
                    </span>
                    <span className="entry__arith entry__arith--withheld">
                      {re.unknown} withheld, not counted
                    </span>
                  </div>
                </div>

                {isDemoted ? (
                  <p className="entry__demoted">
                    The gate demoted this row. It ranks {raw} of {count(total)}{" "}
                    without the gate.
                  </p>
                ) : null}

                {wouldDemote ? (
                  <p className="entry__demoted">
                    This row scores on absence. The gate would rank it {gated}{" "}
                    of {count(total)}.
                  </p>
                ) : null}

                <div className="entry__knobs">
                  {re.knobs.map((k) => (
                    <KnobField
                      key={k.knob}
                      knob={forDisplay(k)}
                      population={row.n_listings}
                      binding={binding.knob === k.knob}
                    />
                  ))}
                </div>

                <Evidence
                  unknown={re.unknown}
                  passed={re.passed}
                  failed={re.failed}
                  members={row.n_members}
                  sellers={row.n_sellers}
                />
              </article>
            </li>
          );
        })}
      </ol>

      <footer className="shortlist__cap">
        <p className="shortlist__cap-line">
          This page draws {count(drawn)} of {count(total)} ranked entries.{" "}
          {count(total - drawn)} entries rank below the cut and stay undrawn.
        </p>
        <p className="shortlist__cap-note">
          The page draws {count(MAX_ENTRIES)} entries at most, because each
          entry is six ruled fields. The rows past that cap are ranked, not
          missing.{" "}
          {products.capped
            ? `The export itself is capped at ${count(products.limit)} rows, so it never wrote the rows below that cut.`
            : `The export itself is not capped. It wrote all ${count(total)} rows against a limit of ${count(products.limit)}.`}
        </p>
        <button
          type="button"
          className="btn"
          disabled={drawn >= ceiling}
          onClick={() => setShown((n) => Math.min(n + PAGE, MAX_ENTRIES))}
        >
          {drawn >= ceiling
            ? `All ${count(drawn)} entries drawn`
            : `Show ${Math.min(PAGE, ceiling - drawn)} more`}
        </button>
      </footer>
    </div>
  );
}
