/* Write the current knob setting somewhere the nightly run can read it.
 *
 * The loop this closes: the operator turns knobs in the browser, publishes
 * the setting, and tonight's sweep ranks against it — instead of the setting
 * living only in one browser's localStorage while the pipeline keeps using
 * whatever `Knobs()` defaults to.
 *
 * The Python end is `scripts/fetch_preset.py`, which runs as step 0 of
 * run_daily, validates the envelope with `Knobs.parse_preset`, and writes
 * `~/.phoneloop/knobs.json`. Every failure path there exits 0 and leaves the
 * previous file alone: a preset that cannot be fetched must never stop a
 * sweep.
 *
 * Deliberately small. This is the only server-side code in the project, and
 * it stays that way — everything else is static files. It validates, it
 * writes one blob, it returns. It reads nothing back and computes nothing,
 * because a second place that computes a knob is a second place that can
 * disagree about one.
 */

import { put } from "@vercel/blob";

export const config = { runtime: "nodejs" };

/** The twelve knob fields, with the bounds the exporter published.
 *
 * Duplicated here rather than imported from `src/lib` on purpose: this file
 * runs on the server and must not be able to accept a value the Python side
 * would refuse. `Knobs.parse_preset` refuses an unknown key and a non-int,
 * and so does this. Where the two disagree, Python wins — it is the one that
 * actually ranks. */
const BOUNDS: Record<string, { min: number; max: number }> = {
  min_margin_pct_bp: { min: 0, max: 10000 },
  min_margin_fixed_pence: { min: 0, max: 5000 },
  str_threshold_bp: { min: 0, max: 20000 },
  price_from_pence: { min: 0, max: 20000 },
  price_to_pence: { min: 0, max: 20000 },
  max_eta_days: { min: 1, max: 30 },
  max_competitors: { min: 1, max: 200 },
  min_seller_feedback_bp: { min: 0, max: 10000 },
  min_seller_feedback_n: { min: 0, max: 1000 },
  vat_on_fees_bp: { min: 10000, max: 12000 },
  ad_rate_bp: { min: 0, max: 1500 },
  import_vat_term_bp: { min: 0, max: 2000 },
};

const BLOB_PATH = "knobs/current.json";
const MAX_BODY_BYTES = 8 * 1024;

interface Preset {
  schema_version: number;
  knobs: Record<string, number>;
}

function reject(reason: string, status = 400): Response {
  return new Response(JSON.stringify({ ok: false, error: reason }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function validate(body: unknown): { preset: Preset } | { error: string } {
  if (typeof body !== "object" || body === null) {
    return { error: "the body must be a JSON object" };
  }
  const envelope = body as Record<string, unknown>;

  if (envelope.schema_version !== 1) {
    return {
      error: `schema_version must be 1, got ${JSON.stringify(envelope.schema_version)}`,
    };
  }
  const knobs = envelope.knobs;
  if (typeof knobs !== "object" || knobs === null || Array.isArray(knobs)) {
    return { error: "knobs must be an object" };
  }

  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(knobs as Record<string, unknown>)) {
    const bound = BOUNDS[key];
    if (!bound) {
      // Refused rather than dropped. A silently ignored key would let the
      // browser believe it published a setting the pipeline never saw.
      return { error: `unknown knob ${JSON.stringify(key)}` };
    }
    // `typeof true === "boolean"`, but JSON `true` would coerce to 1 and
    // read as a plausible knob value. Python refuses a bool here for the
    // same reason.
    if (typeof value !== "number" || !Number.isInteger(value)) {
      return { error: `${key} must be an integer, got ${JSON.stringify(value)}` };
    }
    if (value < bound.min || value > bound.max) {
      return {
        error: `${key} must be between ${bound.min} and ${bound.max}, got ${value}`,
      };
    }
    out[key] = value;
  }

  if (Object.keys(out).length === 0) {
    return { error: "the preset sets no knobs" };
  }

  // The one cross-field rule the pipeline has. An inverted band matches
  // nothing at all, which reads on the board as "the market is empty".
  const from = out.price_from_pence;
  const to = out.price_to_pence;
  if (from !== undefined && to !== undefined && from > to) {
    return { error: `price_from_pence (${from}) is above price_to_pence (${to})` };
  }

  return { preset: { schema_version: 1, knobs: out } };
}

export default async function handler(request: Request): Promise<Response> {
  if (request.method !== "POST") {
    return reject("POST a preset envelope to this endpoint", 405);
  }
  if (!process.env.BLOB_READ_WRITE_TOKEN) {
    // Named, not generic. Without this the operator sees "publish failed"
    // and has no way to know the cause is one missing project setting.
    return reject(
      "BLOB_READ_WRITE_TOKEN is not set on this deployment, so there is " +
        "nowhere to write the preset. Add a Vercel Blob store to the project.",
      503,
    );
  }

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) {
    return reject(`the body is ${raw.length} bytes; the cap is ${MAX_BODY_BYTES}`, 413);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return reject("the body is not valid JSON");
  }

  const checked = validate(parsed);
  if ("error" in checked) return reject(checked.error);

  const blob = await put(BLOB_PATH, JSON.stringify(checked.preset, null, 2), {
    access: "public",
    contentType: "application/json",
    // One path, overwritten. A history of presets would be a second source
    // of truth about what tonight ranks against.
    allowOverwrite: true,
    addRandomSuffix: false,
  });

  return new Response(
    JSON.stringify({
      ok: true,
      url: blob.url,
      knobs: Object.keys(checked.preset.knobs).length,
      note:
        "Tonight's run reads this through scripts/fetch_preset.py. An " +
        "explicit PHONELOOP_* environment variable still beats it.",
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}
