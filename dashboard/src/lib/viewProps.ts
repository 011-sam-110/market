/* The contract every view is built against.
 *
 * One shape, so a view can be written and reviewed without knowing how the
 * bundle was fetched or how the knobs are persisted. A view never fetches
 * the core bundle itself; if it needs one of the two lazy payloads
 * (listings, product metrics) it asks for it through `lazy`.
 */

import type { Bundle } from "./types";
import type { ForecastPayload } from "./forecast";
import type { KnobState } from "./useKnobs";
import type { ListingsPayload } from "./types";
import type { ViewName } from "./router";

export interface LazyPayloads {
  /** The full latest snapshot, 7.2 MB. Resolves once, then cached. */
  listings: () => Promise<ListingsPayload>;
  /** Full `Metrics` per product, 4.1 MB, keyed by product key. */
  productMetrics: () => Promise<Record<string, unknown>>;
  /** The paper forecast record, or null when the export did not write it.
   * Separate from the eBay bundle, so the board loads without it. */
  forecast: () => Promise<ForecastPayload | null>;
  /** What is already in memory, so a view can render without waiting. */
  loaded: { listings: boolean; productMetrics: boolean };
}

export interface ViewProps {
  bundle: Bundle;
  knobState: KnobState;
  params: URLSearchParams;
  setParams: (next: URLSearchParams) => void;
  go: (view: ViewName, params?: URLSearchParams) => void;
  lazy: LazyPayloads;
}
