/* The head of the record, and the first thing on the page.
 *
 * The operator opens this to learn whether the overnight run changed
 * anything. That question is answered here, before any ranking: which dates
 * were read, how many listings, and what can and cannot be differenced. A
 * board that led with a ranking would be answering a question nobody asked
 * yet.
 */

import { count, isoDay, stamp } from "../lib/format";
import type { Bundle } from "../lib/types";
import { bundleWarnings } from "../lib/data";

export function RecordHead({ bundle }: { bundle: Bundle }) {
  const { manifest } = bundle;
  const window = manifest.difference_window;
  const warnings = bundleWarnings(bundle);

  return (
    <header className="record">
      <div className="record__masthead">
        <h1 className="record__title">Market record</h1>
        <p className="record__sub">
          {manifest.source} · swept to {isoDay(manifest.latest_date)} ·{" "}
          exported {stamp(manifest.generated_at)}
        </p>
      </div>

      <dl className="record__facts">
        <div className="record__fact">
          <dt>Listings read</dt>
          <dd>{count(manifest.latest.listings)}</dd>
        </div>
        <div className="record__fact">
          <dt>Leaves in scope</dt>
          <dd>{count(manifest.n_in_scope_leaves)}</dd>
        </div>
        <div className="record__fact">
          <dt>Snapshots held</dt>
          <dd>{count(manifest.n_dates)}</dd>
        </div>
        <div className="record__fact">
          <dt>Differenced</dt>
          <dd>
            {window ? (
              <>
                {count(window.n_known)}
                <span className="record__of">
                  {" "}of {count(window.n_listings)}
                </span>
              </>
            ) : (
              "none"
            )}
          </dd>
        </div>
      </dl>

      <p className="record__provenance">
        {window ? (
          <>
            {isoDay(window.date_a)} → {isoDay(window.date_b)}. A listing can
            only be differenced when eBay showed a sold count on both days,
            which is why the differenced count is smaller than the sweep.
          </>
        ) : (
          <>
            One snapshot. Day-over-day change needs two, so nothing on this
            board is a trend yet.
          </>
        )}
      </p>

      {warnings.length > 0 ? (
        <ul className="record__warnings">
          {warnings.map((w) => (
            <li key={w.what}>
              <strong>{w.what}.</strong> {w.detail}
            </li>
          ))}
        </ul>
      ) : null}
    </header>
  );
}
