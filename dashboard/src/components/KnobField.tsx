/* One knob's ruled field: the reading, what it was measured against, and
 * the count it rests on.
 *
 * Six of these make up an entry. The rules that hold:
 *
 *  - three states, never two. `passed === null` is WITHHELD and is rendered
 *    as a stamp, not as a fail and not as a dash;
 *  - the verdict is always a word as well as a colour;
 *  - the binding knob — the one carrying the load in the current setting —
 *    gets the only heavy rule on the page.
 */

import {
  knobQuestion,
  knobSecondary,
  knobThreshold,
  knobTitle,
  knobValue,
  verdictLabel,
  verdictOf,
} from "../lib/format";
import type { ExportedKnob } from "../lib/types";
import { Figure } from "./Figure";

export interface KnobFieldProps {
  knob: ExportedKnob;
  /** n_listings for this row — the population the denominator sits within. */
  population: number;
  /** True for the knob currently binding the funnel. At most one. */
  binding?: boolean;
}

export function KnobField({ knob, population, binding = false }: KnobFieldProps) {
  const verdict = verdictOf(knob.passed);
  const value = knobValue(knob.knob, knob.value);
  const secondary = knobSecondary(knob.knob, knob.value);

  return (
    <div
      className={[
        "field",
        `field--${verdict}`,
        binding ? "field--binding" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <Figure
        name={knobTitle(knob.knob)}
        value={value}
        denominator={knob.denominator}
        population={population}
        coverage={knob.coverage}
        note={knob.note}
        against={
          <span className="knob-field__against">
            <span className="knob-field__verdict">{verdictLabel(verdict)}</span>
            <span aria-hidden="true"> · </span>
            <span className="knob-field__threshold">
              {knobThreshold(knob.knob, knob.threshold)}
            </span>
          </span>
        }
      />

      {secondary ? <span className="knob-field__secondary">{secondary}</span> : null}

      {binding ? (
        <span className="knob-field__binding">
          binding — this is the knob eliminating the most rows right now
        </span>
      ) : null}

      <span className="visually-hidden">{knobQuestion(knob.knob)}</span>
    </div>
  );
}
