/* Explore — every row of the latest sweep, with nothing sampled.
 *
 * One row is one listing. The payload holds 29,863 of them in 40 columns.
 * It is columnar and dictionary-encoded, so this view decodes a cell only
 * when it draws that cell. It never builds 29,863 row objects.
 *
 * The view answers one question before it answers any other: can this
 * column carry a filter at all? A column with one distinct value cannot
 * separate anything. The coverage ladder grades every column, so the ladder
 * decides which columns this view offers as filters. The rest sit in the
 * no-signal panel with the reason.
 *
 * The board prints WITHHELD when a figure has no observations. It prints
 * UNKNOWN when one cell has no value. Those are two different states, so
 * they keep two different words.
 */

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useVirtualizer, useWindowVirtualizer } from "@tanstack/react-virtual";

import { Figure } from "../components/Figure";
import { Stamp } from "../components/Stamp";
import { dictValue } from "../lib/data";
import {
  bpPercent,
  count,
  coveragePercent,
  isoDay,
  money,
  stamp,
} from "../lib/format";
import type {
  ColumnLadderEntry,
  CoveragePayload,
  ListingsPayload,
} from "../lib/types";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/explore.css";

/* ------------------------------------------------------------- columns */

type CellKind =
  | "title"
  | "text"
  | "id"
  | "money"
  | "count"
  | "feedback"
  | "days"
  | "storage"
  | "moment"
  | "dayoffset"
  | "day"
  | "flag";

interface ColSpec {
  key: string;
  label: string;
  width: number;
  kind: CellKind;
  /** What the column holds, in the operator's words. */
  gloss: string;
}

/* The order is a reading order, not the payload order. The columns that
 * identify a row come first. The columns with one value come last. */
const COLUMNS: ColSpec[] = [
  { key: "title", label: "Title", width: 340, kind: "title", gloss: "The listing title, as eBay printed it. It links to the item." },
  { key: "price_pence", label: "Price", width: 110, kind: "money", gloss: "The asking price, without postage." },
  { key: "total_pence", label: "Landed", width: 115, kind: "money", gloss: "The asking price plus postage." },
  { key: "sold_qty", label: "Sold", width: 105, kind: "count", gloss: "Units sold over the life of the listing, as the card states." },
  { key: "seller_id", label: "Seller", width: 175, kind: "text", gloss: "The eBay handle of the seller." },
  { key: "seller_feedback_bp", label: "Feedback", width: 115, kind: "feedback", gloss: "The feedback percentage of the seller." },
  { key: "seller_feedback_n", label: "Ratings", width: 120, kind: "count", gloss: "The number of ratings under that percentage." },
  { key: "seller_type", label: "Seller type", width: 125, kind: "text", gloss: "Business or private. The sweep reads it on the item page." },
  { key: "category", label: "Department", width: 180, kind: "text", gloss: "The department that the sweep works through." },
  { key: "leaf_category_id", label: "Leaf", width: 120, kind: "text", gloss: "The eBay id of the leaf category." },
  { key: "shard", label: "Sweep page", width: 175, kind: "text", gloss: "The page of the sweep that returned this row." },
  { key: "condition", label: "Condition", width: 165, kind: "text", gloss: "The condition, when the card states one." },
  { key: "brand", label: "Brand", width: 150, kind: "text", gloss: "The brand aspect, read on the item page." },
  { key: "model", label: "Model", width: 140, kind: "text", gloss: "The model, when the title parses into one." },
  { key: "storage_gb", label: "Storage", width: 105, kind: "storage", gloss: "The storage size, when the title parses into one." },
  { key: "epid", label: "Catalogue id", width: 130, kind: "id", gloss: "The eBay catalogue id for the product." },
  { key: "gtin", label: "Barcode", width: 140, kind: "id", gloss: "The barcode of the item." },
  { key: "mpn", label: "Part number", width: 130, kind: "id", gloss: "The part number that the manufacturer assigns." },
  { key: "identity_tier", label: "Identity", width: 125, kind: "text", gloss: "How firmly the pipeline identifies the row." },
  { key: "origin_country", label: "Origin", width: 110, kind: "text", gloss: "The country that the item ships from." },
  { key: "extra_ships_from", label: "Ships from", width: 150, kind: "text", gloss: "The ships-from line on the card. It is blank on most rows." },
  { key: "extra_stock_hint", label: "Stock hint", width: 140, kind: "text", gloss: "The scarcity line on the card, such as Last one." },
  { key: "extra_watchers", label: "Watchers", width: 110, kind: "count", gloss: "The watcher count on the card." },
  { key: "extra_aspects_n", label: "Aspects", width: 105, kind: "count", gloss: "How many item aspects the sweep captured." },
  { key: "eta_days", label: "Delivery", width: 115, kind: "days", gloss: "The delivery estimate, at the late end of the range." },
  { key: "qty_available", label: "Quantity", width: 120, kind: "count", gloss: "The quantity the seller offers." },
  { key: "extra_qty_available_is_floor", label: "Quantity is a floor", width: 150, kind: "flag", gloss: "True when eBay caps the quantity it prints." },
  { key: "store_joined", label: "Store opened", width: 130, kind: "day", gloss: "The month the store of the seller opened." },
  { key: "store_items_sold", label: "Store sold", width: 125, kind: "count", gloss: "How many items the store has sold." },
  { key: "listed_at", label: "Listed", width: 130, kind: "dayoffset", gloss: "The day the seller listed the item." },
  { key: "seen_at", label: "Read at", width: 180, kind: "moment", gloss: "The moment the sweep read this row." },
  { key: "source_id", label: "Item number", width: 135, kind: "id", gloss: "The eBay item number. The item URL rests on it." },
  { key: "currency", label: "Currency", width: 105, kind: "text", gloss: "The currency of the price." },
  { key: "region", label: "Region", width: 105, kind: "text", gloss: "The marketplace region." },
  { key: "source", label: "Marketplace", width: 120, kind: "text", gloss: "The marketplace the sweep read." },
  { key: "kind", label: "Row kind", width: 105, kind: "text", gloss: "An ask is a listing. It is not a sale." },
  { key: "available", label: "Live", width: 100, kind: "flag", gloss: "True when the listing was live at the read." },
  { key: "promoted", label: "Promoted", width: 115, kind: "flag", gloss: "True when eBay marks the listing as promoted." },
  { key: "grade", label: "Grade", width: 105, kind: "text", gloss: "The condition grade of a refurbished item." },
  { key: "extra_sold_bound", label: "Sold is a bound", width: 140, kind: "text", gloss: "True when the sold count is a lower bound." },
];

const NUMERIC: ReadonlySet<CellKind> = new Set<CellKind>([
  "money",
  "count",
  "feedback",
  "days",
  "storage",
]);

const RIGHT: ReadonlySet<CellKind> = new Set<CellKind>([
  "money",
  "count",
  "feedback",
  "days",
  "storage",
]);

/* ------------------------------------------------------------ decoding */

/** One cell, decoded on demand.
 *
 * An empty dictionary string is not a value. Two columns carry one, and a
 * blank cell that renders as "" reads as a measured emptiness. It is not
 * one, so this function returns null and the cell prints UNKNOWN. */
function decodeCell(
  p: ListingsPayload,
  col: string,
  i: number,
): string | number | boolean | null {
  const raw = p.columns[col]?.[i];
  if (raw === undefined || raw === null) return null;
  if (p.dict[col]) {
    const text = dictValue(p, col, raw);
    return text === null || text === "" ? null : text;
  }
  return raw;
}

function formatCell(
  spec: ColSpec,
  value: string | number | boolean | null,
  base: number,
): string | null {
  if (value === null) return null;
  switch (spec.kind) {
    case "money":
      return money(value as number);
    case "count":
      return count(value as number);
    case "feedback":
      return bpPercent(value as number, 1);
    case "days": {
      const n = value as number;
      return `${n} ${n === 1 ? "day" : "days"}`;
    }
    case "storage":
      return `${value as number} GB`;
    case "moment":
      return stamp(new Date((base + (value as number)) * 1000).toISOString());
    case "dayoffset":
      return isoDay(
        new Date((base + (value as number)) * 1000).toISOString().slice(0, 10),
      );
    case "day":
      return isoDay(value as string);
    case "flag":
      return value === true ? "yes" : "no";
    default:
      return String(value);
  }
}

/** The item URL. The exporter dropped the `url` column after it checked the
 * template against all 29,863 rows, and `url_exceptions` is empty. */
function itemUrl(p: ListingsPayload, sourceId: string): string {
  const exception = p.url_exceptions[sourceId];
  if (exception) return exception;
  return p.url_template.replace("{source_id}", sourceId);
}

/* ------------------------------------------------------------- profile */

interface ColProfile {
  spec: ColSpec;
  /** Rows that carry a value, counted in this browser. */
  present: number;
  /** Distinct values, counted in this browser. */
  distinct: number;
  /** The only value, when there is exactly one. */
  soleValue: string | null;
  /** Of the rows that carry a value, how many are deepened rows. */
  inDeepened: number;
  ladder: ColumnLadderEntry | null;
  tier2: boolean;
  /** True when the browser count and the ladder count disagree. */
  disputed: boolean;
  /** True when the column cannot separate one row from another. */
  dead: boolean;
  deadReason: string;
}

interface Profile {
  cols: ColProfile[];
  byKey: Map<string, ColProfile>;
  /** One byte per row per column. 1 means the cell carries a value. */
  presence: Record<string, Uint8Array>;
  deepened: Uint8Array;
  nDeepened: number;
  lowerTitle: string[];
  lowerSeller: string[];
  nSellerKnown: number;
  /** True when `region` marks exactly the rows whose origin reads GB. */
  regionEchoesOrigin: boolean;
}

function buildProfile(p: ListingsPayload, cov: CoveragePayload): Profile {
  const n = p.n;
  const ladder = new Map(cov.columns.map((e) => [e.col, e]));
  const tier2 = new Set(cov.tier2_columns);
  const presence: Record<string, Uint8Array> = {};

  // A deepened row is a row the sweep opened. Any tier-2 column marks one.
  const deepened = new Uint8Array(n);

  const cols: ColProfile[] = [];

  for (const spec of COLUMNS) {
    const values = p.columns[spec.key];
    const has = new Uint8Array(n);
    const seen = new Set<string | number | boolean>();
    let present = 0;
    let sole: string | number | boolean | null = null;

    if (values) {
      const table = p.dict[spec.key];
      for (let i = 0; i < n; i++) {
        const raw = values[i];
        let value: string | number | boolean | null;
        if (raw === undefined || raw === null) {
          value = null;
        } else if (table) {
          const text = typeof raw === "number" ? table[raw] : null;
          value = text === undefined || text === null || text === "" ? null : text;
        } else {
          value = raw;
        }
        if (value !== null) {
          has[i] = 1;
          present++;
          seen.add(value);
          if (sole === null) sole = value;
        }
      }
    }

    presence[spec.key] = has;
    if (tier2.has(spec.key)) {
      for (let i = 0; i < n; i++) if (has[i] === 1) deepened[i] = 1;
    }

    const entry = ladder.get(spec.key) ?? null;
    const distinct = seen.size;
    let dead = false;
    let reason = "";
    if (present === 0) {
      dead = true;
      reason = `No row carries a value. The sweep read ${count(n)} rows and filled none of them.`;
    } else if (distinct === 1 && present === n) {
      dead = true;
      reason = `Every one of the ${count(n)} rows reads ${String(sole)}. One value cannot separate one row from another.`;
    } else if (distinct === 1) {
      dead = true;
      reason = `Only one value exists, ${String(sole)}, on ${count(present)} rows. A filter on it can only split the ${count(present)} rows that carry it from the ${count(n - present)} that do not.`;
    }

    cols.push({
      spec,
      present,
      distinct,
      soleValue: sole === null ? null : String(sole),
      inDeepened: 0,
      ladder: entry,
      tier2: tier2.has(spec.key),
      disputed: entry !== null && entry.non_null !== present,
      dead,
      deadReason: reason,
    });
  }

  let nDeepened = 0;
  for (let i = 0; i < n; i++) if (deepened[i] === 1) nDeepened++;

  for (const col of cols) {
    const has = presence[col.spec.key];
    if (!has) continue;
    let inside = 0;
    for (let i = 0; i < n; i++) if (has[i] === 1 && deepened[i] === 1) inside++;
    col.inDeepened = inside;
  }

  // The search box reads two columns, so it lowercases them once. A per
  // keystroke pass over 29,863 titles wastes the operator's time.
  const titles = p.columns["title"] ?? [];
  const lowerTitle = new Array<string>(n);
  for (let i = 0; i < n; i++) {
    const t = titles[i];
    lowerTitle[i] = typeof t === "string" ? t.toLowerCase() : "";
  }
  const lowerSeller = (p.dict["seller_id"] ?? []).map((s) => s.toLowerCase());
  const sellerHas = presence["seller_id"];
  let nSellerKnown = 0;
  if (sellerHas) for (let i = 0; i < n; i++) if (sellerHas[i] === 1) nSellerKnown++;

  // `region` looks like a second reading of the origin. This checks the two
  // row by row, because a count that matches is not the same as a set that
  // matches. The panel states the result only when this holds.
  const regionHas = presence["region"];
  const originCol = p.columns["origin_country"];
  const originDict = p.dict["origin_country"] ?? [];
  let echo = regionHas !== undefined && originCol !== undefined;
  if (echo && regionHas && originCol) {
    for (let i = 0; i < n; i++) {
      const raw = originCol[i];
      const isGB = typeof raw === "number" && originDict[raw] === "GB";
      if ((regionHas[i] === 1) !== isGB) {
        echo = false;
        break;
      }
    }
  }

  return {
    cols,
    byKey: new Map(cols.map((c) => [c.spec.key, c])),
    presence,
    deepened,
    nDeepened,
    lowerTitle,
    lowerSeller,
    nSellerKnown,
    regionEchoesOrigin: echo,
  };
}

/* --------------------------------------------------------------- state */

interface Filters {
  q: string;
  values: Record<string, string[]>;
  has: Record<string, "yes" | "no">;
  min: Record<string, number>;
  max: Record<string, number>;
}

interface ExploreState extends Filters {
  group: string;
  sortKey: string;
  sortDir: "asc" | "desc";
  showEmpty: boolean;
}

const EMPTY_STATE: ExploreState = {
  q: "",
  values: {},
  has: {},
  min: {},
  max: {},
  group: "",
  sortKey: "",
  sortDir: "asc",
  showEmpty: true,
};

const KNOWN_COLUMNS = new Set(COLUMNS.map((c) => c.key));

function parseState(params: URLSearchParams): ExploreState {
  const next: ExploreState = {
    ...EMPTY_STATE,
    values: {},
    has: {},
    min: {},
    max: {},
  };
  next.q = params.get("q") ?? "";
  const group = params.get("g") ?? "";
  if (KNOWN_COLUMNS.has(group)) next.group = group;
  const sort = params.get("sort");
  if (sort) {
    const [key = "", dir = "asc"] = sort.split(":");
    if (KNOWN_COLUMNS.has(key)) {
      next.sortKey = key;
      next.sortDir = dir === "desc" ? "desc" : "asc";
    }
  }
  next.showEmpty = params.get("empty") !== "0";

  for (const [key, raw] of params) {
    const dot = key.indexOf(".");
    if (dot < 0) continue;
    const prefix = key.slice(0, dot);
    const col = key.slice(dot + 1);
    if (!KNOWN_COLUMNS.has(col)) continue;
    if (prefix === "in") {
      const values = raw
        .split("|")
        .map((v) => decodeURIComponent(v))
        .filter((v) => v.length > 0);
      if (values.length) next.values[col] = values;
    } else if (prefix === "has") {
      if (raw === "yes" || raw === "no") next.has[col] = raw;
    } else if (prefix === "min" || prefix === "max") {
      const n = Number(raw);
      // A filter that reads NaN would drop every row and say nothing. The
      // parser refuses the value instead.
      if (Number.isFinite(n)) next[prefix][col] = n;
    }
  }
  return next;
}

function writeState(base: URLSearchParams, s: ExploreState): URLSearchParams {
  const out = new URLSearchParams(base);
  for (const key of [...out.keys()]) {
    if (key === "q" || key === "g" || key === "sort" || key === "empty") {
      out.delete(key);
    } else if (/^(in|has|min|max)\./.test(key)) {
      out.delete(key);
    }
  }
  if (s.q) out.set("q", s.q);
  if (s.group) out.set("g", s.group);
  if (s.sortKey) out.set("sort", `${s.sortKey}:${s.sortDir}`);
  if (!s.showEmpty) out.set("empty", "0");
  for (const [col, values] of Object.entries(s.values)) {
    if (values.length) {
      out.set(`in.${col}`, values.map((v) => encodeURIComponent(v)).join("|"));
    }
  }
  for (const [col, mode] of Object.entries(s.has)) out.set(`has.${col}`, mode);
  for (const [col, v] of Object.entries(s.min)) out.set(`min.${col}`, String(v));
  for (const [col, v] of Object.entries(s.max)) out.set(`max.${col}`, String(v));
  return out;
}

function activeFilterCount(s: ExploreState): number {
  return (
    (s.q ? 1 : 0) +
    Object.keys(s.values).length +
    Object.keys(s.has).length +
    Object.keys(s.min).length +
    Object.keys(s.max).length
  );
}

/* ---------------------------------------------------------- predicates */

type Predicate = (i: number) => boolean;

/** Build the row test for the current filter.
 *
 * `skipValuesFor` leaves out one column's own value filter. The facet list
 * needs the counts that the operator would get after a click, so it must
 * not apply the selection it is about to change. */
function buildPredicates(
  p: ListingsPayload,
  profile: Profile,
  s: Filters,
  skipValuesFor?: string,
): Predicate[] {
  const preds: Predicate[] = [];

  const q = s.q.trim().toLowerCase();
  if (q) {
    const sellerCol = p.columns["seller_id"];
    const sellerDict = profile.lowerSeller;
    const titles = profile.lowerTitle;
    preds.push((i) => {
      if (titles[i]?.includes(q)) return true;
      const raw = sellerCol?.[i];
      if (typeof raw !== "number" || raw < 0) return false;
      return sellerDict[raw]?.includes(q) ?? false;
    });
  }

  for (const [col, values] of Object.entries(s.values)) {
    if (col === skipValuesFor || values.length === 0) continue;
    const column = p.columns[col];
    if (!column) continue;
    const table = p.dict[col];
    if (table) {
      // Dictionary columns compare integers. The index set is built once,
      // so the row test never touches a string.
      const wanted = new Set<number>();
      for (let k = 0; k < table.length; k++) {
        if (values.includes(table[k] ?? "")) wanted.add(k);
      }
      preds.push((i) => {
        const raw = column[i];
        return typeof raw === "number" && wanted.has(raw);
      });
    } else {
      const wanted = new Set(values);
      preds.push((i) => {
        const raw = column[i];
        return raw !== null && raw !== undefined && wanted.has(String(raw));
      });
    }
  }

  for (const [col, mode] of Object.entries(s.has)) {
    const has = profile.presence[col];
    if (!has) continue;
    const want = mode === "yes" ? 1 : 0;
    preds.push((i) => has[i] === want);
  }

  for (const [col, floor] of Object.entries(s.min)) {
    const column = p.columns[col];
    if (!column) continue;
    // A row with no value cannot clear a floor. It fails the test rather
    // than passing as a zero.
    preds.push((i) => {
      const raw = column[i];
      return typeof raw === "number" && raw >= floor;
    });
  }
  for (const [col, ceiling] of Object.entries(s.max)) {
    const column = p.columns[col];
    if (!column) continue;
    preds.push((i) => {
      const raw = column[i];
      return typeof raw === "number" && raw <= ceiling;
    });
  }

  return preds;
}

function selectRows(n: number, preds: Predicate[]): number[] {
  const out: number[] = [];
  outer: for (let i = 0; i < n; i++) {
    for (let k = 0; k < preds.length; k++) {
      const pred = preds[k];
      if (pred && !pred(i)) continue outer;
    }
    out.push(i);
  }
  return out;
}

function medianOf(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  if (sorted.length % 2 === 1) return sorted[mid] ?? null;
  const low = sorted[mid - 1];
  const high = sorted[mid];
  if (low === undefined || high === undefined) return null;
  return (low + high) / 2;
}

/* ---------------------------------------------------------- small bits */

const WIDE = "(min-width: 880px)";

/** True on a screen wide enough for the 40-column sheet. */
function useWide(): boolean {
  return useSyncExternalStore(
    (cb) => {
      const query = window.matchMedia(WIDE);
      query.addEventListener("change", cb);
      return () => query.removeEventListener("change", cb);
    },
    () => window.matchMedia(WIDE).matches,
    () => true,
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [held, setHeld] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setHeld(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return held;
}

/** The sort mark. It is drawn on the same 24 grid as the view icons. */
function SortMark({ dir }: { dir: "asc" | "desc" }) {
  return (
    <svg
      className="explore__th-sort"
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.2}
      strokeLinecap="square"
      aria-hidden="true"
    >
      {dir === "asc" ? <path d="M4 16l8-8 8 8" /> : <path d="M4 8l8 8 8-8" />}
    </svg>
  );
}

function fillLine(present: number, population: number): string {
  if (present === 0) return `n=0 of ${count(population)} · no values`;
  return `n=${count(present)} of ${count(population)} · ${coveragePercent(
    present / population,
    present / population < 0.1 ? 2 : 1,
  )}`;
}

/* ----------------------------------------------------------- the view */

export function Explore(props: ViewProps) {
  const { lazy, bundle } = props;
  const [payload, setPayload] = useState<ListingsPayload | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let live = true;
    lazy
      .listings()
      .then((p) => {
        if (live) setPayload(p);
      })
      .catch((e: unknown) => {
        if (live) setError(e as Error);
      });
    return () => {
      live = false;
    };
  }, [lazy]);

  if (error) {
    return (
      <div className="view">
        <div className="state state--error">
          <h2 className="state__title">The listings file did not load</h2>
          <p className="state__body">{error.message}</p>
          <p className="state__body">
            The file is <code>listings-{bundle.manifest.latest_date}.json</code>
            . The export writes it as one static file. A failure here means the
            file is missing or half written. It does not mean the sweep found
            no rows.
          </p>
        </div>
      </div>
    );
  }

  if (!payload) {
    return (
      <div className="view">
        <div className="state">
          <h2 className="state__title">Fetching the whole sweep</h2>
          <p className="state__body">
            This view reads every row of the{" "}
            {isoDay(bundle.manifest.latest_date)} sweep. That is{" "}
            {count(bundle.manifest.latest.listings)} rows in 40 columns, and
            about 7.2 MB of JSON in one file.
          </p>
          <p className="state__body">
            The sweep is not sampled. The file arrives whole, so the wait is
            the whole file. The board holds it after the first fetch.
          </p>
        </div>
      </div>
    );
  }

  return <Board payload={payload} {...props} />;
}

/* --------------------------------------------------------------- board */

function Board({
  payload,
  bundle,
  params,
  setParams,
}: ViewProps & { payload: ListingsPayload }) {
  const coverage = bundle.coverage;
  const profile = useMemo(
    () => buildProfile(payload, coverage),
    [payload, coverage],
  );

  const [state, setState] = useState<ExploreState>(() => parseState(params));
  const [openColumn, setOpenColumn] = useState<string>("");
  const [typed, setTyped] = useState(state.q);

  // Two debounces with two jobs. The first protects a pass over 29,863
  // rows. The second protects the address bar.
  const settledQuery = useDebounced(typed, 220);
  useEffect(() => {
    setState((prev) => (prev.q === settledQuery ? prev : { ...prev, q: settledQuery }));
  }, [settledQuery]);

  const paramsRef = useRef(params);
  paramsRef.current = params;
  const shareable = useDebounced(state, 350);
  useEffect(() => {
    setParams(writeState(paramsRef.current, shareable));
  }, [shareable, setParams]);

  const matched = useMemo(
    () => selectRows(payload.n, buildPredicates(payload, profile, state)),
    [payload, profile, state],
  );

  const ordered = useMemo(() => {
    if (!state.sortKey) return matched;
    const col = payload.columns[state.sortKey];
    if (!col) return matched;
    // The column spec is not consulted here. `value()` below decides text or
    // number from the cell itself, because a dictionary column always sorts
    // as text and a raw column always sorts as its own type. Reading the spec
    // as well would give two sources of truth for one decision.
    const table = payload.dict[state.sortKey];
    const sign = state.sortDir === "asc" ? 1 : -1;
    const value = (i: number): string | number | null => {
      const raw = col[i];
      if (raw === null || raw === undefined) return null;
      if (table) {
        const text = typeof raw === "number" ? table[raw] : null;
        return text === undefined || text === null || text === "" ? null : text;
      }
      if (typeof raw === "boolean") return raw ? 1 : 0;
      return raw;
    };
    // Rows with no value stay at the end in both directions. An unknown is
    // not the smallest value, so it never leads the sort.
    return [...matched].sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      if (va === null && vb === null) return a - b;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (typeof va === "number" && typeof vb === "number") {
        return (va - vb) * sign;
      }
      return String(va).localeCompare(String(vb)) * sign;
    });
    // `spec` only proves the key names a real column.
  }, [matched, state.sortKey, state.sortDir, payload]);

  /** How many rows in the current filter carry a value, column by column. */
  const here = useMemo(() => {
    const out: Record<string, number> = {};
    for (const col of COLUMNS) {
      const has = profile.presence[col.key];
      if (!has) {
        out[col.key] = 0;
        continue;
      }
      let n = 0;
      for (let k = 0; k < matched.length; k++) {
        const row = matched[k];
        if (row !== undefined && has[row] === 1) n++;
      }
      out[col.key] = n;
    }
    return out;
  }, [matched, profile]);

  const summary = useMemo(() => {
    const price: number[] = [];
    const landed: number[] = [];
    const sold: number[] = [];
    const feedback: number[] = [];
    const cPrice = payload.columns["price_pence"];
    const cLanded = payload.columns["total_pence"];
    const cSold = payload.columns["sold_qty"];
    const cFeedback = payload.columns["seller_feedback_bp"];
    for (const i of matched) {
      const a = cPrice?.[i];
      if (typeof a === "number") price.push(a);
      const b = cLanded?.[i];
      if (typeof b === "number") landed.push(b);
      const c = cSold?.[i];
      if (typeof c === "number") sold.push(c);
      const d = cFeedback?.[i];
      if (typeof d === "number") feedback.push(d);
    }
    return {
      price: { median: medianOf(price), n: price.length },
      landed: { median: medianOf(landed), n: landed.length },
      sold: { median: medianOf(sold), n: sold.length },
      feedback: { median: medianOf(feedback), n: feedback.length },
    };
  }, [matched, payload]);

  /** When nothing matches, name the filter that empties the set. */
  const blame = useMemo(() => {
    if (matched.length > 0) return null;
    const parts: { label: string; clear: () => ExploreState; back: number }[] = [];
    const test = (label: string, next: ExploreState) => {
      const n = selectRows(payload.n, buildPredicates(payload, profile, next)).length;
      if (n > 0) parts.push({ label, clear: () => next, back: n });
    };
    if (state.q) test(`the search for ${state.q}`, { ...state, q: "" });
    for (const col of Object.keys(state.values)) {
      const values = { ...state.values };
      delete values[col];
      test(labelOf(col), { ...state, values });
    }
    for (const col of Object.keys(state.has)) {
      const has = { ...state.has };
      delete has[col];
      test(labelOf(col), { ...state, has });
    }
    for (const col of Object.keys(state.min)) {
      const min = { ...state.min };
      delete min[col];
      test(`the lowest ${labelOf(col)}`, { ...state, min });
    }
    for (const col of Object.keys(state.max)) {
      const max = { ...state.max };
      delete max[col];
      test(`the highest ${labelOf(col)}`, { ...state, max });
    }
    parts.sort((a, b) => b.back - a.back);
    return parts;
  }, [matched.length, payload, profile, state]);

  const wide = useWide();
  const usable = profile.cols.filter((c) => !c.dead);
  const dead = profile.cols.filter((c) => c.dead);
  const disputed = profile.cols.filter((c) => c.disputed);
  const shownColumns = state.showEmpty
    ? profile.cols
    : profile.cols.filter((c) => (here[c.spec.key] ?? 0) > 0);
  const hiddenCount = profile.cols.length - shownColumns.length;
  const filters = activeFilterCount(state);

  const setFilterValue = useCallback((col: string, value: string) => {
    setState((prev) => {
      const values = { ...prev.values };
      const current = values[col] ?? [];
      const next = current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
      if (next.length) values[col] = next;
      else delete values[col];
      return { ...prev, values };
    });
  }, []);

  const setHas = useCallback((col: string, mode: "yes" | "no" | null) => {
    setState((prev) => {
      const has = { ...prev.has };
      if (mode === null || has[col] === mode) delete has[col];
      else has[col] = mode;
      return { ...prev, has };
    });
  }, []);

  const setBound = useCallback(
    (kind: "min" | "max", col: string, value: number | null) => {
      setState((prev) => {
        const bounds = { ...prev[kind] };
        if (value === null) delete bounds[col];
        else bounds[col] = value;
        return { ...prev, [kind]: bounds };
      });
    },
    [],
  );

  const clearAll = useCallback(() => {
    setTyped("");
    setState((prev) => ({
      ...EMPTY_STATE,
      group: prev.group,
      sortKey: prev.sortKey,
      sortDir: prev.sortDir,
      showEmpty: prev.showEmpty,
    }));
  }, []);

  const toggleSort = useCallback((col: string) => {
    setState((prev) => {
      if (prev.sortKey !== col) return { ...prev, sortKey: col, sortDir: "asc" };
      if (prev.sortDir === "asc") return { ...prev, sortDir: "desc" };
      return { ...prev, sortKey: "", sortDir: "asc" };
    });
  }, []);

  return (
    <div className="view view--explore">
      <header className="view__head">
        <h2 className="view__title">
          Every row of the {isoDay(bundle.manifest.latest_date)} sweep
        </h2>
        <p className="view__lede explore__lede">
          One row is one listing. The sweep read {count(payload.n)} of them and
          this view holds all of them. It caps nothing. The filter below reads{" "}
          {usable.length} columns. The other {dead.length} columns carry no
          signal, and the panel at the foot says why for each one.
        </p>
      </header>

      <section className="explore__count" aria-live="polite">
        <b>{count(ordered.length)}</b>
        <span className="den">of {count(payload.n)} rows</span>
        <p className="explore__count-note">
          {filters === 0
            ? "No filter is set. This is the whole sweep."
            : `${filters} ${filters === 1 ? "filter is" : "filters are"} set. The URL carries them, so a link reproduces this exact view.`}
        </p>
      </section>

      <section className="panel">
        <h3 className="panel__title">What this selection can support</h3>
        <p className="panel__note">
          Each figure below carries the count it was measured over. A figure
          with no observations reads WITHHELD. It never reads 0.
        </p>
        <div className="explore__sum">
          <Figure
            className="field"
            name="Median price"
            value={money(summary.price.median)}
            denominator={summary.price.n}
            population={ordered.length}
            note="No row in this selection carries a price."
          />
          <Figure
            className="field"
            name="Median landed"
            value={money(summary.landed.median)}
            denominator={summary.landed.n}
            population={ordered.length}
            note="No row in this selection carries a postage figure, so no landed price exists."
          />
          <Figure
            className="field"
            name="Median sold"
            value={count(summary.sold.median)}
            denominator={summary.sold.n}
            population={ordered.length}
            note="No row in this selection reports a sold count."
          />
          <Figure
            className="field"
            name="Median rival feedback"
            value={bpPercent(summary.feedback.median, 1)}
            denominator={summary.feedback.n}
            population={ordered.length}
            note="No row in this selection carries a seller feedback figure."
          />
        </div>
      </section>

      <section className="panel">
        <h3 className="panel__title">Filter</h3>
        <label className="control" htmlFor="explore-q">
          <span className="control__label">Search the title and the seller</span>
          <input
            id="explore-q"
            className="explore__search"
            type="search"
            value={typed}
            placeholder="iphone, everyexhaustpart"
            onChange={(e) => setTyped(e.target.value)}
          />
          <span className="control__note">
            The search reads two columns. It matches part of a title or part of
            a seller handle. The seller is unknown on{" "}
            {count(payload.n - profile.nSellerKnown)} rows, so a seller search
            cannot reach them.
          </span>
        </label>

        {filters > 0 ? (
          <ul className="explore__chips">
            {state.q ? (
              <li>
                <button
                  type="button"
                  className="explore__chip"
                  onClick={() => setTyped("")}
                >
                  <b>Search</b> {state.q}{" "}
                  <span className="explore__chip--drop">drop</span>
                </button>
              </li>
            ) : null}
            {Object.entries(state.values).map(([col, values]) =>
              values.map((v) => (
                <li key={`${col}:${v}`}>
                  <button
                    type="button"
                    className="explore__chip"
                    onClick={() => setFilterValue(col, v)}
                  >
                    <b>{labelOf(col)}</b> {v}{" "}
                    <span className="explore__chip--drop">drop</span>
                  </button>
                </li>
              )),
            )}
            {Object.entries(state.has).map(([col, mode]) => (
              <li key={`has:${col}`}>
                <button
                  type="button"
                  className="explore__chip"
                  onClick={() => setHas(col, null)}
                >
                  <b>{labelOf(col)}</b>{" "}
                  {mode === "yes" ? "has a value" : "has no value"}{" "}
                  <span className="explore__chip--drop">drop</span>
                </button>
              </li>
            ))}
            {(["min", "max"] as const).map((kind) =>
              Object.entries(state[kind]).map(([col, v]) => (
                <li key={`${kind}:${col}`}>
                  <button
                    type="button"
                    className="explore__chip"
                    onClick={() => setBound(kind, col, null)}
                  >
                    <b>{labelOf(col)}</b> {kind === "min" ? "at least" : "at most"}{" "}
                    {boundText(col, v)}{" "}
                    <span className="explore__chip--drop">drop</span>
                  </button>
                </li>
              )),
            )}
            <li>
              <button type="button" className="btn btn--quiet" onClick={clearAll}>
                Clear every filter
              </button>
            </li>
          </ul>
        ) : null}

        <details open={wide}>
          <summary className="control__label">
            Choose a column to filter. {usable.length} columns carry signal.
          </summary>
          <p className="control__note">
            Each column shows the rows that carry a value, out of{" "}
            {count(payload.n)}. A tier-2 column exists only on the{" "}
            {count(profile.nDeepened)} rows the sweep opened, which is{" "}
            {coveragePercent(profile.nDeepened / payload.n, 2)} of the sweep.
          </p>
          <ul className="explore__fields">
            {usable.map((col) => (
              <li key={col.spec.key}>
                <button
                  type="button"
                  className="explore__field"
                  aria-expanded={openColumn === col.spec.key}
                  onClick={() =>
                    setOpenColumn(openColumn === col.spec.key ? "" : col.spec.key)
                  }
                >
                  <span className="explore__field-name">
                    {col.spec.label}
                    {col.tier2 ? (
                      <span className="explore__tier2">tier 2</span>
                    ) : null}
                  </span>
                  <span className="den">{fillLine(col.present, payload.n)}</span>
                  <span className="explore__field-gloss">{col.spec.gloss}</span>
                </button>
                {openColumn === col.spec.key ? (
                  <ColumnEditor
                    payload={payload}
                    profile={profile}
                    col={col}
                    state={state}
                    onValue={setFilterValue}
                    onHas={setHas}
                    onBound={setBound}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        </details>

        <div className="explore__actions">
          <label>
            <span className="den">Group the rows by</span>{" "}
            <select
              className="explore__select"
              value={state.group}
              onChange={(e) =>
                setState((prev) => ({ ...prev, group: e.target.value }))
              }
            >
              <option value="">No grouping</option>
              {usable.map((c) => (
                <option key={c.spec.key} value={c.spec.key}>
                  {c.spec.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn btn--quiet"
            onClick={() =>
              setState((prev) => ({ ...prev, showEmpty: !prev.showEmpty }))
            }
          >
            {state.showEmpty
              ? "Hide the columns that are empty here"
              : `Show all ${profile.cols.length} columns`}
          </button>
        </div>
        {!state.showEmpty && hiddenCount > 0 ? (
          <p className="explore__foot">
            The sheet hides {hiddenCount} columns. Every row in this selection
            leaves them empty.
          </p>
        ) : null}
      </section>

      {state.group ? (
        <GroupPanel
          payload={payload}
          profile={profile}
          groupKey={state.group}
          rows={matched}
        />
      ) : null}

      {ordered.length === 0 ? (
        <section className="state">
          <h3 className="state__title">No row matches this filter</h3>
          <p className="state__body">
            The sweep holds {count(payload.n)} rows. This filter reaches none of
            them.
          </p>
          {blame && blame.length > 0 ? (
            <ul className="explore__chips">
              {blame.map((b) => (
                <li key={b.label}>
                  <button
                    type="button"
                    className="explore__chip"
                    onClick={() => setState(b.clear())}
                  >
                    <b>Drop {b.label}</b> {count(b.back)} rows return
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="state__body">
              No single filter explains the result. The filters are only empty
              in combination.
            </p>
          )}
          <div className="explore__actions">
            <button type="button" className="btn" onClick={clearAll}>
              Clear every filter
            </button>
          </div>
        </section>
      ) : wide ? (
        <Sheet
          payload={payload}
          columns={shownColumns}
          here={here}
          rows={ordered}
          sortKey={state.sortKey}
          sortDir={state.sortDir}
          onSort={toggleSort}
          total={payload.n}
        />
      ) : (
        <Records payload={payload} profile={profile} rows={ordered} />
      )}

      <section className="panel">
        <h3 className="panel__title">
          No signal: {dead.length} of {profile.cols.length} columns
        </h3>
        <p className="panel__note">
          These columns exist in the payload, so the sheet still prints them.
          The filter does not offer them. A column with one distinct value
          cannot separate one row from another.
        </p>
        <ul className="explore__dead">
          {dead.map((col) => (
            <li key={col.spec.key}>
              <span className="explore__dead-name">{col.spec.label}</span>{" "}
              <Stamp title={col.deadReason}>
                {col.present === 0 ? "empty" : "one value"}
              </Stamp>
              <span className="den">{fillLine(col.present, payload.n)}</span>
              <span className="explore__dead-why">{col.deadReason}</span>
            </li>
          ))}
        </ul>
        {profile.regionEchoesOrigin ? (
          <p className="explore__foot">
            Region reads GB on exactly the rows where Origin reads GB, and it
            is empty on every other row. It repeats Origin. It adds nothing.
          </p>
        ) : null}
        <p className="explore__foot">
          The exporter also dropped {payload.dropped_columns.length} columns
          before this file was written.{" "}
          {payload.dropped_columns.map((d) => `${d.column}: ${d.reason}`).join(". ")}
          .
        </p>
        {disputed.length > 0 ? (
          <div className="explore__disagree">
            <p>
              This browser counted a different number of filled cells from the
              coverage ladder on {disputed.length}{" "}
              {disputed.length === 1 ? "column" : "columns"}. Read the coverage
              view before you trust either count.
            </p>
            <ul className="explore__dead">
              {disputed.map((c) => (
                <li key={c.spec.key}>
                  <span className="explore__dead-name">{c.spec.label}</span>
                  <span className="den">
                    ladder {count(c.ladder?.non_null ?? 0)} · counted here{" "}
                    {count(c.present)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </section>
    </div>
  );
}

function labelOf(col: string): string {
  return COLUMNS.find((c) => c.key === col)?.label ?? col;
}

function boundText(col: string, value: number): string {
  const spec = COLUMNS.find((c) => c.key === col);
  if (!spec) return String(value);
  return formatCell(spec, value, 0) ?? String(value);
}

/* ------------------------------------------------------- column editor */

function ColumnEditor({
  payload,
  profile,
  col,
  state,
  onValue,
  onHas,
  onBound,
}: {
  payload: ListingsPayload;
  profile: Profile;
  col: ColProfile;
  state: ExploreState;
  onValue: (col: string, value: string) => void;
  onHas: (col: string, mode: "yes" | "no" | null) => void;
  onBound: (kind: "min" | "max", col: string, value: number | null) => void;
}) {
  const key = col.spec.key;
  const [narrow, setNarrow] = useState("");
  const table = payload.dict[key];
  const selected = state.values[key] ?? [];

  // The facet counts drop this column's own selection. The operator needs
  // the count a click would produce, not the count the click already made.
  const facets = useMemo(() => {
    if (!table) return null;
    const column = payload.columns[key];
    if (!column) return null;
    const preds = buildPredicates(payload, profile, state, key);
    const rows = selectRows(payload.n, preds);
    const tally = new Int32Array(table.length);
    let unknown = 0;
    for (const i of rows) {
      const raw = column[i];
      if (typeof raw === "number" && raw >= 0 && table[raw] !== undefined && table[raw] !== "") {
        tally[raw] = (tally[raw] ?? 0) + 1;
      } else {
        unknown++;
      }
    }
    const list = table
      .map((label, index) => ({ label, n: tally[index] ?? 0 }))
      .filter((entry) => entry.label !== "")
      .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
    return { list, unknown, population: rows.length };
  }, [payload, profile, state, key, table]);

  const numeric = NUMERIC.has(col.spec.kind);

  const filtered = facets
    ? facets.list.filter(
        (entry) =>
          narrow === "" ||
          entry.label.toLowerCase().includes(narrow.toLowerCase()),
      )
    : [];
  const CAP = 24;
  const shown = filtered.slice(0, CAP);
  const below = filtered.length - shown.length;

  return (
    <div className="explore__facet">
      <p className="control__note">
        {col.spec.gloss} The ladder grades it {col.ladder?.band ?? "outside the ladder"}.
      </p>

      {facets ? (
        <>
          {facets.list.length > CAP ? (
            <input
              className="explore__narrow"
              type="search"
              value={narrow}
              placeholder={`Narrow ${count(facets.list.length)} values`}
              onChange={(e) => setNarrow(e.target.value)}
            />
          ) : null}
          <p className="control__note">
            The counts hold every other filter. They do not hold this column's
            own selection.
          </p>
          <ul className="explore__facet-list">
            {shown.map((entry) => (
              <li key={entry.label}>
                <button
                  type="button"
                  className="explore__facet-row"
                  aria-pressed={selected.includes(entry.label)}
                  onClick={() => onValue(key, entry.label)}
                >
                  <span>{entry.label}</span>
                  <span className="den">
                    {count(entry.n)} of {count(facets.population)}
                  </span>
                </button>
              </li>
            ))}
            {facets.unknown > 0 ? (
              <li>
                <button
                  type="button"
                  className="explore__facet-row explore__facet-row--unknown"
                  aria-pressed={state.has[key] === "no"}
                  onClick={() => onHas(key, "no")}
                >
                  <span>Rows with no value</span>
                  <span className="den">
                    {count(facets.unknown)} of {count(facets.population)}
                  </span>
                </button>
              </li>
            ) : null}
          </ul>
          {below > 0 ? (
            <p className="control__note">
              The list shows {count(shown.length)} values, ordered by row count.{" "}
              {count(below)} values sit below the cut. Type in the box above to
              reach them.
            </p>
          ) : null}
        </>
      ) : null}

      {numeric ? (
        <>
          <div className="explore__range">
            <label>
              <span className="den">At least</span>{" "}
              <input
                type="number"
                value={boundInput(col.spec, state.min[key])}
                onChange={(e) =>
                  onBound("min", key, readBound(col.spec, e.target.value))
                }
              />
            </label>
            <label>
              <span className="den">At most</span>{" "}
              <input
                type="number"
                value={boundInput(col.spec, state.max[key])}
                onChange={(e) =>
                  onBound("max", key, readBound(col.spec, e.target.value))
                }
              />
            </label>
          </div>
          <p className="control__note">
            {col.spec.kind === "money"
              ? "Enter pounds. The payload stores pence."
              : col.spec.kind === "feedback"
                ? "Enter a percentage, such as 99.5."
                : "Enter a whole number."}{" "}
            A row with no value in this column cannot clear a bound, so a bound
            drops the {count(payload.n - col.present)} rows that leave it empty.
          </p>
        </>
      ) : null}

      <div className="explore__actions">
        <button
          type="button"
          className="btn btn--quiet"
          onClick={() => onHas(key, "yes")}
        >
          {state.has[key] === "yes" ? "Stop asking for a value" : "Only rows with a value"}
        </button>
        <button
          type="button"
          className="btn btn--quiet"
          onClick={() => onHas(key, "no")}
        >
          {state.has[key] === "no" ? "Stop asking for an empty cell" : "Only rows with no value"}
        </button>
      </div>
    </div>
  );
}

function boundInput(spec: ColSpec, value: number | undefined): string {
  if (value === undefined) return "";
  if (spec.kind === "money") return String(value / 100);
  if (spec.kind === "feedback") return String(value / 100);
  return String(value);
}

function readBound(spec: ColSpec, raw: string): number | null {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  if (spec.kind === "money" || spec.kind === "feedback") return Math.round(n * 100);
  return n;
}

/* --------------------------------------------------------- group panel */

function GroupPanel({
  payload,
  profile,
  groupKey,
  rows,
}: {
  payload: ListingsPayload;
  profile: Profile;
  groupKey: string;
  rows: number[];
}) {
  const spec = COLUMNS.find((c) => c.key === groupKey);
  const CAP = 200;

  const groups = useMemo(() => {
    const buckets = new Map<string, number[]>();
    const unknown: number[] = [];
    for (const i of rows) {
      const value = decodeCell(payload, groupKey, i);
      if (value === null) {
        unknown.push(i);
        continue;
      }
      const label = String(value);
      const bucket = buckets.get(label);
      if (bucket) bucket.push(i);
      else buckets.set(label, [i]);
    }
    const landed = payload.columns["total_pence"];
    const build = (label: string, members: number[], known: boolean) => {
      const prices: number[] = [];
      for (const i of members) {
        const raw = landed?.[i];
        if (typeof raw === "number") prices.push(raw);
      }
      return {
        label,
        known,
        n: members.length,
        landedN: prices.length,
        landed: medianOf(prices),
      };
    };
    const list = [...buckets.entries()]
      .map(([label, members]) => build(label, members, true))
      .sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
    if (unknown.length) list.push(build("No value", unknown, false));
    return list;
  }, [rows, payload, groupKey]);

  const shown = groups.slice(0, CAP);
  const below = groups.length - shown.length;
  const col = profile.byKey.get(groupKey);

  if (!spec || !col) return null;

  return (
    <section className="panel">
      <h3 className="panel__title">Grouped by {spec.label}</h3>
      <p className="panel__note">
        {count(groups.length)} groups over {count(rows.length)} rows. The rows
        with no value form their own group. This panel never drops them.
      </p>
      <div className="scroll-x">
        <table className="explore__groups">
          <thead>
            <tr>
              <th scope="col">{spec.label}</th>
              <th scope="col">Rows</th>
              <th scope="col">Share of {count(rows.length)}</th>
              <th scope="col">Median landed</th>
              <th scope="col">Measured over</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((g) => (
              <tr key={g.label}>
                <td>
                  {g.known ? (
                    g.label
                  ) : (
                    <span className="explore__unknown">no value</span>
                  )}
                </td>
                <td className="is-num">{count(g.n)}</td>
                <td className="is-num">
                  {coveragePercent(rows.length ? g.n / rows.length : 0, 1)}
                </td>
                <td className="is-num">
                  {g.landedN > 0 ? (
                    money(g.landed)
                  ) : (
                    <span className="explore__unknown">withheld</span>
                  )}
                </td>
                <td className="den">
                  {g.landedN > 0
                    ? `n=${count(g.landedN)} of ${count(g.n)}`
                    : "no observations"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {below > 0 ? (
        <p className="explore__foot">
          The table shows {count(CAP)} groups, ordered by row count.{" "}
          {count(below)} groups sit below the cut and this table does not print
          them.
        </p>
      ) : null}
    </section>
  );
}

/* ---------------------------------------------------------- wide sheet */

const ROW_HEIGHT = 46;

function Sheet({
  payload,
  columns,
  here,
  rows,
  sortKey,
  sortDir,
  onSort,
  total,
}: {
  payload: ListingsPayload;
  columns: ColProfile[];
  here: Record<string, number>;
  rows: number[];
  sortKey: string;
  sortDir: "asc" | "desc";
  onSort: (col: string) => void;
  total: number;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });

  const width = columns.reduce((sum, c) => sum + c.spec.width, 0);
  const filtered = rows.length !== total;

  return (
    <section className="panel">
      <h3 className="panel__title">The sheet</h3>
      <p className="panel__note">
        {count(rows.length)} of {count(total)} rows, in{" "}
        {count(columns.length)} columns. The sheet scrolls inside its own
        frame. Each heading carries the fill rate of its column.
      </p>
      <div className="scroll-x explore__sheet" ref={scrollRef}>
        <div
          className="explore__grid"
          style={{ width }}
          role="table"
          aria-label={`${rows.length} rows of the sweep`}
          aria-rowcount={rows.length}
        >
          <div className="explore__head" role="row">
            {columns.map((col, index) => {
              const present = here[col.spec.key] ?? 0;
              return (
                <button
                  key={col.spec.key}
                  type="button"
                  role="columnheader"
                  aria-sort={
                    sortKey === col.spec.key
                      ? sortDir === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                  className={`explore__th ${index === 0 ? "explore__th--sticky" : ""} ${
                    col.dead ? "explore__th--dead" : ""
                  }`}
                  style={{ width: col.spec.width }}
                  onClick={() => onSort(col.spec.key)}
                  title={col.dead ? col.deadReason : col.spec.gloss}
                >
                  <span className="explore__th-name">
                    {col.spec.label}
                    {sortKey === col.spec.key ? <SortMark dir={sortDir} /> : null}
                  </span>
                  <span className="den">{fillLine(present, rows.length)}</span>
                  {filtered ? (
                    <span className="den">
                      {coveragePercent(
                        col.present / total,
                        col.present / total < 0.1 ? 2 : 1,
                      )}{" "}
                      of all {count(total)}
                    </span>
                  ) : null}
                  {col.tier2 ? (
                    <span className="explore__tier2">tier 2</span>
                  ) : null}
                </button>
              );
            })}
          </div>

          <div
            className="explore__rows"
            style={{ height: virtual.getTotalSize() }}
          >
            {virtual.getVirtualItems().map((item) => {
              const row = rows[item.index];
              if (row === undefined) return null;
              return (
                <SheetRow
                  key={item.key}
                  payload={payload}
                  columns={columns}
                  row={row}
                  top={item.start}
                  band={item.index % 2 === 1}
                />
              );
            })}
          </div>
        </div>
      </div>
      <p className="explore__foot">
        The sheet holds every matching row. It caps nothing. A title is clipped
        to its column here. The record card and the item link carry the whole
        title.
      </p>
    </section>
  );
}

const SheetRow = memo(function SheetRow({
  payload,
  columns,
  row,
  top,
  band,
}: {
  payload: ListingsPayload;
  columns: ColProfile[];
  row: number;
  top: number;
  band: boolean;
}) {
  const base = payload.time_base_epoch;
  const sourceId = payload.columns["source_id"]?.[row];
  const href = typeof sourceId === "string" ? itemUrl(payload, sourceId) : null;

  return (
    <div
      className={`explore__row ${band ? "explore__row--band" : ""}`}
      style={{ top, height: ROW_HEIGHT }}
      role="row"
    >
      {columns.map((col, index) => {
        const value = decodeCell(payload, col.spec.key, row);
        const text = formatCell(col.spec, value, base);
        const numeric = RIGHT.has(col.spec.kind);
        return (
          <div
            key={col.spec.key}
            role="cell"
            className={`explore__cell ${numeric ? "explore__cell--num" : ""} ${
              index === 0 ? "explore__cell--sticky" : ""
            }`}
            style={{ width: col.spec.width }}
            title={text ?? undefined}
          >
            {text === null ? (
              <span className="explore__unknown">unknown</span>
            ) : col.spec.kind === "title" && href ? (
              <a href={href} target="_blank" rel="noreferrer">
                {text}
              </a>
            ) : (
              text
            )}
          </div>
        );
      })}
    </div>
  );
});

/* ------------------------------------------------------- record cards */

function Records({
  payload,
  profile,
  rows,
}: {
  payload: ListingsPayload;
  profile: Profile;
  rows: number[];
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const [margin, setMargin] = useState(0);

  // The page scrolls, not a frame inside it. A phone with a fixed bottom
  // nav has no room for a second scroll region.
  useLayoutEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const update = () => setMargin(el.getBoundingClientRect().top + window.scrollY);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(document.body);
    return () => observer.disconnect();
  }, []);

  const virtual = useWindowVirtualizer({
    count: rows.length,
    estimateSize: () => 260,
    overscan: 4,
    scrollMargin: margin,
  });

  return (
    <section className="panel">
      <h3 className="panel__title">The records</h3>
      <p className="panel__note">
        One card is one row. A card prints the columns that carry a value on
        that row. It names the columns that carry none.
      </p>
      <div
        className="fanfold explore__cards"
        ref={listRef}
        style={{ height: virtual.getTotalSize() }}
      >
        {virtual.getVirtualItems().map((item) => {
          const row = rows[item.index];
          if (row === undefined) return null;
          return (
            <Record
              key={item.key}
              index={item.index}
              dataIndex={item.index}
              payload={payload}
              profile={profile}
              row={row}
              top={item.start - virtual.options.scrollMargin}
              measure={virtual.measureElement}
              band={item.index % 2 === 1}
            />
          );
        })}
      </div>
    </section>
  );
}

const Record = memo(function Record({
  index,
  dataIndex,
  payload,
  profile,
  row,
  top,
  measure,
  band,
}: {
  index: number;
  dataIndex: number;
  payload: ListingsPayload;
  profile: Profile;
  row: number;
  top: number;
  measure: (node: Element | null) => void;
  band: boolean;
}) {
  const base = payload.time_base_epoch;
  const title = payload.columns["title"]?.[row];
  const sourceId = payload.columns["source_id"]?.[row];
  const href = typeof sourceId === "string" ? itemUrl(payload, sourceId) : null;

  const filled: { spec: ColSpec; text: string }[] = [];
  const missing: string[] = [];
  for (const col of profile.cols) {
    if (col.spec.kind === "title") continue;
    const value = decodeCell(payload, col.spec.key, row);
    const text = formatCell(col.spec, value, base);
    if (text === null) missing.push(col.spec.label);
    else filled.push({ spec: col.spec, text });
  }

  const price = decodeCell(payload, "price_pence", row);
  const landed = decodeCell(payload, "total_pence", row);

  return (
    <article
      className={`explore__card ${band ? "explore__card--band" : ""}`}
      style={{ top }}
      data-index={dataIndex}
      ref={measure}
    >
      <div className="explore__card-head">
        <span className="explore__card-n">{count(index + 1)}</span>
        <h4 className="explore__card-title">
          {href ? (
            <a href={href} target="_blank" rel="noreferrer">
              {typeof title === "string" ? title : "This row carries no title"}
            </a>
          ) : typeof title === "string" ? (
            title
          ) : (
            "This row carries no title"
          )}
        </h4>
      </div>

      <div className="explore__card-price">
        <Figure
          className="field"
          name="Price"
          value={money(typeof price === "number" ? price : null)}
          denominator={typeof price === "number" ? 1 : 0}
          population={1}
          note="This row carries no price."
        />
        <Figure
          className="field"
          name="Landed"
          value={money(typeof landed === "number" ? landed : null)}
          denominator={typeof landed === "number" ? 1 : 0}
          population={1}
          note="This row carries no postage figure, so it has no landed price."
        />
      </div>

      <dl className="explore__card-fields">
        {filled
          .filter((f) => f.spec.key !== "price_pence" && f.spec.key !== "total_pence")
          .map((f) => (
            <div className="explore__card-field" key={f.spec.key}>
              <dt>{f.spec.label}</dt>
              <dd>{f.text}</dd>
            </div>
          ))}
      </dl>

      <details className="explore__card-missing">
        <summary>
          {count(missing.length)} of {count(profile.cols.length)} columns are
          unknown here
        </summary>
        <p>{missing.join(", ")}.</p>
      </details>

      {href ? (
        <a
          className="explore__card-link"
          href={href}
          target="_blank"
          rel="noreferrer"
        >
          Open item {typeof sourceId === "string" ? sourceId : ""} on eBay
        </a>
      ) : null}
    </article>
  );
});
