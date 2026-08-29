"""Copy the newest generated reports into web/ and rebuild its index.

Vercel hosts the VIEWER, never the sweep. Three measured reasons the scrape
cannot move there, all recorded in scripts/run_daily.cmd:

  * eBay refuses any datacenter or VPN exit, and refused the residential
    proxy too. The only egress that gets a 200 is the split-tunnelled Chrome
    for Testing on this machine's home IP.
  * The sweep runs ~21 minutes; a Vercel function caps out at 300s.
  * The store is parquet under DATA_DIR, gitignored and growing daily, while
    a serverless filesystem is ephemeral.

So the pipeline stays here and PUBLISHES. This script is the seam: it picks
the newest `niche-board-*.html` and `population-*.html` out of
DATA_DIR/reports, copies them into web/reports/ under a dated name, and
regenerates web/index.html from whatever is actually on disk afterwards.

The index is generated from the directory listing, never from a hand-kept
list -- a run that failed to produce a report is then absent from the page
rather than showing a dead link to a file that was never written.

The full population page (~5 MB, with the row-level explorer) is skipped by
default: `population_report.py --summary` produces the same panels at ~215 KB
and the repo would otherwise gain 5 MB a night. Pass --include-full to
override.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
WEB_REPORTS = WEB / "reports"

# niche-board-20260829T164029Z.html -> 20260829T164029Z
_STAMP = re.compile(r"-(\d{8}T\d{6}Z)\.html$")
_FULL_PAGE_BYTES = 1_000_000


def _stamp_of(path: Path) -> str | None:
    m = _STAMP.search(path.name)
    return m.group(1) if m else None


def _day_of(stamp: str) -> str:
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _newest_per_day(paths, *, skip_large: bool) -> dict[str, Path]:
    """day -> the LAST report written that day. A day with two runs keeps the
    later one; the earlier is not published, and is not deleted either."""
    best: dict[str, Path] = {}
    for p in sorted(paths):
        stamp = _stamp_of(p)
        if stamp is None:
            continue
        if skip_large and p.stat().st_size > _FULL_PAGE_BYTES:
            continue
        day = _day_of(stamp)
        if day not in best or stamp > (_stamp_of(best[day]) or ""):
            best[day] = p
    return best


def collect(data_dir: Path, *, include_full: bool) -> dict[str, dict]:
    """day -> {"board": Path|None, "population": Path|None} from disk."""
    src = data_dir / "reports"
    if not src.is_dir():
        return {}
    boards = _newest_per_day(src.glob("niche-board-*.html"), skip_large=False)
    pops = _newest_per_day(src.glob("population-*.html"),
                           skip_large=not include_full)
    days = sorted(set(boards) | set(pops), reverse=True)
    return {d: {"board": boards.get(d), "population": pops.get(d)}
            for d in days}


def publish(runs: dict[str, dict]) -> dict[str, dict]:
    """Copy into web/reports/ under stable dated names. Returns the same map
    with web-relative hrefs and byte sizes."""
    WEB_REPORTS.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    for day, kinds in runs.items():
        entry: dict = {}
        for kind, path in kinds.items():
            if path is None:
                entry[kind] = None
                continue
            dest = WEB_REPORTS / f"{kind}-{day}.html"
            shutil.copy2(path, dest)
            entry[kind] = {"href": f"reports/{dest.name}",
                           "bytes": dest.stat().st_size}
        out[day] = entry
    return out


def _kb(n: int) -> str:
    return f"{n / 1024:,.0f} KB" if n < 1_000_000 else f"{n / 1048576:.1f} MB"


def render_index(published: dict[str, dict], *, generated: datetime) -> str:
    days = list(published)
    rows = []
    for i, day in enumerate(days):
        e = published[day]
        pretty = datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
        latest = ('<span class="tag">latest</span>' if i == 0 else "")
        cells = []
        for kind, label in (("board", "Niche board"),
                            ("population", "Population review")):
            item = e.get(kind)
            if item:
                cells.append(
                    f'<a class="doc" href="{html.escape(item["href"])}">'
                    f'<span class="doc-name">{label}</span>'
                    f'<span class="doc-size">{_kb(item["bytes"])}</span></a>')
            else:
                # "not published", NOT "not generated" -- the two differ and
                # the page must not claim the stronger one. A report can be
                # absent here because the run never wrote it, or because it
                # was written as the ~5 MB full page and skipped by the size
                # rule above. This label is true in both cases.
                cells.append(f'<span class="doc missing">{label}'
                             '<span class="doc-size">not published</span></span>')
        rows.append(
            f'<li class="run"><div class="run-day">{pretty}{latest}</div>'
            f'<div class="run-docs">{"".join(cells)}</div></li>')

    stamp = generated.strftime("%d %b %Y %H:%M UTC")
    body = "\n".join(rows) or '<li class="run empty">No reports published yet.</li>'
    n = len(days)
    return _TEMPLATE.format(rows=body, stamp=html.escape(stamp),
                            n_runs=n, plural="" if n == 1 else "s")


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Market runs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,600&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{{
  --ground:#F5F6F8;--surface:#FFFFFF;--surface-2:#EEF0F4;--ink:#161A21;
  --ink-2:#39424F;--muted:#5C6673;--rule:#DDE1E8;--accent:#1F6F6B;
  --accent-soft:#E4F0EF;
}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    --ground:#101319;--surface:#171B22;--surface-2:#1E232B;--ink:#E7EAEF;
    --ink-2:#BAC2CE;--muted:#8E99A8;--rule:#2A303A;--accent:#4FB3AC;
    --accent-soft:#16302F;
  }}
}}
:root[data-theme="dark"]{{
  --ground:#101319;--surface:#171B22;--surface-2:#1E232B;--ink:#E7EAEF;
  --ink-2:#BAC2CE;--muted:#8E99A8;--rule:#2A303A;--accent:#4FB3AC;
  --accent-soft:#16302F;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);
  font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased;}}
.wrap{{max-width:760px;margin:0 auto;padding:56px 24px 96px;}}
.eyebrow{{font-family:"IBM Plex Mono",monospace;font-size:.7rem;font-weight:500;
  letter-spacing:.14em;text-transform:uppercase;color:var(--muted);}}
h1{{font-family:Newsreader,Georgia,serif;font-size:2.4rem;font-weight:600;
  margin:.25rem 0 0;letter-spacing:-.015em;}}
.sub{{color:var(--ink-2);margin:14px 0 0;max-width:60ch;}}
.meta{{font-family:"IBM Plex Mono",monospace;font-size:.76rem;color:var(--muted);
  margin-top:18px;padding-bottom:26px;border-bottom:1px solid var(--rule);}}
ul{{list-style:none;margin:32px 0 0;padding:0;display:flex;flex-direction:column;gap:14px;}}
.run{{background:var(--surface);border:1px solid var(--rule);border-radius:8px;
  padding:16px 18px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;
  justify-content:space-between;}}
.run-day{{font-family:"IBM Plex Mono",monospace;font-size:.9rem;font-weight:500;
  display:flex;align-items:center;gap:10px;}}
.tag{{font-size:.62rem;letter-spacing:.09em;text-transform:uppercase;
  background:var(--accent-soft);color:var(--accent);padding:3px 7px;border-radius:4px;
  font-weight:600;}}
.run-docs{{display:flex;gap:10px;flex-wrap:wrap;}}
.doc{{display:flex;flex-direction:column;gap:1px;text-decoration:none;
  background:var(--surface-2);border:1px solid transparent;border-radius:6px;
  padding:8px 12px;min-width:150px;}}
a.doc:hover,a.doc:focus-visible{{border-color:var(--accent);outline:none;}}
.doc-name{{font-size:.85rem;color:var(--ink);font-weight:500;}}
a.doc .doc-name{{color:var(--accent);}}
.doc-size{{font-family:"IBM Plex Mono",monospace;font-size:.7rem;color:var(--muted);}}
.missing{{opacity:.55;}}
.empty{{color:var(--muted);justify-content:center;}}
footer{{margin-top:44px;padding-top:18px;border-top:1px solid var(--rule);
  color:var(--muted);font-size:.78rem;font-family:"IBM Plex Mono",monospace;}}
@media(max-width:560px){{
  h1{{font-size:1.9rem}}.run{{flex-direction:column;align-items:flex-start}}
  .doc{{min-width:0}}.run-docs{{width:100%}}.doc{{flex:1}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">eBay UK niche finder</div>
  <h1>Market runs</h1>
  <p class="sub">Nightly sweep output. The scrape runs on the machine at home &mdash;
  eBay refuses datacenter egress, so only these generated reports are published here.</p>
  <div class="meta">{n_runs} run{plural} published &middot; updated {stamp}</div>
  <ul>
{rows}
  </ul>
  <footer>Generated by scripts/publish_web.py</footer>
</div>
</body>
</html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="store root holding reports/ (default: config DATA_DIR)")
    ap.add_argument("--include-full", action="store_true",
                    help="also publish the ~5 MB full population page")
    args = ap.parse_args(argv)

    data_dir = args.data_dir
    if data_dir is None:
        sys.path.insert(0, str(REPO))
        from phoneloop import config
        data_dir = config.load().DATA_DIR

    runs = collect(Path(data_dir), include_full=args.include_full)
    if not runs:
        print(f"no reports found under {Path(data_dir) / 'reports'}")
        return 1

    published = publish(runs)
    index = WEB / "index.html"
    index.write_text(render_index(published, generated=datetime.now(UTC)),
                     encoding="utf-8")

    for day, e in published.items():
        have = [k for k, v in e.items() if v]
        print(f"  {day}: {', '.join(have) if have else '(nothing)'}")
    print(f"wrote {index} ({len(published)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
