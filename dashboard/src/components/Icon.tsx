/* Drawn icons, one stroke weight, one grid.
 *
 * Six marks for six views. They are authored rather than pulled from a
 * library so they can carry the world's own vocabulary — a fanfold sheet, a
 * ruled field, a load path, a stamp — instead of the generic chart/table/gear
 * set every dashboard ships. Emoji would be the other cheap route and are
 * not an icon system.
 *
 * All paths are drawn on a 24 grid at 1.6 stroke, square-cut, no fills.
 */

export type IconName =
  | "shortlist"
  | "console"
  | "niches"
  | "explore"
  | "coverage"
  | "trends";

const PATHS: Record<IconName, JSX.Element> = {
  // a fanfold sheet with a ruled entry on it
  shortlist: (
    <>
      <path d="M4 4h16v16H4z" />
      <path d="M4 9h16M4 15h16" />
      <path d="M8 4v16" />
    </>
  ),
  // a load path: three struts, the middle one carrying
  console: (
    <>
      <path d="M4 19V9M12 19V5M20 19v-7" />
      <path d="M2 21h20" />
      <path d="M9 12h6" />
    </>
  ),
  // leaf × band: a grid split by a price cut
  niches: (
    <>
      <path d="M3 5h18v14H3z" />
      <path d="M3 12h18" />
      <path d="M14 5v14" />
    </>
  ),
  // a stack of rows with a magnifier over it
  explore: (
    <>
      <path d="M3 6h12M3 11h8M3 16h6" />
      <circle cx="17" cy="14" r="4" />
      <path d="M20 17l2 2" />
    </>
  ),
  // a measured bar and the gap it did not reach
  coverage: (
    <>
      <path d="M3 8h9M3 16h5" />
      <path d="M21 4v16" />
      <path d="M14 8h7M10 16h11" strokeDasharray="2 2.5" />
    </>
  ),
  // a step change between two days
  trends: (
    <>
      <path d="M3 17l5-5 4 3 5-8 4 4" />
      <path d="M3 21h18" />
    </>
  ),
};

export function Icon({
  name,
  size = 20,
}: {
  name: IconName;
  size?: number;
}) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="square"
      strokeLinejoin="miter"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
