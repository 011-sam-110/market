/* The rubber stamp.
 *
 * A withheld verdict owns its field the way VOID owns a cancelled coupon.
 * The whole reason this exists as a component rather than a colour is that
 * "we did not measure this" is the most important state on the board and the
 * conventional rendering of it — a small grey dash, or worse, a 0 — is
 * exactly the lie the pipeline spent its whole design avoiding.
 *
 * The word is always present. Colour never carries a verdict alone here.
 */

import type { ReactNode } from "react";

export interface StampProps {
  children: ReactNode;
  tone?: "withheld" | "fail";
  title?: string;
}

export function Stamp({ children, tone = "withheld", title }: StampProps) {
  return (
    <span
      className={`stamp ${tone === "fail" ? "stamp--fail" : ""}`}
      title={title}
    >
      {children}
    </span>
  );
}
