/* A hash router, hand-rolled, because the whole routing requirement is
 * "seven views and some state that must survive a paste into a chat window".
 *
 * The URL is the shareable artifact: a knob setting that produced an
 * interesting board has to be reproducible by sending someone the link, and
 * the second operator reading this board is exactly the person that matters
 * for. So knob overrides live in the URL, not only in localStorage.
 */

import { useEffect, useState } from "react";

export const VIEWS = [
  "shortlist",
  "console",
  "niches",
  "explore",
  "coverage",
  "trends",
  "forecast",
] as const;

export type ViewName = (typeof VIEWS)[number];

export const VIEW_TITLES: Record<ViewName, string> = {
  shortlist: "Shortlist",
  console: "Knobs",
  niches: "Niches",
  explore: "Listings",
  coverage: "Coverage",
  trends: "Change",
  forecast: "Forecast",
};

/** What each view is for, in one line, shown in the nav on a wide screen.
 * The terminology is this project's own invention; it has to teach itself. */
export const VIEW_BLURBS: Record<ViewName, string> = {
  shortlist: "Today’s ranked candidates. Each one shows its own evidence.",
  console: "Move a threshold. See which knob binds the funnel.",
  niches: "Every leaf and price band. How much of each one the pipeline measured.",
  explore: "Every listing in the latest sweep. Filter on any field.",
  coverage: "What the pipeline measured, and what it did not.",
  trends: "Day-over-day change. What stays locked until more snapshots arrive.",
  forecast: "A paper-money forecaster, scored against the market price.",
};

export interface Route {
  view: ViewName;
  params: URLSearchParams;
}

function parse(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [path = "", query = ""] = raw.split("?");
  const view = (VIEWS as readonly string[]).includes(path)
    ? (path as ViewName)
    : "shortlist";
  return { view, params: new URLSearchParams(query) };
}

export function buildHash(view: ViewName, params?: URLSearchParams): string {
  const q = params?.toString();
  return `#/${view}${q ? `?${q}` : ""}`;
}

export function useRoute(): [Route, (view: ViewName, params?: URLSearchParams) => void] {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));

  useEffect(() => {
    const onHash = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (view: ViewName, params?: URLSearchParams) => {
    const next = buildHash(view, params);
    if (next === window.location.hash) return;
    window.location.hash = next;
  };

  return [route, go];
}

/** Replace the current query string without pushing a history entry.
 *
 * Used for view state that changes as you type — a filter box that pushed a
 * history entry per keystroke would make the back button useless. */
export function replaceParams(view: ViewName, params: URLSearchParams): void {
  const next = buildHash(view, params);
  window.history.replaceState(null, "", next);
}
