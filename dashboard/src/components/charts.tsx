/* The chart kit.
 *
 * Small on purpose. Every form here was picked for the job the figure does
 * (magnitude → bars, composition → one stacked bar with its parts labelled,
 * a distribution → a histogram) rather than for variety, and there is no
 * line chart because two snapshots is not a trend and drawing one would
 * invent the thing this board exists to avoid.
 *
 * Two hard rules, both inherited from the palette validation in tokens.css:
 *
 *  - the four delta buckets land the green↔red deutan pair at ΔE 6.8, inside
 *    the band that is legal ONLY with secondary encoding, so every segment
 *    carries BOTH a hatch and a direct label. Removing either makes the
 *    palette non-compliant, not merely less pretty;
 *  - there is never a second y-axis. Two measures of different scale get two
 *    charts.
 */

import { useId, useState } from "react";

import { count, coveragePercent } from "../lib/format";

/* ------------------------------------------------------------- hatching */

const HATCHES = [
  { id: "h-solid", angle: null },
  { id: "h-45", angle: 45 },
  { id: "h-135", angle: 135 },
  { id: "h-dot", angle: null, dots: true },
] as const;

/** `hatchAt` pins which hatch a single-colour swatch uses. Without it a
 * legend swatch would always draw HATCHES[0] — solid — and the legend would
 * stop matching the bar it explains, which is precisely the secondary
 * encoding the palette depends on. */
function HatchDefs({
  prefix,
  colors,
  hatchAt,
}: {
  prefix: string;
  colors: string[];
  hatchAt?: number;
}) {
  return (
    <defs>
      {colors.map((color, i) => {
        const spec = HATCHES[(hatchAt ?? i) % HATCHES.length]!;
        const id = `${prefix}-${i}`;
        if (spec.angle === null && !("dots" in spec && spec.dots)) {
          return (
            <pattern key={id} id={id} width="1" height="1">
              <rect width="1" height="1" fill={color} />
            </pattern>
          );
        }
        if ("dots" in spec && spec.dots) {
          return (
            <pattern
              key={id}
              id={id}
              width="5"
              height="5"
              patternUnits="userSpaceOnUse"
            >
              <rect width="5" height="5" fill={color} opacity="0.28" />
              <circle cx="2.5" cy="2.5" r="1.15" fill={color} />
            </pattern>
          );
        }
        return (
          <pattern
            key={id}
            id={id}
            width="6"
            height="6"
            patternUnits="userSpaceOnUse"
            patternTransform={`rotate(${spec.angle})`}
          >
            <rect width="6" height="6" fill={color} opacity="0.26" />
            <line x1="0" y1="0" x2="0" y2="6" stroke={color} strokeWidth="2.6" />
          </pattern>
        );
      })}
    </defs>
  );
}

/* ------------------------------------------------------- horizontal bars */

export interface BarDatum {
  label: string;
  /** The measured value, 0..1 when `asShare`. */
  value: number;
  /** The count the value was measured over. 0 means unknown. */
  denominator: number;
  population: number;
  /** Pulls the row out of the ramp and marks it as the outlier it is. */
  flagged?: boolean;
  note?: string;
}

/** Magnitude across a small set of named things — the form for "which
 * department is broken". Sorted by the caller, never re-sorted here: the
 * order is information. */
export function Bars({
  data,
  max = 1,
  asShare = true,
  caption,
}: {
  data: BarDatum[];
  max?: number;
  asShare?: boolean;
  caption?: string;
}) {
  return (
    <div className="bars">
      {caption ? <p className="chart__caption">{caption}</p> : null}
      <ul className="bars__list">
        {data.map((d) => {
          const known = d.denominator > 0;
          const w = known ? Math.max(0, Math.min(1, d.value / max)) : 0;
          return (
            <li
              key={d.label}
              className={`bars__row ${d.flagged ? "bars__row--flagged" : ""}`}
            >
              <span className="bars__label">{d.label}</span>
              <span className="bars__track">
                <span
                  className="bars__fill"
                  style={{ width: `${(w * 100).toFixed(2)}%` }}
                />
              </span>
              <span className="bars__value">
                {known
                  ? asShare
                    ? coveragePercent(d.value, d.value < 0.1 ? 2 : 1)
                    : count(d.value)
                  : "withheld"}
              </span>
              <span className="den bars__den">
                {known
                  ? `n=${count(d.denominator)} of ${count(d.population)}`
                  : "no observations"}
              </span>
              {d.note ? <span className="bars__note">{d.note}</span> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------ composition bar */

export interface Segment {
  key: string;
  label: string;
  value: number;
  color: string;
  /** What this bucket means, in the pipeline's own words. */
  note?: string;
}

/** One bar, its parts labelled. The form for "of everything we tried to
 * measure, how much came back as each kind of answer". */
export function Composition({
  segments,
  total,
  caption,
}: {
  segments: Segment[];
  total: number;
  caption?: string;
}) {
  const prefix = useId().replace(/:/g, "");
  const [hover, setHover] = useState<string | null>(null);
  const colors = segments.map((s) => s.color);
  const shown = segments.filter((s) => s.value > 0);

  let x = 0;
  // The hatch index must follow the segment's position in the ORIGINAL list,
  // not in the filtered one: dropping an empty bucket would otherwise shift
  // every hatch after it onto a different meaning.
  const placed = shown.map((s) => {
    const w = total ? (s.value / total) * 100 : 0;
    const at = x;
    x += w;
    return { ...s, x: at, w, hatch: segments.findIndex((o) => o.key === s.key) };
  });

  return (
    <figure className="composition">
      {caption ? <figcaption className="chart__caption">{caption}</figcaption> : null}

      <svg
        className="composition__bar"
        viewBox="0 0 100 8"
        preserveAspectRatio="none"
        role="img"
        aria-label={placed
          .map((s) => `${s.label} ${count(s.value)}`)
          .join(", ")}
      >
        <HatchDefs prefix={prefix} colors={colors} />
        {placed.map((s) => (
          <rect
            key={s.key}
            x={s.x}
            // A 2px surface gap between adjacent fills, expressed in the
            // viewBox's own units so it survives the non-uniform scale.
            width={Math.max(0, s.w - 0.35)}
            y={0}
            height={8}
            fill={`url(#${prefix}-${s.hatch})`}
            opacity={hover && hover !== s.key ? 0.35 : 1}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
          />
        ))}
      </svg>

      {/* Direct labels, always. The palette is only compliant with them. */}
      <ul className="composition__key">
        {segments.map((s, i) => (
          <li
            key={s.key}
            className={`composition__item ${
              hover && hover !== s.key ? "is-dim" : ""
            }`}
            onMouseEnter={() => setHover(s.key)}
            onMouseLeave={() => setHover(null)}
          >
            <svg className="composition__swatch" viewBox="0 0 14 14" aria-hidden="true">
              <HatchDefs prefix={`${prefix}-k${i}`} colors={[s.color]} hatchAt={i} />
              <rect width="14" height="14" fill={`url(#${prefix}-k${i}-0)`} />
            </svg>
            <span className="composition__name">{s.label}</span>
            <span className="composition__value">{count(s.value)}</span>
            <span className="den composition__share">
              {total ? coveragePercent(s.value / total, 1) : "—"}
            </span>
            {s.note ? <span className="composition__note">{s.note}</span> : null}
          </li>
        ))}
      </ul>
    </figure>
  );
}

/* ---------------------------------------------------------- histogram */

/** A distribution over small integer counts. Used for cluster size, where
 * the shape IS the finding: one enormous bar at 1 and almost nothing after. */
export function Histogram({
  data,
  xLabel,
  caption,
}: {
  data: { members: number; products: number }[];
  xLabel: string;
  caption?: string;
}) {
  const max = Math.max(1, ...data.map((d) => d.products));
  const [hover, setHover] = useState<number | null>(null);

  return (
    <figure className="histogram">
      {caption ? <figcaption className="chart__caption">{caption}</figcaption> : null}
      <div className="histogram__plot">
        {data.map((d) => {
          // Log height: a linear axis makes every bar after the first
          // invisible, which hides the tail this chart exists to show. Said
          // on the axis, not silently.
          const h = Math.log10(d.products + 1) / Math.log10(max + 1);
          return (
            <div
              key={d.members}
              className={`histogram__col ${hover === d.members ? "is-hover" : ""}`}
              onMouseEnter={() => setHover(d.members)}
              onMouseLeave={() => setHover(null)}
            >
              <span className="histogram__count">
                {hover === d.members ? count(d.products) : ""}
              </span>
              <span
                className="histogram__bar"
                style={{ height: `${Math.max(2, h * 100).toFixed(1)}%` }}
              />
              <span className="histogram__tick">{d.members}</span>
            </div>
          );
        })}
      </div>
      <p className="chart__axis">
        {xLabel} · bar height is log-scaled, or every column after the first
        would be invisible
      </p>
    </figure>
  );
}

/* ------------------------------------------------------------- locked */

/** A panel that cannot be drawn yet, saying what it will show and when.
 * Never a placeholder chart, and never zeros. */
export function Locked({
  title,
  needs,
  have,
  shows,
}: {
  title: string;
  needs: number;
  have: number;
  shows: string;
}) {
  const toGo = Math.max(0, needs - have);
  return (
    <div className="locked">
      <h3 className="locked__title">{title}</h3>
      <p className="locked__body">{shows}</p>
      <p className="locked__when">
        Needs {needs} daily snapshots; {have} held.{" "}
        {toGo === 0
          ? "Ready on the next export."
          : `${toGo} more ${toGo === 1 ? "night" : "nights"} to go.`}
      </p>
    </div>
  );
}
