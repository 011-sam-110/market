/* The shell: load the bundle once, hold the knob setting, route six views.
 *
 * Loading state matters more here than on most boards. The bundle is 21 MB
 * raw and the operator often opens this on a phone on a train, so "nothing
 * yet" has to be a named state that says what it is waiting for — not a
 * spinner, and absolutely not an empty board that looks like a store with no
 * rows in it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Icon, type IconName } from "./components/Icon";
import { RecordHead } from "./components/RecordHead";
import { loadBundle, loadListings, loadProductMetrics } from "./lib/data";
import { VIEWS, VIEW_BLURBS, VIEW_TITLES, replaceParams, useRoute } from "./lib/router";
import { useKnobs } from "./lib/useKnobs";
import type { Bundle, ListingsPayload } from "./lib/types";
import type { LazyPayloads, ViewProps } from "./lib/viewProps";

import { Shortlist } from "./views/Shortlist";
import { Console } from "./views/Console";
import { Niches } from "./views/Niches";
import { Explore } from "./views/Explore";
import { Coverage } from "./views/Coverage";
import { Trends } from "./views/Trends";

const VIEW_COMPONENTS = {
  shortlist: Shortlist,
  console: Console,
  niches: Niches,
  explore: Explore,
  coverage: Coverage,
  trends: Trends,
} as const;

export function App() {
  const [route, go] = useRoute();
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let live = true;
    loadBundle()
      .then((b) => live && setBundle(b))
      .catch((e: unknown) => live && setError(e as Error));
    return () => {
      live = false;
    };
  }, []);

  if (error) {
    return (
      <main className="sheet">
        <div className="state state--error">
          <h1 className="state__title">The bundle did not load</h1>
          <p className="state__body">{error.message}</p>
          <p className="state__body">
            This board reads static JSON files.{" "}
            <code>scripts/export_dashboard.py</code> writes them. No code runs
            at request time. A failure here means a missing or half-written
            file. It never means a query timed out.
          </p>
        </div>
      </main>
    );
  }

  if (!bundle) {
    return (
      <main className="sheet">
        <div className="state">
          <h1 className="state__title">Reading the record</h1>
          <p className="state__body">
            The board is fetching the manifest, the knob settings, the fee
            table and the ranked boards. The download is about 3 MB.
          </p>
        </div>
      </main>
    );
  }

  return <Board bundle={bundle} route={route} go={go} />;
}

function Board({
  bundle,
  route,
  go,
}: {
  bundle: Bundle;
  route: ReturnType<typeof useRoute>[0];
  go: ReturnType<typeof useRoute>[1];
}) {
  const knobState = useKnobs(bundle.knobs, route.view, route.params);

  // The two heavy payloads, fetched at most once each and shared across
  // views. A ref rather than state for the promise so two views mounting in
  // the same tick cannot start two fetches of a 7 MB file.
  const listingsRef = useRef<Promise<ListingsPayload> | null>(null);
  const metricsRef = useRef<Promise<Record<string, unknown>> | null>(null);
  const [loaded, setLoaded] = useState({ listings: false, productMetrics: false });

  const lazy: LazyPayloads = useMemo(
    () => ({
      listings: () => {
        listingsRef.current ??= loadListings(bundle.manifest.latest_date).then(
          (p) => {
            setLoaded((s) => ({ ...s, listings: true }));
            return p;
          },
        );
        return listingsRef.current;
      },
      productMetrics: () => {
        metricsRef.current ??= loadProductMetrics(
          bundle.manifest.latest_date,
        ).then((p) => {
          setLoaded((s) => ({ ...s, productMetrics: true }));
          return p;
        });
        return metricsRef.current;
      },
      loaded,
    }),
    [bundle.manifest.latest_date, loaded],
  );

  const setParams = useCallback(
    (next: URLSearchParams) => {
      for (const [k, v] of knobState.query) next.set(k, v);
      replaceParams(route.view, next);
      // `replaceParams` does not fire `hashchange`, so nudge the router.
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    },
    [knobState.query, route.view],
  );

  const View = VIEW_COMPONENTS[route.view];
  const props: ViewProps = {
    bundle,
    knobState,
    params: route.params,
    setParams,
    go,
    lazy,
  };

  return (
    <>
      <a className="skip visually-hidden" href="#main">
        Skip to the record
      </a>

      <nav className="nav" aria-label="Views">
        {VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            className="nav__item"
            aria-current={route.view === v ? "page" : undefined}
            title={VIEW_BLURBS[v]}
            onClick={() => go(v, knobState.query)}
          >
            <Icon name={v as IconName} />
            <span>{VIEW_TITLES[v]}</span>
          </button>
        ))}
      </nav>

      <main className="sheet" id="main">
        <RecordHead bundle={bundle} />
        <View {...props} />
      </main>
    </>
  );
}
