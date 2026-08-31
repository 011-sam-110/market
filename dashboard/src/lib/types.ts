/* The shape of the JSON bundle, as `scripts/export_dashboard.py` writes it.
 *
 * These are declarations, not validation. The one thing worth stating up
 * front, because it governs how every figure on this board is rendered:
 *
 *   A denominator of 0 means UNKNOWN. It never means a real zero.
 *
 * The exporter guarantees that every value field ships beside its own
 * denominator, so there is no figure on this board that can legitimately be
 * drawn without one. If you find yourself rendering a number and reaching for
 * a denominator that is not right there in the same record, you are about to
 * print something the pipeline cannot support.
 */

import type { KnobName, Knobs } from "./knobs";

export type { KnobName, Knobs };

/** A knob verdict as the exporter ships it: `passed: null` is unknown. */
export interface ExportedKnob {
  knob: KnobName;
  value: number | null | (number | null)[];
  threshold: number | number[];
  passed: boolean | null;
  denominator: number;
  coverage: number;
  note: string;
  /** Convenience flag the exporter adds; always `passed === null`. */
  unknown?: boolean;
}

/* ------------------------------------------------------------- manifest */

export interface Manifest {
  schema_version: number;
  source: string;
  store_path: string;
  git_sha: string;
  generated_at: string;
  empty: boolean;
  dates: string[];
  n_dates: number;
  latest_date: string;
  latest: { listings: number; denominator_field: string };
  counts_by_date: { date: string; listings: number; rows: number }[];
  has_difference: boolean;
  difference_window: {
    date_a: string;
    date_b: string;
    n_listings: number;
    n_known: number;
    coverage: number;
  } | null;
  in_scope_leaves: string[];
  n_in_scope_leaves: number;
  unlocks: {
    unlocks: string;
    at_dates: number;
    dates_to_go: number;
    state: "live" | "locked";
  }[];
}

/* ---------------------------------------------------------------- knobs */

export type Provenance = "measured" | "chosen" | "unverified" | "decision";

export interface KnobField {
  field: keyof Knobs;
  label: string;
  /** 1-6 for the six knobs; null for a fee term that is not a knob. */
  knob: number | null;
  unit: "basis_points" | "pence" | "days" | "count";
  value: number;
  default: number;
  min: number;
  max: number;
  step: number;
  env_var: string;
  note: string;
  provenance: Provenance;
}

export interface KnobsPayload {
  fields: KnobField[];
  knob_names: KnobName[];
  sell_through_window: {
    target_days: number;
    min_comparable_fraction: number;
    min_comparable_days: number;
    note: string;
  };
}

/* ----------------------------------------------------------------- fees */

export interface FeesPayload {
  everything_else_bp: number;
  regulatory_bp: number;
  per_order_pence_low: number;
  per_order_pence_high: number;
  per_order_threshold_pence: number;
  fvf_bp_by_leaf: Record<string, number>;
  fvf_bp_by_top_level_name: Record<string, number>;
  top_level_id_to_name: Record<string, string>;
  leaves: string[];
  leaves_unresolved: string[];
  n_leaves: number;
  n_leaves_unresolved: number;
  note: string;
}

/* --------------------------------------------------------------- niches */

export interface NicheRow {
  key: string;
  label: string;
  leaf_category_id: string;
  band: string;
  level: string;
  identity_tier: string;
  score: number;
  n_listings: number;
  n_sellers: number;
  n_business: number;
  n_private: number;
  n_passed: number;
  n_failed: number;
  n_unknown: number;
  sold_known: number;
  str_source: string;
  max_cost_at_floor_pence: number | null;
  max_cost_at_median_pence: number | null;
  knobs: ExportedKnob[];
  metrics: Record<string, number | null>;
}

export interface NichesPayload {
  n: number;
  limit: number;
  capped: boolean;
  in_scope_leaves: string[];
  note: string;
  rows: NicheRow[];
}

/* ------------------------------------------------------- products (columnar) */

/** One knob's column block. `threshold_constant` is present exactly when the
 * threshold was identical on every row, in which case the per-row array is
 * omitted — see the payload's own `knobs.note`. */
export interface KnobColumns {
  value: (number | null | (number | null)[])[];
  passed: (boolean | null)[];
  denominator: number[];
  coverage: number[];
  note_idx: number[];
  threshold_constant: number | number[] | null;
  threshold?: (number | number[])[];
}

export interface ProductsPayload {
  encoding: "columnar";
  n: number;
  n_leaves: number;
  limit: number;
  capped: boolean;
  null_index: number;
  float_dp: number;
  note: string;
  knob_names: KnobName[];
  scalar_fields: string[];
  dict: Record<string, string[]>;
  columns: Record<string, (number | string | null)[]>;
  knobs: {
    by_knob: Record<KnobName, KnobColumns>;
    notes: string[];
    note: string;
    null_index: number;
  };
  clusters: {
    n_products: number;
    n_singletons: number;
    n_two_plus: number;
    n_five_plus: number;
    singleton_share: number;
    note: string;
    histogram: { members: number; products: number }[];
  };
}

/** A product decoded out of the columnar payload, one row at a time. */
export interface ProductRow {
  index: number;
  key: string;
  label: string;
  leaf_category_id: string;
  identity_tier: string;
  str_source: string;
  score: number;
  n_listings: number;
  n_members: number;
  n_sellers: number;
  n_business: number;
  n_private: number;
  n_passed: number;
  n_failed: number;
  n_unknown: number;
  sold_known: number;
  max_cost_at_floor_pence: number | null;
  max_cost_at_median_pence: number | null;
  knobs: ExportedKnob[];
}

/* ------------------------------------------------------------- coverage */

export interface CoveragePanel {
  id: string;
  title: string;
  state: "live" | "unavailable" | "error";
  note: string;
  data: unknown;
}

export interface DepartmentCoverage {
  category: string;
  n: number;
  seller_cov: number;
  feedback_cov: number;
  landed_cov: number;
  eta_cov: number;
  origin_cov: number;
  sold_cov: number;
  condition_cov: number;
}

export interface ColumnLadderEntry {
  col: string;
  non_null: number;
  fill: number;
  distinct: number;
  band: string;
  tier: "tier1_search_row" | "tier2_deepen" | "derived" | "dead";
}

export interface CoveragePayload {
  latest_date: string;
  n_listings: number;
  note: string;
  density_min_seller_coverage: number;
  panels: CoveragePanel[];
  columns: ColumnLadderEntry[];
  tier2_columns: string[];
  call_outs: {
    global_seller_coverage: number;
    per_department_seller_coverage: DepartmentCoverage[];
    worst_department: DepartmentCoverage | null;
    green_because_broken: unknown;
    constants_and_contradictions: unknown;
    deepen_budget: {
      n: number;
      deepened: number;
      business: number;
      private: number;
      business_rate: number;
      leaves_swept: number;
      leaves_touched: number;
      n_niches: number;
      /** Business listings wanted for each leaf. 30 today. */
      target: number;
      needed_business: number;
      needed_listings: number;
      multiple: number;
      /** Niches whose knob denominators are all 0. The board must not read
       * this as 34 niches that scored zero. It is 34 niches nobody measured. */
      zero_denominator: number;
      closing: string;
      rows: Record<string, number | string>[];
    };
  };
}

/* --------------------------------------------------------------- deltas */

export type DeltaBucket =
  | "unknown"
  | "bounded_refused"
  | "known_negative"
  | "known_positive";

export interface DeltaCounts {
  unknown: number;
  bounded_refused: number;
  known_negative: number;
  known_positive: number;
}

export interface DeltaAggregate {
  key: string;
  counts: DeltaCounts;
  sum_delta_known_positive: number;
  sum_delta_known_negative: number;
}

/* Whether two nights swept the same population, read off each night's own
 * sweep checkpoint rather than off the config that asked for it. The config
 * says what was requested. The checkpoint says what happened, including the
 * shards eBay blocked, and those are exactly the nights that differ.
 *
 * `comparable: false` does NOT mean the difference is wrong. It means the
 * difference measures the sweep as well as the market, so no figure derived
 * from it may be presented as change over time. */
export interface Comparability {
  date_a: string;
  date_b: string;
  comparable: boolean;
  leaves_added: string[];
  leaves_removed: string[];
  depth_changed: Record<string, [number, number]>;
  /* Sweep settings that moved between the two nights — the results view
   * above all, which changes what a page returns without touching a single
   * shard key. A gallery card carries no seller line; a list card does. */
  sweep_changed: Record<string, [unknown, unknown]>;
  note: string;
}

export interface DeltasPayload {
  state: "live" | "unavailable";
  /* null when the build could not read a verdict at all, which is not the
   * same as a clean one and must never render as one. */
  population_comparable: Comparability | null;
  date_a: string;
  date_b: string;
  window_days: number;
  n_listings: number;
  n_known: number;
  coverage: number;
  buckets: DeltaBucket[];
  bucket_precedence: string;
  counts: DeltaCounts;
  note: string;
  by_niche: DeltaAggregate[];
  /* The per-listing arrays and the per-product aggregates live in their own
   * file. They are the two parts of this payload that grow with the
   * population, only the Change view reads them, and the board loads this
   * file before it paints anything. */
  detail_file: string;
  n_detail_listings: number;
  n_detail_products: number;
  aggregates_omitted: { niche: number; product: number; reason: string };
  sell_through_comparable: {
    comparable: boolean;
    window_days: number;
    target_days: number;
    min_comparable_days: number;
    note: string;
  };
}

/* The heavy half of one day-pair difference, fetched only by the Change
 * view. Every count in `DeltasPayload` is computed over the whole
 * population, so nothing here is a denominator for anything there. */
export interface DeltaDetailPayload {
  state: "live" | "locked" | "unavailable";
  date_a: string;
  date_b: string;
  note: string;
  by_product: DeltaAggregate[];
  listings: {
    source_id: string[];
    sold_a: (number | null)[];
    sold_b: (number | null)[];
    delta_sold: (number | null)[];
    days: (number | null)[];
    bucket: number[];
  };
}

/* ------------------------------------------------------------- listings */

export interface ListingsPayload {
  n: number;
  null_index: number;
  time_base_epoch: number;
  url_template: string;
  url_exceptions: Record<string, string>;
  note: string;
  row_fields: string[];
  dict: Record<string, string[]>;
  columns: Record<string, (number | string | boolean | null)[]>;
  dropped_columns: { column: string; reason: string }[];
}

/* ---------------------------------------------------------------- bundle */

export interface Bundle {
  manifest: Manifest;
  knobs: KnobsPayload;
  fees: FeesPayload;
  niches: NichesPayload;
  products: ProductsPayload;
  coverage: CoveragePayload;
  deltas: DeltasPayload | null;
}
