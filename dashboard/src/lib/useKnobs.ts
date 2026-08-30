/* Knob state: the thing the operator actually turns.
 *
 * Three layers, in the same order and with the same precedence as the Python
 * side (`Knobs.load`): the exported defaults, then a saved preset, then an
 * explicit override. The difference is that here the override lives in the
 * URL, so a board worth showing someone can be sent as a link.
 *
 * Only fields that DIFFER from the exported defaults go in the URL. A URL
 * carrying all twelve values would silently pin the two fee terms nobody has
 * decided yet (`import_vat_term_bp` is flagged unverified, `vat_on_fees_bp`
 * is a live business decision), so that a link shared today would keep
 * asserting today's guess long after it was settled.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import type { Knobs } from "./knobs";
import type { KnobsPayload } from "./types";
import { knobDefaults } from "./data";
import { replaceParams, type ViewName } from "./router";

const STORAGE_KEY = "market-dashboard.knobs.v1";

export interface KnobState {
  knobs: Knobs;
  defaults: Knobs;
  /** Fields the operator has moved away from the exported default. */
  overridden: (keyof Knobs)[];
  set: (field: keyof Knobs, value: number) => void;
  reset: () => void;
  resetField: (field: keyof Knobs) => void;
  /** The shareable query string for the current setting. */
  query: URLSearchParams;
}

function readStorage(): Partial<Knobs> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<Knobs>) : {};
  } catch {
    // A private window, or storage disabled. The board works without it.
    return {};
  }
}

function writeStorage(overrides: Partial<Knobs>): void {
  try {
    if (Object.keys(overrides).length === 0) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(overrides));
    }
  } catch {
    /* nothing to do; the URL still carries the setting */
  }
}

/** Read overrides out of a query string, keeping only known integer fields.
 * An unknown key or a non-integer is dropped rather than coerced — a knob
 * silently reading NaN would make every comparison against it false, which
 * is a confident wrong answer. */
function fromParams(
  params: URLSearchParams,
  payload: KnobsPayload,
): Partial<Knobs> {
  const known = new Map(payload.fields.map((f) => [f.field, f]));
  const out: Record<string, number> = {};
  for (const [key, raw] of params) {
    const field = known.get(key as keyof Knobs);
    if (!field) continue;
    const n = Number(raw);
    if (!Number.isFinite(n) || !Number.isInteger(n)) continue;
    out[key] = n;
  }
  return out as Partial<Knobs>;
}

export function useKnobs(
  payload: KnobsPayload,
  view: ViewName,
  params: URLSearchParams,
): KnobState {
  const defaults = useMemo(() => knobDefaults(payload), [payload]);

  // The URL wins on first load — a link someone was sent must show what the
  // sender saw, not what this browser last had open.
  const [overrides, setOverrides] = useState<Partial<Knobs>>(() => {
    const fromUrl = fromParams(params, payload);
    return Object.keys(fromUrl).length ? fromUrl : readStorage();
  });

  const knobs = useMemo(
    () => ({ ...defaults, ...overrides }) as Knobs,
    [defaults, overrides],
  );

  const overridden = useMemo(
    () =>
      (Object.keys(overrides) as (keyof Knobs)[]).filter(
        (k) => overrides[k] !== defaults[k],
      ),
    [overrides, defaults],
  );

  const query = useMemo(() => {
    const q = new URLSearchParams();
    for (const k of overridden) q.set(k, String(knobs[k]));
    return q;
  }, [overridden, knobs]);

  // Keep the address bar in step without pushing a history entry per drag.
  useEffect(() => {
    const merged = new URLSearchParams(params);
    for (const f of payload.fields) merged.delete(f.field);
    for (const [k, v] of query) merged.set(k, v);
    replaceParams(view, merged);
    writeStorage(overrides);
    // `params` is intentionally not a dependency: it is the value this effect
    // writes, and including it would make the effect re-run on its own write.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, view, overrides, payload]);

  const set = useCallback((field: keyof Knobs, value: number) => {
    setOverrides((prev) => ({ ...prev, [field]: value }));
  }, []);

  const resetField = useCallback((field: keyof Knobs) => {
    setOverrides((prev) => {
      const next = { ...prev };
      delete next[field];
      return next;
    });
  }, []);

  const reset = useCallback(() => setOverrides({}), []);

  return { knobs, defaults, overridden, set, reset, resetField, query };
}
