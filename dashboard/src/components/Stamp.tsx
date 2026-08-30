/* The rubber stamp.
 *
 * A withheld verdict owns its field the way VOID owns a cancelled coupon.
 * The whole reason this exists as a component rather than a colour is that
 * "we did not measure this" is the most important state on the board and the
 * conventional rendering of it — a small grey dash, or worse, a 0 — is
 * exactly the lie the pipeline spent its whole design avoiding.
 *
 * The word is always present. Colour never carries a verdict alone here.
 *
 * `scale` is the size of the figure the stamp stands in for. A stamp set at
 * the micro size while the figure beside it is set at 38px says the opposite
 * of what this component is for: it makes absence the quiet state and
 * presence the loud one. The stamp matches the field it owns.
 */

import type { ReactNode } from "react";

export interface StampProps {
  children: ReactNode;
  tone?: "withheld" | "fail";
  title?: string;
  /** The scale of the figure this stamp replaces. `micro` is the inline
   * default, for a stamp that sits inside a line of type. */
  scale?: "micro" | "figure" | "record";
}

export function Stamp({
  children,
  tone = "withheld",
  title,
  scale = "micro",
}: StampProps) {
  return (
    <span
      className={[
        "stamp",
        tone === "fail" ? "stamp--fail" : "",
        scale === "micro" ? "" : `stamp--${scale}`,
      ]
        .filter(Boolean)
        .join(" ")}
      title={title}
    >
      {children}
    </span>
  );
}
