/* How much of a row's score was earned, and how much of it is absence.
 *
 * `score = passed − failed`, so a knob nobody could measure scores exactly
 * the same as a knob that was measured and satisfied. That is the pipeline's
 * arithmetic and this board reproduces it rather than quietly improving on
 * it — but it is also the single most misleading thing a ranked list can do,
 * so every row states how much of its score rests on nothing.
 *
 * Two separate claims live here and they are NOT the same:
 *
 *   "unknown"   — how many of the six knobs could not be measured at all;
 *   "singleton" — how many listings the product itself is made of.
 *
 * A product can be fully measured and still be one listing by one seller,
 * which is not evidence of repeatable supply. 93.4% of the products in this
 * store are singletons, so this distinction is most of the board.
 */

import { count } from "../lib/format";

export interface EvidenceProps {
  /** Knobs that came back unknown, out of six. */
  unknown: number;
  passed: number;
  failed: number;
  /** Listings behind the row. 1 means one listing by one seller. */
  members: number;
  sellers: number;
}

export type EvidenceGrade = "measured" | "thin" | "absent";

export function gradeEvidence({
  unknown,
  members,
}: Pick<EvidenceProps, "unknown" | "members">): EvidenceGrade {
  // Half the knobs missing is the line: at three unknowns a score of 3 is
  // indistinguishable from a score of 3 that was actually earned.
  if (unknown >= 3) return "absent";
  if (unknown >= 1 || members < 2) return "thin";
  return "measured";
}

const WORDS: Record<EvidenceGrade, string> = {
  measured: "measured",
  thin: "thin evidence",
  absent: "scored on absence",
};

export function Evidence({
  unknown,
  passed,
  failed,
  members,
  sellers,
}: EvidenceProps) {
  const grade = gradeEvidence({ unknown, members });

  return (
    <div className={`evidence evidence--${grade}`}>
      <span className="evidence__grade">{WORDS[grade]}</span>

      <span className="evidence__tally">
        {passed} cleared · {failed} failed ·{" "}
        <span className={unknown ? "evidence__unknown" : undefined}>
          {unknown} withheld
        </span>
      </span>

      <span className="evidence__supply">
        {members === 1
          ? "one listing, one seller"
          : `${count(members)} listings · ${count(sellers)} sellers`}
      </span>

      {grade === "absent" ? (
        <p className="evidence__why">
          {unknown} of the six knobs had nothing to measure. A knob with no
          data scores the same as a knob that passed. This row ranks above
          better-measured rows for that reason alone.
        </p>
      ) : null}

      {grade !== "absent" && members === 1 ? (
        <p className="evidence__why">
          One listing by one seller. This row can score well. It is still not
          evidence that you can buy the product again.
        </p>
      ) : null}
    </div>
  );
}
