/* The forecast record: a paper-money forecaster, scored against the market.
 *
 * This view has the same job as the rest of the board. It exposes evidence.
 * It does not recommend a bet. The forecaster places no order, holds no
 * wallet and reads only Polymarket's public API. Every stake on this page is
 * a paper number.
 *
 * The comparison is the agent's Brier score against the market price's Brier
 * score, over resolved markets. Below 100 resolved forecasts every comparison
 * renders as a stamp, with its measured value one step down and its n under
 * it. The counts always print, because a count is never a verdict.
 */

import { useEffect, useState } from "react";

import { Figure } from "../components/Figure";
import { count, stamp } from "../lib/format";
import {
  paper,
  pct,
  type ForecastPayload,
  type ForecastRow,
  type ScoreFigure,
} from "../lib/forecast";
import type { ViewProps } from "../lib/viewProps";

import "../styles/views/forecast.css";

type Load =
  | { state: "loading" }
  | { state: "missing" }
  | { state: "error"; message: string }
  | { state: "ready"; payload: ForecastPayload };

export function Forecast({ lazy }: ViewProps) {
  const [load, setLoad] = useState<Load>({ state: "loading" });

  useEffect(() => {
    let live = true;
    lazy
      .forecast()
      .then((p) => live && setLoad(p ? { state: "ready", payload: p } : { state: "missing" }))
      .catch((e: unknown) => live && setLoad({ state: "error", message: (e as Error).message }));
    return () => {
      live = false;
    };
  }, [lazy]);

  return (
    <div className="view forecast">
      <header className="view__head">
        <h2 className="view__title">Forecast record</h2>
        <p className="view__lede">
          A paper-money forecaster. It reads open two-outcome markets from
          Polymarket&rsquo;s public API, makes at most 20 DeepSeek forecasts a
          day with Reddit and news evidence, and scores each forecast against
          the market price when the market resolves. It places no order.
        </p>
      </header>
      {load.state === "loading" ? (
        <div className="state">
          <h3 className="state__title">Reading the forecast record</h3>
        </div>
      ) : load.state === "missing" ? (
        <div className="state">
          <h3 className="state__title">This export holds no forecast record</h3>
          <p className="state__body">
            <code>scripts/export_forecast.py</code> writes{" "}
            <code>data/forecast.json</code> from the ledger. The ledger lives on
            provenance-1. A missing file means nobody ran the export. It does
            not mean the forecaster made no forecasts.
          </p>
        </div>
      ) : load.state === "error" ? (
        <div className="state state--error">
          <h3 className="state__title">The forecast record did not load</h3>
          <p className="state__body">{load.message}</p>
        </div>
      ) : (
        <Record payload={load.payload} />
      )}
    </div>
  );
}

function scoreValue(f: ScoreFigure, digits: number, signed = false): string | null {
  if (f.value === null) return null;
  const s = f.value.toFixed(digits);
  return signed && f.value > 0 ? `+${s}` : s;
}

function Record({ payload }: { payload: ForecastPayload }) {
  const { report } = payload;
  const c = report.counts;
  const b = report.brier;
  const p = report.paper;
  const need = report.withhold_below;
  const why = `${count(c.resolved)} of ${count(c.forecasts)} forecasts have resolved. The comparison needs ${count(need)}.`;

  return (
    <>
      <p className="forecast__exported">
        Exported {stamp(report.generated_at)}. Comparisons open at {count(need)}{" "}
        resolved forecasts: {count(c.resolved)} so far.
      </p>

      <section className="panel forecast__readings" aria-label="The score">
        <Figure
          name="Agent Brier"
          value={scoreValue(b.agent, 4)}
          denominator={b.agent.n}
          population={c.forecasts}
          withheld={b.agent.withheld}
          note={why}
        />
        <Figure
          name="Market Brier"
          value={scoreValue(b.market, 4)}
          denominator={b.market.n}
          population={c.forecasts}
          withheld={b.market.withheld}
          note={why}
        />
        <Figure
          name="Gap (market − agent)"
          value={scoreValue(b.gap, 4, true)}
          denominator={b.gap.n}
          population={c.forecasts}
          withheld={b.gap.withheld}
          note={`${why} Above 0 means the agent scored better.`}
        />
        <Figure
          name="Paper P&L"
          value={paper(p.pnl.value, true)}
          denominator={p.pnl.n}
          population={c.staked}
          withheld={p.pnl.withheld}
          note={`${why} Paper dollars. Fees are not modelled.`}
        />
      </section>

      <section className="panel" aria-labelledby="fc-counts">
        <h3 className="panel__title" id="fc-counts">
          Counts
        </h3>
        <dl className="forecast__counts">
          <Count label="Forecasts" value={c.forecasts} />
          <Count label="Open" value={c.open} />
          <Count label="Resolved" value={c.resolved} />
          <Count label="Void (50/50)" value={c.void} />
          <Count label="With a paper stake" value={c.staked} />
          <Count label="No stake" value={c.no_stake} />
          <Count label="With Reddit hits" value={c.with_reddit} of={c.forecasts} />
          <Count label="With news headlines" value={c.with_news} of={c.forecasts} />
          <Count
            label={`Model calls ${c.calls_day}`}
            value={c.calls_today}
            of={c.calls_cap}
          />
          <Count label="Agent closer than price" value={b.agent_closer.value} of={b.agent_closer.n} />
        </dl>
        <p className="panel__note">
          Paper capital {paper(p.capital)}. Open stakes {paper(p.open_stake)}.
          Free {paper(p.free)}. Settled stakes: won {count(p.wins)}, lost{" "}
          {count(p.losses)}, flat {count(p.flat)}. A void market stays out of
          the Brier score.
        </p>
      </section>

      <section className="panel" aria-labelledby="fc-recent">
        <h3 className="panel__title" id="fc-recent">
          Recent forecasts
        </h3>
        <p className="panel__note">
          The newest {count(payload.recent.length)} of {count(c.forecasts)}.
          {payload.recent_dropped > 0
            ? ` ${count(payload.recent_dropped)} older forecasts are not in this file.`
            : ""}{" "}
          The agent did not see the market price.
        </p>
        <ol className="fanfold forecast__rows">
          {payload.recent.map((r) => (
            <Row key={r.market_id} row={r} />
          ))}
        </ol>
      </section>
    </>
  );
}

function Count({ label, value, of }: { label: string; value: number; of?: number }) {
  return (
    <div className="forecast__count">
      <dt>{label}</dt>
      <dd>
        {count(value)}
        {of !== undefined ? <span className="forecast__of"> of {count(of)}</span> : null}
      </dd>
    </div>
  );
}

function sideLabel(r: ForecastRow): string {
  if (r.side === "none" || r.stake <= 0) return "No stake";
  return `${paper(r.stake)} on ${r.side === "a" ? r.outcome_a : r.outcome_b}`;
}

function outcomeLabel(r: ForecastRow): string {
  if (r.status === "open") return "Open";
  if (r.status === "void") return "Void";
  const won = r.final_price_a === 1 ? r.outcome_a : r.outcome_b;
  return `${won} won · P&L ${paper(r.pnl, true)}`;
}

function Row({ row: r }: { row: ForecastRow }) {
  const links = r.evidence.filter((e) => e.url);
  return (
    <li className="forecast__row">
      <p className="forecast__q">
        {r.market_url ? (
          <a href={r.market_url} target="_blank" rel="noreferrer">
            {r.question}
          </a>
        ) : (
          r.question
        )}
      </p>
      <dl className="forecast__pair">
        <div>
          <dt>Agent, {r.outcome_a}</dt>
          <dd>{pct(r.probability)}</dd>
        </div>
        <div>
          <dt>Price at forecast</dt>
          <dd>{pct(r.market_price)}</dd>
        </div>
        <div>
          <dt>Paper stake</dt>
          <dd className="forecast__small">{sideLabel(r)}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd className="forecast__small">{outcomeLabel(r)}</dd>
        </div>
      </dl>
      <p className="forecast__evidence">
        {links.length === 1 ? "1 evidence link." : `${links.length} evidence links.`}{" "}
        {links.slice(0, 3).map((e, i) => (
          <span key={`${e.url}-${i}`}>
            <a href={e.url ?? undefined} target="_blank" rel="noreferrer">
              {e.kind === "reddit" ? "Reddit" : "News"}: {(e.title ?? "").slice(0, 70)}
            </a>
            {e.used ? " (used)" : ""}
            {i < Math.min(links.length, 3) - 1 ? " · " : ""}
          </span>
        ))}
      </p>
      <p className="forecast__sources">
        {r.reddit_status}. {r.news_status}.
      </p>
    </li>
  );
}
