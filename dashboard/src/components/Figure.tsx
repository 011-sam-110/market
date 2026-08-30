/* A figure and the count it was measured over, in that order, always.
 *
 * This component is the whole thesis of the board in one element. There is
 * no prop that lets a caller render the number without the denominator,
 * because the denominator is not decoration on the number — it is the part
 * that says whether the number means anything. A figure with a denominator
 * of 0 does not render as a number at all; it renders as a stamp.
 */

import type { ReactNode } from "react";

import { denominatorLine } from "../lib/format";
import { Stamp } from "./Stamp";

export interface FigureProps {
  /** The already-formatted figure, or null when it is unknown. */
  value: string | null;
  /** How many observations the figure was measured over. 0 means unknown. */
  denominator: number;
  /** The population the denominator sits within, for the fraction. */
  population: number;
  /** The exporter's own coverage fraction, when it carries one. */
  coverage?: number;
  /** Small caps above the figure. */
  name?: ReactNode;
  /** What the threshold was, rendered in the same units as the value. */
  against?: ReactNode;
  /** Why it is unknown, in the pipeline's own words. */
  note?: string;
  /** Rendered at the largest scale on the page. One per view, at most. */
  scale?: "record" | "figure";
  className?: string;
}

export function Figure({
  value,
  denominator,
  population,
  coverage,
  name,
  against,
  note,
  scale = "figure",
  className = "",
}: FigureProps) {
  const known = denominator > 0 && value !== null;

  return (
    <div className={`figure ${known ? "" : "figure--unknown"} ${className}`}>
      {name ? <span className="field__name">{name}</span> : null}

      {known ? (
        <span
          className="field__figure"
          style={scale === "record" ? { fontSize: "var(--t-record)" } : undefined}
        >
          {value}
        </span>
      ) : (
        <Stamp>withheld</Stamp>
      )}

      <span className={`den ${known ? "" : "den--unknown"}`}>
        {denominatorLine(denominator, population, coverage)}
      </span>

      {against ? <span className="figure__against">{against}</span> : null}
      {!known && note ? <p className="figure__why">{note}</p> : null}
    </div>
  );
}
