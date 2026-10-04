/* The paper forecast record: its payload shape and its loader.
 *
 * `scripts/export_forecast.py` writes `data/forecast.json` from the ledger
 * on provenance-1. The file is separate from the eBay bundle on purpose. The
 * eBay board must load when no forecast record exists, and the forecast view
 * must say "not exported" rather than show an empty record.
 *
 * Every figure here is the scorer's own (forecast/score.py build_report).
 * The browser computes no score. A `withheld: true` figure renders as a
 * stamp, with its measured value one step down, because the record needs
 * 100 resolved forecasts before any comparison means anything.
 */

export interface ScoreFigure {
  value: number | null;
  n: number;
  withheld: boolean;
  note: string;
}

export interface ForecastCounts {
  forecasts: number;
  open: number;
  resolved: number;
  void: number;
  staked: number;
  no_stake: number;
  sports: number;
  with_reddit: number;
  with_news: number;
  calls_today: number;
  calls_cap: number;
  calls_day: string;
  calls_total: number;
  calls_ok_total: number;
}

export interface ForecastReport {
  generated_at: string;
  ledger_path: string;
  withhold_below: number;
  counts: ForecastCounts;
  brier: {
    agent: ScoreFigure;
    market: ScoreFigure;
    gap: ScoreFigure;
    agent_closer: { value: number; n: number };
  };
  paper: {
    capital: number;
    open_stake: number;
    free: number;
    pnl: ScoreFigure;
    staked_settled: number;
    wins: number;
    losses: number;
    flat: number;
    fees_modelled: boolean;
  };
}

export interface ForecastEvidence {
  kind: string | null;
  url: string | null;
  title: string | null;
  used: boolean;
}

export interface ForecastRow {
  market_id: string;
  question: string;
  outcome_a: string;
  outcome_b: string;
  market_url: string | null;
  end_date: string | null;
  created_at: string;
  market_price: number;
  probability: number;
  side: "a" | "b" | "none";
  stake: number;
  status: "open" | "resolved" | "void";
  final_price_a: number | null;
  pnl: number | null;
  is_sports: boolean;
  reddit_status: string;
  news_status: string;
  evidence: ForecastEvidence[];
}

export interface ForecastPayload {
  payload_version: number;
  git_sha: string;
  report: ForecastReport;
  recent: ForecastRow[];
  recent_limit: number;
  recent_dropped: number;
}

/** The payload, or null when the export has not written it (a 404).
 *
 * Null is a named state, not an empty record. Any other failure throws, so
 * a half-written file never renders as "no forecasts yet". */
export async function loadForecast(): Promise<ForecastPayload | null> {
  const res = await fetch(`${import.meta.env.BASE_URL}data/forecast.json`, {
    cache: "no-cache",
  });
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new Error(
      `The server returned ${res.status} for forecast.json. ` +
        `scripts/export_forecast.py writes it.`,
    );
  }
  const text = await res.text();
  // A dev server answers a missing file with index.html and a 200.
  if (text.trimStart().startsWith("<")) return null;
  return JSON.parse(text) as ForecastPayload;
}

/** A probability or a price, 0..1, as a percentage with one decimal. */
export function pct(x: number | null | undefined): string | null {
  if (x === null || x === undefined) return null;
  return `${(x * 100).toFixed(1)}%`;
}

/** Paper dollars, signed when asked. The unit is never converted. */
export function paper(x: number | null | undefined, signed = false): string | null {
  if (x === null || x === undefined) return null;
  const body = Math.abs(x).toFixed(2);
  if (!signed) return `${x < 0 ? "−" : ""}${body}`;
  return `${x > 0 ? "+" : x < 0 ? "−" : "±"}${body}`;
}
