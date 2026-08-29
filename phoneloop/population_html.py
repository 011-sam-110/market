"""Rendering for the population review page. No data decisions live here.

The central inversion this file exists to encode: **colour encodes evidence,
a glyph encodes the verdict.** On a conventional board a PASS on n=1 and a
PASS on n=681 are the same shade of green, which is precisely the confusion
that lets a niche rank highly because its seller column is empty. Here the
fill is the denominator bucket, the glyph is the verdict, and the cell prints
the denominator as text so colour is never the sole channel.

Two rules are enforced mechanically rather than by review:

1. **Every figure carries its denominator.** `_fig()` is the only way a number
   reaches the page, and it always emits `class="figure"` immediately followed
   by `class="denom"`. A structural test asserts the two counts are equal, so
   a figure added without evidence fails the suite rather than the reader.
2. **A locked panel contains no figure at all.** That is the testable
   definition of "no placeholder number": not "the numbers are honest", but
   "there are no numbers". The scaffold (axes, row labels, em-dashes) still
   renders, so nothing moves when the panel fills.

Formatting reuses `board._fmt` / `board._fmt_pence` rather than
reimplementing the `None -> "?"` convention -- two report modules disagreeing
about how they print an unknown is exactly how "?" quietly becomes "0".

Self-contained: one inline `<style>`, no CDN, no external font, no fetch, and
no `http://` or `https://` substring anywhere in the output -- including
inside listing titles, which `population._sanitise` strips before they are
ever encoded.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path

from phoneloop import population
from phoneloop.board import _fmt, _fmt_pence
from phoneloop.population import Build

_REPORT_DIR_NAME = "reports"

# board._STYLE's palette, extended with the evidence ramp. The two reports
# have to read as one system: a reader moving from the niche board to this
# page should not have to relearn what green means.
_STYLE = """
:root{
  --bg:#0b0e13; --fg:#e6e6e6; --line:#2a2f3a; --head:#161b22; --alt:#11151c;
  --pass:#4caf50; --fail:#e05555; --unknown:#9aa4b2; --note:#e0a955;
  --d0:#171a21; --d1:#23313f; --d2:#2c4b63; --d3:#316a8c; --d4:#3f8fb8;
}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:0;
     background:var(--bg);color:var(--fg);font-size:14px;line-height:1.45}
.wrap{max-width:1180px;margin:0 auto;padding:0 1.5rem 5rem}
h1{font-size:1.5rem;margin:1.6rem 0 .2rem}
h2{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;
   color:var(--unknown);margin:2.6rem 0 .6rem;border-top:1px solid var(--line);
   padding-top:.9rem}
h3{font-size:1.02rem;margin:0 0 .15rem}
p{margin:.45rem 0}
small,.meta{color:var(--unknown)}
code{font-family:Consolas,Menlo,monospace;font-size:.85em;
     background:var(--head);padding:.05rem .3rem;border-radius:3px}
a{color:#7fb2d8}
.rail{position:sticky;top:0;z-index:20;background:var(--bg);
      border-bottom:1px solid var(--line);padding:.5rem 1.5rem;
      display:flex;gap:.9rem;align-items:center;flex-wrap:wrap;
      font-size:.8rem;color:var(--unknown)}
.tick{display:inline-block;width:13px;height:13px;border:1px solid var(--line);
      margin-right:3px;vertical-align:-2px;background:var(--d0)}
.tick.on{background:var(--d4);border-color:var(--d4)}
.panel{border:1px solid var(--line);border-radius:6px;padding:1rem 1.1rem;
       margin:.9rem 0;background:#0d1117}
.panel .q{color:var(--unknown);font-style:italic;margin:0 0 .7rem;font-size:.86rem}
.figure{font-size:1.05rem;font-weight:600;color:#fff;font-variant-numeric:tabular-nums}
.denom{color:var(--unknown);font-size:.78rem;margin-left:.35rem;
       font-variant-numeric:tabular-nums;white-space:nowrap}
.headline{font-size:1.02rem;margin:.3rem 0 .8rem}
table{border-collapse:collapse;width:100%;font-size:.8rem;
      font-variant-numeric:tabular-nums}
th,td{border:1px solid var(--line);padding:3px 7px;text-align:left;
      vertical-align:top}
th{background:var(--head);font-weight:600}
tr:nth-child(even) td{background:var(--alt)}
.scroll{overflow-x:auto;max-width:100%}
.tall{max-height:420px;overflow-y:auto}
.pass{color:var(--pass)}
.fail{color:var(--fail)}
.unknown{color:var(--unknown)}
.lifetime-note{color:var(--note)}
.flag{color:var(--note)}
.locked{border-style:dashed;border-color:#3a3f4d;background:#0c1016}
.locked h3{color:var(--note)}
.locked .lock-head{font-size:.76rem;letter-spacing:.13em;color:var(--note);
                   font-weight:700;margin:.6rem 0 .3rem}
.locked .lock-line{color:var(--unknown);font-size:.84rem;margin:.2rem 0;
                   max-width:78ch}
.scaffold{opacity:.55}
.dash{color:#5a6270}
.grid{display:grid;gap:2px;font-size:.62rem}
.cell{position:relative;height:24px;border-radius:2px;display:flex;
      align-items:center;justify-content:center;gap:2px;cursor:default;
      color:#cfd6de;font-variant-numeric:tabular-nums}
.cell .g{font-weight:700}
.d-0{background:var(--d0)} .d-1{background:var(--d1)} .d-2{background:var(--d2)}
.d-3{background:var(--d3)} .d-4{background:var(--d4)}
.unreadable{background:repeating-linear-gradient(45deg,#14171e,#14171e 4px,
            #1b1f27 4px,#1b1f27 8px);color:#6a7280}
.rowlab{font-size:.62rem;color:var(--unknown);height:24px;display:flex;
        align-items:center;justify-content:flex-end;padding-right:5px;
        white-space:nowrap}
.collab{font-size:.62rem;color:var(--unknown);height:6.2em;
        writing-mode:vertical-rl;transform:rotate(180deg);margin:0 auto;
        white-space:nowrap}
.grid.compact .cell{height:15px;font-size:.55rem;gap:1px}
.legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.74rem;
        color:var(--unknown);margin:.7rem 0}
.legend span.sw{display:inline-block;width:14px;height:12px;
                vertical-align:-2px;margin-right:4px;border-radius:2px}
.bands{display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0}
.band{border:1px solid var(--line);border-radius:4px;padding:.35rem .55rem;
      font-size:.74rem;color:var(--unknown)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.15rem .8rem;
    font-size:.82rem;margin:.4rem 0}
.kv dt{color:var(--unknown)}
.kv dd{margin:0}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:1.1rem}
.gutter{border-left:1px dashed #3a3f4d;color:var(--note);font-size:.68rem;
        padding-left:.5rem}
.caveat{border-left:2px solid var(--note);padding-left:.6rem;color:#c9b48f;
        font-size:.83rem;margin:.5rem 0}
.crit{border-left:2px solid var(--fail);padding-left:.6rem;color:#d9a6a6;
      font-size:.83rem;margin:.5rem 0}
.ok{border-left:2px solid var(--pass);padding-left:.6rem;color:#a9cfae;
    font-size:.83rem;margin:.5rem 0}
/* An SVG with a viewBox stretches to whatever box it is given. Every chart
   is drawn at a deliberate size, so `.chart` caps it there: upscaled to
   1180px a 210x90 scaffold becomes a 500px-tall expanse of nothing. */
svg{max-width:100%;height:auto;display:block}
.chart{width:100%}
.ctrl{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.6rem 0}
select,input{background:var(--head);color:var(--fg);border:1px solid var(--line);
             border-radius:4px;padding:.25rem .4rem;font-size:.78rem}
#pop-scroll{height:440px;overflow-y:auto;border:1px solid var(--line);
            border-radius:4px;position:relative}
#pop-spacer{position:relative;width:100%}
#pop-rows{position:absolute;top:0;left:0;right:0}
.prow{display:grid;height:26px;align-items:center;font-size:.74rem;
      border-bottom:1px solid #171b22;padding:0 .4rem;
      grid-template-columns:110px 1fr 78px 78px 92px 120px 64px 64px;gap:.4rem;
      font-variant-numeric:tabular-nums}
.prow.head{position:sticky;top:0;background:var(--head);z-index:2;
           font-weight:600;color:var(--unknown)}
.prow .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:820px){.cols2{grid-template-columns:1fr}}
"""


# --- primitives ----------------------------------------------------------------

def _e(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _num(value: object) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return f"{value:,.3g}"
    return f"{value:,}"


def _pct(part: object, whole: object) -> str:
    if part is None or not whole:
        return "?"
    return f"{part / whole:.2%}"


def _fig(value: object, denom: str) -> str:
    """The ONLY way a number reaches this page: the figure and the evidence
    it rests on, emitted together so neither can travel alone."""
    return (f'<span class="figure">{_e(value)}</span>'
            f'<span class="denom">{_e(denom)}</span>')


def _of(part: object, whole: object, unit: str = "") -> str:
    tail = f" {unit}" if unit else ""
    return f"n={_num(part)} of {_num(whole)}{tail}"


def _panel(name: str, title: str, question: str, body: str, *,
           reason: str | None = None) -> str:
    """One panel. When `reason` is set the panel is LOCKED: it keeps its
    position and size, states what it needs, and carries no figure."""
    cls = "panel locked" if reason else "panel"
    head = (f'<h3>{_e(title)}</h3>'
            + (f'<p class="q">{_e(question)}</p>' if question else ""))
    lock = _locked_body(reason) if reason else ""
    return (f'<div class="{cls}" data-panel="{_e(name)}" '
            f'id="panel-{_e(name)}">{head}{lock}{body}</div>')


def _locked_body(reason: str) -> str:
    lines = reason.split("\n")
    out = [f'<div class="lock-head">{_e(lines[0])}</div>']
    for line in lines[1:]:
        if line.startswith("[what unlocks this"):
            out.append('<p class="lock-line">'
                       '<a href="#panel-date_ledger">[what unlocks this -&gt;]</a></p>')
        else:
            out.append(f'<p class="lock-line">{_e(line)}</p>')
    return "".join(out)


def _table(headers, rows, *, cls: str = "") -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    wrap = f'<div class="scroll {cls}">' if cls else '<div class="scroll">'
    return f"{wrap}<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _cell(verdict, denominator: int | None, *, title: str = "") -> str:
    """One knob cell. Fill = evidence bucket, glyph = verdict, text = the
    denominator itself -- three channels, so colour is never the only one.

    The printed denominator is deliberately NOT a `class="figure"`: it IS the
    evidence, not a figure resting on evidence, and wrapping it would demand
    a denominator for a denominator.
    """
    if denominator is None:
        return f'<div class="cell unreadable" title="{_e(title)}">-</div>'
    bucket = population.denom_bucket(denominator)
    idx = population.DENOM_BUCKETS.index(bucket)
    if verdict is None:
        glyph, cls = "?", "unknown"
    elif verdict:
        glyph, cls = "✓", "pass"
    else:
        glyph, cls = "✗", "fail"
    body = f'<span class="g {cls}">{glyph}</span><span>{denominator}</span>'
    if verdict is None:
        return (f'<div class="cell unreadable" title="{_e(title)}">'
                f'<span class="g unknown">?</span></div>')
    return f'<div class="cell d-{idx}" title="{_e(title)}">{body}</div>'


def _ramp_legend() -> str:
    labels = ("no evidence", "n=1", "n=2-5", "n=6-30", "n>30")
    swatches = "".join(
        f'<span><span class="sw d-{i}"></span>{_e(lab)}</span>'
        for i, lab in enumerate(labels))
    return ('<div class="legend">'
            '<span><strong>fill = evidence</strong></span>' + swatches +
            '<span><strong>glyph = verdict</strong></span>'
            '<span class="pass">✓ pass</span>'
            '<span class="fail">✗ fail</span>'
            '<span class="unknown">? unreadable (no verdict)</span></div>')


# --- small charts ----------------------------------------------------------------

def _chart(svg: str, width: int) -> str:
    """Cap a chart at the size it was drawn for. Without this every viewBox
    scales to the full 1180px column, which turns a small scaffold into half
    a screen of empty axes."""
    return f'<div class="chart" style="max-width:{width}px">{svg}</div>'


def _bar_chart(pairs, *, width=560, bar_h=13, label_w=150, value_fmt=_num,
               shade=None) -> str:
    """Horizontal bars. `shade` is an optional (lo, hi) range on the LABEL
    axis rendered as a band behind the bars."""
    pairs = list(pairs)
    if not pairs:
        return '<p class="meta">(nothing to draw)</p>'
    top = max((v for _, v in pairs), default=0) or 1
    h = len(pairs) * (bar_h + 3) + 6
    plot_w = width - label_w - 60
    parts = [f'<svg viewBox="0 0 {width} {h}" role="img">']
    if shade:
        lo_i, hi_i = shade
        y0 = 3 + lo_i * (bar_h + 3)
        y1 = 3 + (hi_i + 1) * (bar_h + 3)
        parts.append(f'<rect x="0" y="{y0}" width="{width}" height="{y1 - y0}" '
                     f'fill="#1b2a1c"/>')
    for i, (label, value) in enumerate(pairs):
        y = 3 + i * (bar_h + 3)
        w = max(1, int(plot_w * (value / top)))
        parts.append(
            f'<text x="{label_w - 6}" y="{y + bar_h - 3}" fill="#9aa4b2" '
            f'font-size="9" text-anchor="end">{_e(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w}" height="{bar_h}" '
            f'fill="#316a8c"/>'
            f'<text x="{label_w + w + 5}" y="{y + bar_h - 3}" fill="#c9d1d9" '
            f'font-size="9">{_e(value_fmt(value))}</text>')
    parts.append("</svg>")
    return _chart("".join(parts), width)


_CATEGORY_HUES = ("#4caf50", "#e0a955", "#7fb2d8", "#c792ea", "#e05555",
                  "#4dd0c1", "#d4a373", "#9aa4b2", "#8fbf6f")


def _scatter(points, *, width=620, height=300, x_label="", y_label="") -> str:
    if not points:
        return '<p class="meta">(nothing to draw)</p>'
    pad_l, pad_b, pad_t, pad_r = 46, 34, 12, 12
    xs = [p["x"] for p in points]
    ys = [p["y"] for p in points]
    x_max = max(xs) or 1
    y_max = max(ys) or 1
    r_max = max(p["r"] for p in points) or 1
    cats = sorted({p["cat"] for p in points})
    colour = {c: _CATEGORY_HUES[i % len(_CATEGORY_HUES)] for i, c in enumerate(cats)}
    pw = width - pad_l - pad_r
    ph = height - pad_b - pad_t

    out = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    out.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" '
               f'y2="{pad_t + ph}" stroke="#2a2f3a"/>'
               f'<line x1="{pad_l}" y1="{pad_t + ph}" x2="{pad_l + pw}" '
               f'y2="{pad_t + ph}" stroke="#2a2f3a"/>')
    for frac in (0, 0.5, 1.0):
        y = pad_t + ph - frac * ph
        out.append(f'<text x="{pad_l - 6}" y="{y + 3}" fill="#9aa4b2" '
                   f'font-size="9" text-anchor="end">{frac * y_max:.0%}</text>')
        out.append(f'<text x="{pad_l + frac * pw}" y="{pad_t + ph + 13}" '
                   f'fill="#9aa4b2" font-size="9" text-anchor="middle">'
                   f'{int(frac * x_max)}</text>')
    for p in points:
        cx = pad_l + (p["x"] / x_max) * pw
        cy = pad_t + ph - (p["y"] / y_max) * ph
        r = 2.5 + 7 * (p["r"] / r_max) ** 0.5
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
                   f'fill="{colour[p["cat"]]}" fill-opacity="0.55" '
                   f'stroke="{colour[p["cat"]]}">'
                   f'<title>{_e(p["title"])}</title></circle>')
    out.append(f'<text x="{pad_l + pw / 2}" y="{height - 3}" fill="#9aa4b2" '
               f'font-size="9" text-anchor="middle">{_e(x_label)}</text>')
    out.append(f'<text x="10" y="{pad_t + ph / 2}" fill="#9aa4b2" font-size="9" '
               f'transform="rotate(-90 10 {pad_t + ph / 2})" '
               f'text-anchor="middle">{_e(y_label)}</text>')
    out.append("</svg>")
    legend = "".join(
        f'<span><span class="sw" style="background:{colour[c]}"></span>{_e(c)}</span>'
        for c in cats)
    return _chart("".join(out), width) + f'<div class="legend">{legend}</div>'


# --- zone 0 -----------------------------------------------------------------------

def _rail(b: Build) -> str:
    ticks = "".join('<span class="tick on"></span>' for _ in b.dates) or \
            '<span class="tick"></span>'
    dates = ", ".join(d.isoformat() for d in b.dates) or "none"
    return (f'<div class="rail">{ticks}'
            f'<span><strong>{len(b.dates)}</strong> snapshot date'
            f'{"" if len(b.dates) == 1 else "s"} on file — {_e(dates)}</span>'
            f'<span>· everything windowed needs a 2nd</span>'
            f'<span>· <a href="#panel-date_ledger">unlock calendar</a></span>'
            f'</div>')


def _render_provenance(b: Build) -> str:
    payload, reason = population.panel_provenance(b)
    if reason:
        return ""
    p = payload
    ticks = "".join('<span class="tick on" title="' + d.isoformat() + '"></span>'
                    for d in p["dates"])
    body = [
        f'<p>read from <code>{_e(p["data_dir"])}</code></p>',
        f'<p>{ticks} <small>{_e(", ".join(d.isoformat() for d in p["dates"]))}'
        f'</small></p>',
        '<div class="bands">',
        '<div class="band">' + _fig(_num(p["raw_rows"]), "raw observation rows")
        + '</div>',
        '<div class="band">' + _fig(
            _num(p["listings"]),
            _of(p["listings"], p["raw_rows"], "after newest-per-source_id"))
        + '</div>',
        '<div class="band">' + _fig(
            _num(p["leaves_swept"]),
            _of(p["swept_in_scope"], p["in_scope"], "in-scope leaves swept"))
        + '</div>',
        '<div class="band">' + _fig(
            _num(len(p["stray_leaves"])),
            _of(p["stray_rows"], p["listings"], "rows in out-of-catalogue leaves"))
        + '</div>',
        '<div class="band">' + _fig(
            f'{p["store_bytes"] / 1024 / 1024:.1f} MB',
            _of(p["raw_rows"], p["raw_rows"], "rows on disk"))
        + '</div>',
        '</div>',
        f'<p class="meta">sweep window (UTC) '
        f'{_e(p["first_seen"])} → {_e(p["last_seen"])}</p>',
    ]
    return _panel("provenance", "P0 · Provenance",
                  "Which store, which dates, and can I trust anything below?",
                  "".join(body))


def _empty_page(b: Build) -> str:
    body = (
        f'<p class="headline">no eBay snapshots in '
        f'DATA_DIR=<code>{_e(b.data_dir)}</code></p>'
        '<p>This store holds no rows for source=ebay. Run the sweep first '
        '(<code>scripts/run_daily.cmd</code>), or point '
        '<code>PHONELOOP_DATA_DIR</code> at the store that holds them.</p>'
        '<p>Nothing below is rendered because there is nothing to render — '
        'this is not a zeroed page.</p>')
    return _document(
        "eBay population review — empty store",
        '<div class="rail"><span class="tick"></span>'
        '<span>0 snapshot dates on file</span></div>'
        '<div class="wrap"><h1>eBay population review</h1>'
        + _panel("empty_store", "no eBay snapshots", "", body) + "</div>")


# --- zone 1 -------------------------------------------------------------------------

def _render_trust_ledger(b: Build) -> str:
    payload, reason = population.panel_trust_ledger(b)
    if reason:
        return ""
    p = payload
    knobs = p["n_knobs"] or 1
    strip = ['<div class="grid compact" style="grid-template-columns:repeat('
             f'{knobs}, 1fr);max-width:300px">']
    for cell in p["grid"]:
        title = f'{cell["niche"]} · {cell["knob"]} · denominator {cell["denominator"]}'
        strip.append(_cell(cell["verdict"], cell["denominator"], title=title))
    strip.append("</div>")

    buckets = "".join(
        f'<div class="band"><span class="sw d-{i}"></span> denominator '
        f'{_e(name)}: {_num(p["buckets"][name])} verdicts</div>'
        for i, name in enumerate(population.DENOM_BUCKETS))

    body = [
        f'<p class="headline">'
        + _fig(f'{_num(p["verdicts"])} of {_num(p["cells"])}',
               "knob cells that rendered a verdict at all")
        + "</p>",
        f'<p class="headline">'
        + _fig(f'{_num(p["buckets"]["1"])} of {_num(p["verdicts"])}',
               "of those verdicts rest on a denominator of 1")
        + "</p>",
        f'<p class="meta">{p["n_niches"]} in-scope niches × {p["n_knobs"]} '
        f'knobs = {p["cells"]} cells. '
        f'{p["unreadable"]} rendered nothing.</p>',
        # The strip is deliberately kept beside the reading of it rather than
        # above: the hatched mass IS the finding, and it is 43 rows tall.
        '<div class="cols2"><div>' + "".join(strip) + "</div><div>"
        + _ramp_legend() + f'<div class="bands">{buckets}</div>'
        + '<p class="meta">One cell per (niche, knob), in board order. '
          'Colour is how much evidence the verdict rests on; the glyph is '
          'the verdict; the number is the denominator itself. A hatched cell '
          'rendered no verdict at all.</p></div></div>',
    ]
    return _panel("trust_ledger", "P1 · Trust ledger",
                  "Should I act on any row of tonight's board?",
                  "".join(body))


def _render_knob_matrix(b: Build) -> str:
    payload, reason = population.panel_knob_matrix(b)
    if reason:
        return ""
    p = payload
    names = p["knob_names"]
    cols = f"minmax(90px,110px) repeat({len(names)}, minmax(46px,1fr))"
    out = [f'<div class="scroll"><div class="grid" '
           f'style="grid-template-columns:{cols}">',
           '<div></div>']
    for name in names:
        out.append(f'<div class="collab">{_e(name)}</div>')
    for row in p["grid"]:
        out.append(f'<div class="rowlab" title="score {row["score"]}">'
                   f'{_e(row["niche"])}</div>')
        for cell in row["cells"]:
            if cell is None:
                out.append('<div class="cell unreadable">-</div>')
                continue
            title = (f'{cell["knob"]}: value={_fmt(cell["value"])} '
                     f'threshold={_fmt(cell["threshold"])} '
                     f'denominator={cell["denominator"]} '
                     f'coverage={cell["coverage"]:.1%}'
                     + (f' — {cell["note"]}' if cell["note"] else ""))
            out.append(_cell(cell["verdict"], cell["denominator"], title=title))
    out.append("</div></div>")

    foot_rows = []
    for name in names:
        slot = p["per_knob"][name]
        dens = slot["denominators"]
        foot_rows.append((
            _e(name),
            _fig(_num(slot["unreadable"]),
                 _of(slot["unreadable"], p["n_niches"], "niches unreadable")),
            _fig(_num(slot["pass"]), _of(slot["pass"], p["n_niches"], "pass")),
            _fig(_num(slot["fail"]), _of(slot["fail"], p["n_niches"], "fail")),
            _e(f'{min(dens)}–{max(dens)}' if dens else "?"),
        ))

    body = [
        '<p class="meta">Hover a cell for the KnobResult behind it: value, '
        'threshold, denominator, coverage and note, verbatim.</p>',
        _ramp_legend(),
        "".join(out),
        _table(("knob", "unreadable", "pass", "fail", "denominator range"),
               foot_rows),
        f'<div class="crit">{_e(p["saturation_note"])}</div>',
    ]
    return _panel("knob_matrix", "P2 · Knob readability matrix",
                  "Which knob do I spend the next scrape hour on?",
                  "".join(body))


def _render_green_because_broken(b: Build) -> str:
    payload, reason = population.panel_green_because_broken(b)
    if reason:
        return ""
    p = payload
    rows = []
    for f in p["flagged"]:
        sat = f["saturation"]
        rows.append((
            _e(f["niche"]),
            _fig(f'{f["seller_cov"]:.2%}',
                 _of(f["seller_known"], f["n"], "rows with a seller")),
            _fig(_num(f["score"]), _of(f["rank"], p["n_niches"], "rank")),
            _fig(_num(f["price_median_denominator"]),
                 _of(f["price_median_denominator"], f["n"],
                     "business landed prices")),
            _e(", ".join(f["passing"])),
            _e(f'saturation={_fmt(sat.value if sat else None)}'),
        ))
    body = [
        '<p class="headline">'
        + _fig(f'{_num(len(p["flagged"]))} of {_num(p["n_scoring_above_zero"])}',
               "in-scope niches scoring above zero pass only because the data "
               "is absent")
        + "</p>",
        f'<p class="meta">Flagged when a knob PASSES while the leaf\'s seller '
        f'column is under {p["threshold"]:.0%} present.</p>',
        _table(("niche", "seller coverage", "score / rank",
                "business landed prices", "passing knobs", "saturation"), rows)
        if rows else '<p class="ok">No in-scope niche passes a knob on '
                     'sub-threshold seller coverage.</p>',
        f'<div class="crit">{_e(p["critique"])}</div>',
        '<p class="meta">Cross-referenced by '
        '<a href="#panel-competition_density">P7</a>, which excludes exactly '
        'these leaves from every density figure.</p>',
    ]
    return _panel("green_because_broken", "P3 · Green because broken",
                  "Which rows do I strike out before reading the ranking?",
                  "".join(body))


def _render_coverage_ladder(b: Build) -> str:
    payload, reason = population.panel_coverage_ladder(b)
    if reason:
        return ""
    p = payload
    n = p["n"]
    seen_tier2 = False
    rows = []
    for entry in p["ladder"]:
        if entry["band"] == "TIER-2 COHORT":
            if seen_tier2:
                continue
            seen_tier2 = True
            label = ("TIER-2 COHORT: "
                     + ", ".join(p["tier2_columns"]))
            flag = (f'verified as ONE cohort — a five-way AND returns '
                    f'{_num(p["tier2_cohort_and"])} rows, the same count each '
                    f'column carries alone')
            rows.append((
                f'<strong>{_e(label)}</strong>',
                _fig(_num(p["tier2_cohort_and"]),
                     _of(p["tier2_cohort_and"], n, "rows")),
                _e("—"),
                f'<span class="flag">{_e(flag)}</span>'))
            continue
        flag = ""
        if entry["col"] == "condition":
            flag = (f'{1 - entry["fill"]:.1%} NULL — new-vs-used is NOT '
                    f'knowable on this population')
        elif entry["col"] == "seller_feedback_bp":
            flag = (f'{_num(p["feedback_bp_zero_with_ratings"])} rows carry '
                    f'bp=0 beside a non-zero rating count — an unread field, '
                    f'not a 0% seller')
        rows.append((
            _e(entry["col"]),
            _fig(f'{entry["fill"]:.2%}', _of(entry["non_null"], n, "non-null")),
            _e(_num(entry["distinct"])),
            (f'<span class="flag">{_e(flag)}</span>' if flag
             else f'<span class="meta">{_e(entry["band"])}</span>')))
    body = [
        '<p class="meta">Every stored column plus the derived date, sorted '
        'ascending — the cliff between what Tier 1 fills and what only Tier 2 '
        'touches IS the shape of this panel.</p>',
        _table(("column", "fill", "distinct values", "band / flag"), rows,
               cls="tall"),
    ]
    return _panel("coverage_ladder", "P4 · Coverage ladder",
                  "Which columns actually exist on this population?",
                  "".join(body))


def _render_category_integrity(b: Build) -> str:
    payload, reason = population.panel_category_integrity(b)
    if reason:
        return ""
    p = payload
    headers = ("category", "listings") + tuple(
        c.replace("_cov", "") for c in p["cov_cols"])
    rows = []
    for r in p["rows"]:
        cells = [_e(r["category"]),
                 _fig(_num(r["n"]), _of(r["n"], b.n, "rows"))]
        for c in p["cov_cols"]:
            known = int(round(r[c] * r["n"]))
            cells.append(_fig(f'{r[c]:.1%}', _of(known, r["n"])))
        rows.append(tuple(cells))
    worst = p["worst_category"]
    caption = ""
    if worst:
        caption = (
            f'The global seller coverage of {p["global_seller_cov"]:.2%} is '
            f'not evenly spread: {worst["category"]} carries '
            f'{worst["seller_cov"]:.2%} on {_num(worst["n"])} rows and drags '
            f'the whole figure. Its leaves are the ones '
            f'P3 flags and P7 excludes.')
    body = [
        _table(headers, rows),
        f'<p class="meta">{_e(caption)}</p>' if caption else "",
        '<p class="meta">See <a href="#panel-green_because_broken">P3</a>.</p>',
    ]
    return _panel("category_integrity", "P5 · Category integrity",
                  "Is the coverage deficit global, or one department?",
                  "".join(body))


def _render_constants(b: Build) -> str:
    payload, reason = population.panel_constants_and_contradictions(b)
    if reason:
        return ""
    p = payload
    chips = "".join(
        f'<div class="band"><code>{_e(c["col"])}</code> = '
        f'{_e(_fmt(c["value"]))} <small>({_e(c["kind"])}, '
        f'{_num(c["non_null"])} rows)</small></div>'
        for c in p["constants"])
    never = "".join(f'<div class="band"><code>{_e(c["col"])}</code> '
                    '<small>never populated</small></div>'
                    for c in p["never_populated"])

    blocks = []
    for c in p["contradictions"]:
        blocks.append(
            f'<div class="crit"><strong>{_e(c["name"])}</strong><br>'
            f'contract: {_e(c["contract"])}<br>'
            f'observed: <code>{_e(json.dumps(c["observed"], default=str))}</code>'
            f'<br>{_e(c["reading"])}</div>')

    body = ['<div class="bands">' + chips + never + '</div>'] + blocks
    return _panel("constants", "P6 · Constants and contradictions",
                  "Which columns carry no information, and where does the "
                  "data disagree with the contract that describes it?",
                  "".join(body))


# --- zone 2 --------------------------------------------------------------------------

def _render_density(b: Build) -> str:
    payload, reason = population.panel_competition_density(b)
    if reason:
        return ""
    p = payload
    points = [{
        "x": e["sellers"], "y": e["top5_share"], "r": e["n"], "cat": e["category"],
        "title": (f'leaf {e["leaf"]} · {e["sellers"]} sellers · '
                  f'top-5 {e["top5_share"]:.1%} · HHI {e["hhi"]:.4f} · '
                  f'{e["n"]} listings'),
    } for e in p["measured"] if e["top5_share"] is not None]

    rows = [(
        _e(e["leaf"]),
        _e(e["category"]),
        _fig(_num(e["sellers"]),
             _of(e["seller_known"], e["n"], "rows with a seller")),
        _fig(f'{e["top5_share"]:.1%}',
             _of(min(5, e["sellers"]), e["sellers"], "sellers")),
        _fig(f'{e["hhi"]:.4f}', _of(e["seller_known"], e["n"])),
    ) for e in p["measured"][:12]]

    excluded = [(
        _e(e["leaf"]),
        _e(e["category"]),
        _fig(f'{e["seller_cov"]:.2%}',
             _of(e["seller_known"], e["n"], "rows with a seller")),
        '<span class="flag">seller data missing — not measurable</span>',
    ) for e in p["excluded"]]

    locked_payload, locked_reason = population.panel_seller_entry_exit(b)
    entry_exit = ""
    if locked_reason:
        entry_exit = ('<div class="panel locked" data-panel="seller_entry_exit">'
                      '<h3>Seller entry and exit</h3>'
                      + _locked_body(locked_reason) + "</div>")
    else:
        entry_exit = (
            '<div class="panel" data-panel="seller_entry_exit">'
            '<h3>Seller entry and exit</h3><p>'
            + _fig(_num(len(locked_payload["entered"])),
                   _of(len(locked_payload["entered"]),
                       len(locked_payload["entered"]) + locked_payload["persisting"],
                       "sellers new since the previous date"))
            + "</p><p>"
            + _fig(_num(len(locked_payload["exited"])),
                   _of(len(locked_payload["exited"]),
                       len(locked_payload["exited"]) + locked_payload["persisting"],
                       "sellers gone since the previous date"))
            + "</p></div>")

    body = [
        f'<p class="meta">Gated at {p["gate"]:.0%} seller coverage: '
        f'{len(p["measured"])} of {p["in_scope"]} in-scope leaves are '
        f'measurable. Dot area = listings swept, colour = department.</p>',
        _scatter(points, x_label="distinct KNOWN sellers in the leaf",
                 y_label="top-5 seller share"),
        '<p class="meta">Thinnest twelve by top-5 share:</p>',
        _table(("leaf", "department", "distinct sellers", "top-5 share", "HHI"),
               rows),
        f'<p class="flag">Excluded — seller data missing, not measurable '
        f'({len(p["excluded"])} leaves):</p>',
        _table(("leaf", "department", "seller coverage", "why"), excluded)
        if excluded else '<p class="meta">(none)</p>',
        '<p class="meta">Without this gate all of the above show a 100% top-5 '
        'share off a handful of known sellers and rank as the store\'s most '
        'concentrated markets. See '
        '<a href="#panel-green_because_broken">P3</a>.</p>',
        entry_exit,
    ]
    return _panel("competition_density", "P7 · Competition density",
                  "Where is competition thin?", "".join(body))


def _render_footprint(b: Build) -> str:
    payload, reason = population.panel_seller_footprint(b)
    if reason:
        return ""
    p = payload
    depth = [(_e(s["seller_id"]),
              _fig(_num(s["listings"]), _of(s["listings"], p["known_rows"],
                                            "rows with a known seller")),
              _e(_num(s["leaves"])))
             for s in p["by_depth"]]
    breadth = [(_e(s["seller_id"]),
                _fig(_num(s["leaves"]), _of(s["leaves"], 67, "leaves swept")),
                _e(_num(s["listings"])))
               for s in p["by_breadth"]]
    body = [
        '<p>' + _fig(_num(p["n_sellers"]),
                     _of(p["known_rows"], p["n"], "rows carry a seller id"))
        + " distinct sellers</p>",
        '<div class="cols2"><div><p class="meta">By depth (listings):</p>'
        + _table(("seller_id", "listings", "leaves"), depth)
        + '</div><div><p class="meta">By breadth (leaves):</p>'
        + _table(("seller_id", "leaves", "listings"), breadth) + "</div></div>",
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["one_leaf"]), _of(p["one_leaf"], p["n_sellers"], "in exactly one leaf"))}</div>'
        f'<div class="band">{_fig(_num(p["two_plus"]), _of(p["two_plus"], p["n_sellers"], "in 2+ leaves"))}</div>'
        f'<div class="band">{_fig(_num(p["five_plus"]), _of(p["five_plus"], p["n_sellers"], "in 5+ leaves"))}</div>'
        f'<div class="band">{_fig(_num(p["max_leaves"]), _of(1, p["n_sellers"], "widest seller"))}</div>'
        '</div>',
        '<p class="meta">Sellers are named by <code>seller_id</code> only.</p>',
    ]
    return _panel("seller_footprint", "P8 · Seller footprint",
                  "Who is actually in this market?", "".join(body))


def _render_landed_price(b: Build) -> str:
    payload, reason = population.panel_landed_price(b)
    if reason:
        return ""
    p = payload
    lo, hi = p["band"]
    bins = p["histogram"]
    shade = None
    if bins:
        idx = [i for i, entry in enumerate(bins) if lo <= entry["bin"] <= hi]
        if idx:
            shade = (min(idx), max(idx))
    chart = _bar_chart([(_fmt_pence(e["bin"]), e["count"]) for e in bins],
                       shade=shade, label_w=70, bar_h=8, width=620)
    body = [
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["known"]), _of(p["known"], p["n"], "landed prices known"))}</div>'
        f'<div class="band">{_fig(_num(p["in_band"]), _of(p["in_band"], p["known"], "inside the knob band"))}</div>'
        f'<div class="band">{_fig(_num(p["below"]), _of(p["below"], p["known"], "below the floor"))}</div>'
        f'<div class="band">{_fig(_num(p["above"]), _of(p["above"], p["known"], "above the ceiling"))}</div>'
        f'<div class="band">{_fig(_num(p["unknown"]), _of(p["unknown"], p["n"], "delivery not stated"))}</div>'
        '</div>',
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["free_delivery"]), _of(p["free_delivery"], p["known"], "landed == headline"))}</div>'
        f'<div class="band">{_fig(_num(p["paid_delivery"]), _of(p["paid_delivery"], p["known"], "landed > headline"))}</div>'
        f'<div class="band">{_fig(_num(p["impossible"]), _of(p["impossible"], p["known"], "landed < headline (impossible)"))}</div>'
        f'<div class="band">{_fig(_fmt_pence(int(p["median_uplift"]) if p["median_uplift"] is not None else None), _of(p["paid_delivery"], p["known"], "median postage uplift"))}</div>'
        '</div>',
        f'<p class="meta">Histogram in £2.50 bins; the shaded band is the '
        f'price_band knob ({_fmt_pence(lo)}–{_fmt_pence(hi)}).</p>',
        chart,
        f'<div class="crit">{_pct(p["below"], p["known"])} of every sweep '
        f'returns below the floor the shard claims to have filtered on — the '
        f'band filter is on the headline price, not the landed one.</div>',
    ]
    return _panel("landed_price", "P9 · Landed price population",
                  "Does the population sit where the price knob says it does?",
                  "".join(body))


def _render_leaf_price_map(b: Build) -> str:
    payload, reason = population.panel_leaf_price_map(b)
    if reason:
        return ""
    p = payload
    lo, hi = p["band"]
    rows = p["rows"]
    if not rows:
        return _panel("leaf_price_map", "P10 · Leaf price map", "",
                      '<p class="meta">(no in-scope leaf carries a landed price)</p>')
    top = max((r["p75"] or 0) for r in rows) or 1
    row_h, label_w, plot_w, gutter_w = 15, 78, 380, 150
    width = label_w + plot_w + gutter_w + 14
    height = len(rows) * row_h + 22
    x = lambda v: label_w + min(plot_w, plot_w * (v / top))  # noqa: E731
    svg = [f'<svg viewBox="0 0 {width} {height}" role="img">']
    svg.append(f'<rect x="{x(lo):.1f}" y="0" width="{x(hi) - x(lo):.1f}" '
               f'height="{height - 18}" fill="#16281a"/>')
    for i, r in enumerate(rows):
        y = i * row_h + 4
        svg.append(f'<text x="{label_w - 5}" y="{y + 8}" fill="#9aa4b2" '
                   f'font-size="8" text-anchor="end">{_e(r["leaf"])}</text>')
        if r["p25"] is None:
            svg.append(f'<text x="{label_w + 4}" y="{y + 8}" fill="#5a6270" '
                       f'font-size="8">no landed price</text>')
            continue
        svg.append(
            f'<rect x="{x(r["p25"]):.1f}" y="{y + 1}" '
            f'width="{max(1, x(r["p75"]) - x(r["p25"])):.1f}" height="7" '
            f'fill="#316a8c" fill-opacity="0.75"/>'
            f'<line x1="{x(r["p50"]):.1f}" y1="{y}" x2="{x(r["p50"]):.1f}" '
            f'y2="{y + 9}" stroke="#e6e6e6" stroke-width="1.2">'
            f'<title>leaf {_e(r["leaf"])}: p25 {_fmt_pence(int(r["p25"]))} · '
            f'p50 {_fmt_pence(int(r["p50"]))} · p75 {_fmt_pence(int(r["p75"]))} · '
            f'{r["known"]} of {r["n"]} known</title></line>')
    svg.append(f'<line x1="{label_w + plot_w + 7}" y1="0" '
               f'x2="{label_w + plot_w + 7}" y2="{height - 18}" '
               f'stroke="#3a3f4d" stroke-dasharray="3 3"/>')
    svg.append(f'<text x="{label_w + plot_w + 13}" y="12" fill="#e0a955" '
               f'font-size="8">{_e(p["gutter_label"])}</text>')
    svg.append(f'<text x="{label_w}" y="{height - 5}" fill="#9aa4b2" '
               f'font-size="8">£0</text>'
               f'<text x="{label_w + plot_w}" y="{height - 5}" fill="#9aa4b2" '
               f'font-size="8" text-anchor="end">{_fmt_pence(int(top))}</text>')
    svg.append("</svg>")

    body = [
        f'<p class="meta">{len(rows)} in-scope leaves, ordered by median '
        f'landed price. Shaded band = the price_band knob '
        f'({_fmt_pence(lo)}–{_fmt_pence(hi)}). The right gutter is reserved, '
        f'sized and labelled now so that when a second sweep lands the '
        f'sparklines fill it and no pixel moves.</p>',
        _chart("".join(svg), width),
        '<p>' + _fig(
            _num(sum(1 for r in rows
                     if r["p50"] is not None and lo <= r["p50"] <= hi)),
            _of(len([r for r in rows if r["p50"] is not None]), len(rows),
                "leaves whose median sits inside the band"))
        + "</p>",
    ]
    return _panel("leaf_price_map", "P10 · Leaf price map",
                  "Which leaves live inside the price band, and which only "
                  "overlap it?", "".join(body))


def _render_saturation(b: Build) -> str:
    payload, reason = population.panel_saturation_calibration(b)
    if reason:
        return ""
    p = payload
    sizes = [(f"{k} listing{'' if k == 1 else 's'}", v)
             for k, v in list(p["size_histogram"].items())[:10]]
    sellers = [(f"{k} seller{'' if k == 1 else 's'}", v)
               for k, v in list(p["sellers_per_product"].items())[:10]]
    cap_label = f'groups above max_competitors={p["max_competitors"]}'
    body = [
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["total_groups"]), _of(p["total_groups"], b.n, "listings grouped into products"))}</div>'
        f'<div class="band">{_fig(_pct(p["singletons"], p["total_groups"]), _of(p["singletons"], p["total_groups"], "single-listing products"))}</div>'
        f'<div class="band">{_fig(_num(p["over_cap"]), _of(p["over_cap"], p["total_groups"], cap_label))}</div>'
        '</div>',
        '<div class="cols2"><div><p class="meta">Listings per product group:</p>'
        + _bar_chart(sizes, width=470, label_w=90)
        + '</div><div><p class="meta">Distinct sellers per product group:</p>'
        + _bar_chart(sellers, width=470, label_w=90) + "</div></div>",
        f'<div class="crit">{_e(p["verdict"])}</div>',
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["epid_multi_seller"]), _of(p["epid_multi_seller"], p["n_epids"], "epids carried by more than one seller"))}</div>'
        f'<div class="band">{_fig(_num(p["epid_multi_leaf"]), _of(p["epid_multi_leaf"], p["n_epids"], "epids spanning more than one leaf"))}</div>'
        '</div>',
        '<p class="meta">Grouping is one pass of '
        '<code>board.group_products_in_leaf</code>, per leaf — '
        '<code>identity.cluster()</code> refuses rows spanning more than one, '
        'so no aggregate here crosses a leaf boundary.</p>',
    ]
    return _panel("saturation_calibration", "P11 · Saturation calibration",
                  "Is knob 6 measuring anything?", "".join(body))


def _render_demand(b: Build) -> str:
    payload, reason = population.panel_demand_evidence(b)
    if reason:
        return ""
    p = payload
    rows = [(
        _e(r["leaf"]),
        _fig(_num(r["n"]), _of(r["n"], p["n"], "listings")),
        _fig(_pct(r["sold_known"], r["n"]), _of(r["sold_known"], r["n"], "with a sold badge")),
        _e(_fmt(r["sold_median"])),
        _fig(_num(r["watched"]), _of(r["watched"], r["n"], "watchers > 0")),
        _fig(_num(r["scarce"]), _of(r["scarce"], r["n"], "scarcity badge")),
    ) for r in p["rows"][:25]]
    hot = [(_e(h["source_id"]), _e(h["leaf"]), _e(h["watchers"]),
            _e(h["stock_hint"])) for h in p["hot"][:25]]
    caveats = "".join(f'<div class="caveat">{_e(c)}</div>' for c in p["caveats"])
    body = [
        '<div class="bands">'
        f'<div class="band">{_fig(_pct(p["sold_known"], p["n"]), _of(p["sold_known"], p["n"], "listings show a sold badge"))}</div>'
        f'<div class="band">{_fig(_num(p["sold_max"]), _of(p["sold_known"], p["n"], "largest badge seen"))}</div>'
        f'<div class="band">{_fig(_pct(p["listed_at_rows"], p["n"]), _of(p["listed_at_rows"], p["n"], "carry listed_at"))}</div>'
        '</div>',
        caveats,
        _table(("leaf", "listings", "sold badge coverage", "median badge",
                "watched", "scarce"), rows, cls="tall"),
        '<p class="meta">Watched AND scarce, by bare source_id:</p>',
        _table(("source_id", "leaf", "watchers", "stock hint"), hot)
        if hot else '<p class="meta">(no listing carries both signals)</p>',
    ]
    return _panel("demand_evidence", "P12 · Demand evidence by leaf",
                  "What evidence of demand exists, and what is it not?",
                  "".join(body))


def _render_origin(b: Build) -> str:
    payload, reason = population.panel_origin_and_fulfilment(b)
    if reason:
        return ""
    p = payload
    origin_rows = [(_e(t["country"]),
                    _fig(_num(t["n"]), _of(t["n"], p["n"], "listings")))
                   for t in p["top"]]
    origin_rows.append(("other",
                        _fig(_num(p["other"]), _of(p["other"], p["n"], "listings"))))
    origin_rows.append(("(unknown)",
                        _fig(_num(p["unknown"]), _of(p["unknown"], p["n"], "listings"))))
    eta_rows = [(_e(e["eta_days"]),
                 _fig(_num(e["n"]), _of(e["n"], p["eta_known"], "known ETAs")))
                for e in p["eta"]]
    modal = p["eta_modal"]
    sentence = ""
    if modal:
        sentence = (
            f'The delivery knob is measured on {p["eta_known"] / p["n"]:.2%} of '
            f'the population, and {_num(modal["n"])} of {_num(p["eta_known"])} '
            f'known values are exactly {modal["eta_days"]} days — it cannot '
            f'gate anything today (threshold: {p["max_eta_days"]} days).')
    body = [
        '<div class="cols2"><div><p class="meta">Origin country:</p>'
        + _table(("country", "listings"), origin_rows)
        + '</div><div><p class="meta">Promised delivery (max days):</p>'
        + _table(("eta_days", "listings"), eta_rows) + "</div></div>",
        f'<div class="crit">{_e(sentence)}</div>' if sentence else "",
    ]
    return _panel("origin_fulfilment", "P13 · Origin and fulfilment",
                  "Where does it ship from, and how fast?", "".join(body))


# --- zone 3 -----------------------------------------------------------------------

def _json_blob(payload: dict) -> str:
    """The explorer payload as script-safe JSON.

    `<` is emitted as `\\u003c` throughout, so a listing title containing
    `</script>` cannot break out of the blob. `population._sanitise` has
    already replaced any url in a string value, so no `https://` substring
    survives into the page.
    """
    dumped = json.dumps(payload, separators=(",", ":"), default=str)
    return dumped.replace("<", "\\u003c")


def _render_explorer(b: Build, *, limit: int | None) -> str:
    payload, reason = population.panel_explorer(b, limit=limit)
    if reason:
        return ""
    p = payload
    blob = _json_blob(p["payload"])
    facets = p["facets"]
    cat_opts = "".join(f'<option value="{_e(c)}">{_e(c)}</option>'
                       for c in facets["category"])
    leaf_opts = "".join(f'<option value="{_e(c)}">{_e(c)}</option>'
                        for c in facets["leaf"])
    origin_opts = "".join(f'<option value="{_e(c)}">{_e(c)}</option>'
                          for c in facets["origin"])
    lo, hi = p["band"]
    body = [
        '<p>' + _fig(_num(p["shown"]), _of(p["shown"], p["n"], "listings in "
                                           "the payload"))
        + " — every column below is a whitelist; <code>url</code> is absent by "
          "construction.</p>",
        '<div class="ctrl">'
        f'<input id="q" type="search" placeholder="title contains…" size="22">'
        f'<select id="f-cat"><option value="">all departments</option>{cat_opts}</select>'
        f'<select id="f-leaf"><option value="">all leaves</option>{leaf_opts}</select>'
        f'<select id="f-origin"><option value="">all origins</option>{origin_opts}</select>'
        '<select id="f-band"><option value="">any landed price</option>'
        f'<option value="in">inside {_fmt_pence(lo)}–{_fmt_pence(hi)}</option>'
        '<option value="below">below the floor</option>'
        '<option value="above">above the ceiling</option>'
        '<option value="null">delivery not stated</option></select>'
        '<select id="f-fb"><option value="">any feedback</option>'
        '<option value="hi">&gt;= 99.0%</option>'
        '<option value="lo">&lt; 99.0%</option>'
        '<option value="null">unknown</option></select>'
        '<select id="f-flag"><option value="">no extra filter</option>'
        '<option value="watched">watched (&gt;0)</option>'
        '<option value="sold">has a sold count</option>'
        '<option value="seller">seller known</option>'
        '<option value="noseller">seller UNKNOWN</option></select>'
        '<select id="sort">'
        '<option value="total_pence">sort: landed price</option>'
        '<option value="price_pence">sort: headline price</option>'
        '<option value="sold_qty">sort: sold badge</option>'
        '<option value="seller_feedback_n">sort: feedback count</option>'
        '<option value="title">sort: title</option></select>'
        '<label><input type="checkbox" id="desc" checked> desc</label>'
        '<span id="count" class="meta"></span></div>',
        '<div id="pop-scroll"><div class="prow head">'
        '<span>source_id</span><span>title</span><span>headline</span>'
        '<span>landed</span><span>leaf</span><span>seller</span>'
        '<span>sold</span><span>watch</span></div>'
        '<div id="pop-spacer"><div id="pop-rows"></div></div></div>',
        '<p class="meta">Rendered ~100 rows at a time against the full blob; '
        'a 29,602-row DOM table does not survive. Sorting an unknown never '
        'promotes it to zero — unknowns sort last in both directions.</p>',
        f'<script type="application/json" id="pop">{blob}</script>',
        f"<script>var LO={int(lo)},HI={int(hi)};{_EXPLORER_JS}</script>",
    ]
    return _panel("population_explorer", "P14 · Population explorer",
                  "Every listing that was scraped, filterable.", "".join(body))


_EXPLORER_JS = r"""
(function(){
  var el=document.getElementById('pop'); if(!el) return;
  var raw=JSON.parse(el.textContent);
  var F=raw.fields, N=raw.n, C={};
  F.forEach(function(f){
    var c=raw.cols[f];
    if(c.t==='dict'){ C[f]=c.v.map(function(i){return i===null?null:c.d[i];}); }
    else { C[f]=c.v; }
  });
  var idx=[]; for(var i=0;i<N;i++) idx.push(i);
  var view=idx.slice();
  var scroll=document.getElementById('pop-scroll');
  var spacer=document.getElementById('pop-spacer');
  var rowsEl=document.getElementById('pop-rows');
  var countEl=document.getElementById('count');
  var RH=26, WINDOW=100;

  function money(p){ if(p===null||p===undefined) return '?';
    var s=p<0?'-':'', a=Math.abs(p);
    return s+'£'+Math.floor(a/100)+'.'+('0'+(a%100)).slice(-2); }
  function txt(v){ return (v===null||v===undefined)?'—':String(v); }

  function pass(i){
    var q=document.getElementById('q').value.toLowerCase();
    if(q){ var t=C.title[i]; if(!t||t.toLowerCase().indexOf(q)<0) return false; }
    var c=document.getElementById('f-cat').value;
    if(c && C.category[i]!==c) return false;
    var lf=document.getElementById('f-leaf').value;
    if(lf && C.leaf_category_id[i]!==lf) return false;
    var o=document.getElementById('f-origin').value;
    if(o && C.origin_country[i]!==o) return false;
    var band=document.getElementById('f-band').value, tp=C.total_pence[i];
    if(band==='null' && tp!==null) return false;
    if(band==='in' && !(tp!==null && tp>=LO && tp<=HI)) return false;
    if(band==='below' && !(tp!==null && tp<LO)) return false;
    if(band==='above' && !(tp!==null && tp>HI)) return false;
    var fb=document.getElementById('f-fb').value, bp=C.seller_feedback_bp[i];
    if(fb==='null' && bp!==null) return false;
    if(fb==='hi' && !(bp!==null && bp>=9900)) return false;
    if(fb==='lo' && !(bp!==null && bp<9900)) return false;
    var fl=document.getElementById('f-flag').value;
    if(fl==='watched' && !(C.watchers[i]>0)) return false;
    if(fl==='sold' && C.sold_qty[i]===null) return false;
    if(fl==='seller' && !C.seller_id[i]) return false;
    if(fl==='noseller' && C.seller_id[i]) return false;
    return true;
  }

  function apply(){
    view=idx.filter(pass);
    var key=document.getElementById('sort').value;
    var desc=document.getElementById('desc').checked;
    var col=C[key];
    view.sort(function(a,bb){
      var x=col[a], y=col[bb];
      if(x===null&&y===null) return 0;
      if(x===null) return 1;
      if(y===null) return -1;
      if(x===y) return 0;
      return (x>y ? 1 : -1) * (desc ? -1 : 1);
    });
    countEl.textContent=view.length+' of '+N+' listings match';
    spacer.style.height=(view.length*RH)+'px';
    draw();
  }

  function draw(){
    var first=Math.max(0, Math.floor(scroll.scrollTop/RH)-10);
    var last=Math.min(view.length, first+WINDOW);
    var out=[];
    for(var k=first;k<last;k++){
      var i=view[k];
      out.push('<div class="prow" style="position:absolute;top:'+(k*RH)+'px;left:0;right:0">'
        +'<span>'+esc(C.source_id[i])+'</span>'
        +'<span class="t" title="'+esc(C.title[i])+'">'+esc(C.title[i])+'</span>'
        +'<span>'+money(C.price_pence[i])+'</span>'
        +'<span>'+money(C.total_pence[i])+'</span>'
        +'<span>'+esc(txt(C.leaf_category_id[i]))+'</span>'
        +'<span class="t">'+esc(txt(C.seller_id[i]))+'</span>'
        +'<span>'+esc(txt(C.sold_qty[i]))+'</span>'
        +'<span>'+esc(txt(C.watchers[i]))+'</span>'
        +'<'+'/div>');
    }
    rowsEl.innerHTML=out.join('');
  }
  function esc(s){ if(s===null||s===undefined) return '—';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                    .replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  ['q','f-cat','f-leaf','f-origin','f-band','f-fb','f-flag','sort','desc']
    .forEach(function(id){
      var n=document.getElementById(id);
      n.addEventListener('input',apply); n.addEventListener('change',apply);
    });
  scroll.addEventListener('scroll',draw);
  apply();
})();
"""


# --- zone 4 ------------------------------------------------------------------------

def _render_deepen(b: Build) -> str:
    payload, reason = population.panel_deepen_budget(b)
    if reason:
        return ""
    p = payload
    rows = [(
        _e(r["leaf"]),
        _fig(_num(r["deepened"]), _of(r["deepened"], p["deepened"],
                                      "of the Tier-2 cohort")),
        _fig(_num(r["business"]), _of(r["business"], r["deepened"], "business")),
        _fig(_num(r["priced"]), _of(r["priced"], r["business"],
                                    "business rows with a landed price")),
        _fig(_num(r["eta"]), _of(r["eta"], r["business"],
                                 "business rows with an ETA")),
        _e(_num(r.get("price_median_denominator"))),
    ) for r in p["rows"]]
    arithmetic = []
    for r in p["rows"][:4]:
        arithmetic.append(
            f'leaf {r["leaf"]} deepened {r["deepened"]} listings and yielded '
            f'{r["business"]} business sellers, {r["priced"]} with a price, '
            f'{r["eta"]} with an ETA')
    cost = ""
    if p["needed_listings"]:
        cost = (
            f'A denominator of {p["target"]} in each of '
            f'{len(b.in_scope)} in-scope leaves needs '
            f'~{_num(p["needed_business"])} business rows; at today\'s '
            f'{p["business"]}/{p["deepened"]} business rate that is '
            f'~{_num(p["needed_listings"])} listings deepened, '
            f'~{p["multiple"]:.0f}x the pass that has run.')
    body = [
        '<div class="bands">'
        f'<div class="band">{_fig(_num(p["deepened"]), _of(p["deepened"], p["n"], "listings deepened"))}</div>'
        f'<div class="band">{_fig(_num(p["business"]), _of(p["business"], p["deepened"], "business"))}</div>'
        f'<div class="band">{_fig(_num(p["private"]), _of(p["private"], p["deepened"], "private"))}</div>'
        f'<div class="band">{_fig(_num(p["leaves_touched"]), _of(p["leaves_touched"], 67, "leaves touched"))}</div>'
        f'<div class="band">{_fig(_num(p["zero_denominator"]), _of(p["zero_denominator"], p["n_niches"], "niches with NO business landed price"))}</div>'
        '</div>',
        _table(("leaf", "deepened", "business", "priced", "with an ETA",
                "price_median_denominator"), rows, cls="tall"),
        '<p class="meta">' + "; ".join(_e(a) for a in arithmetic) + ".</p>",
        f'<div class="crit">{_e(cost)}</div>' if cost else "",
        f'<div class="ok">{_e(p["closing"])}</div>',
    ]
    return _panel("deepen_budget", "P15 · Deepen budget and yield",
                  "What did Tier 2 buy, and what would the rest cost?",
                  "".join(body))


def _render_sweep_footprint(b: Build) -> str:
    payload, reason = population.panel_sweep_footprint(b)
    if reason:
        return ""
    p = payload
    date_rows = [(
        _e(d["date"]),
        _fig(_num(d["raw_rows"]), _of(d["listings"], d["raw_rows"],
                                      "distinct listings")),
        _e(str(d["first_seen"])), _e(str(d["last_seen"])),
    ) for d in p["per_date"]]

    sizes = [(f'leaf {r["leaf"]}', r["n"]) for r in p["per_leaf"][:20]]
    shards = [(_e(r["shard"]),
               _fig(_num(r["n"]), _of(r["n"], b.n, "listings")))
              for r in p["per_shard"][:15]]
    strays = [(_e(r["leaf"]), _fig(_num(r["n"]), _of(r["n"], b.n, "listings")))
              for r in p["strays"]]

    if p["checkpoint_found"]:
        blocked = p["blocked"] or {}
        dumped = population._sanitise(json.dumps(blocked))[:400]
        cp = ('<p>' + _fig(_num(p["shards_done"]),
                           _of(len(blocked), p["shards_done"] or 0,
                               "shards blocked"))
              + ' shards recorded in the checkpoint</p>'
              + (f'<div class="crit">blocked: <code>{_e(dumped)}</code></div>'
                 if blocked else '<div class="ok">no shard was blocked</div>'))
    else:
        cp = (f'<div class="crit">no checkpoint found at '
              f'<code>{_e(p["checkpoint_path"])}</code> — that is an absent '
              f'file, NOT a zero blocked count.</div>')

    body = [
        _table(("date", "raw rows", "first seen (UTC)", "last seen (UTC)"),
               date_rows),
        f'<p class="meta">Listings SWEPT per leaf (capped ~{p["cap"]}/leaf: '
        f'3 pages at ~240 results) — top 20:</p>',
        _bar_chart(sizes, width=620, label_w=90),
        '<div class="cols2"><div><p class="meta">Rows per page shard:</p>'
        + _table(("shard", "listings"), shards)
        + '</div><div><p class="meta">Out-of-catalogue leaves swept:</p>'
        + (_table(("leaf", "listings"), strays) if strays
           else '<p class="meta">(none)</p>') + "</div></div>",
        cp,
        f'<div class="crit">{_e(p["exit_note"])}</div>',
        '<p>' + _fig(_num(p["reobserved"]),
                     _of(p["reobserved_moved"], p["reobserved"],
                         "at a different price"))
        + ' listings were observed more than once on this date. '
          'That is sweep footprint — overlapping shards inside one pass — '
          'not change over time, and it is stated here so it is never '
          'promoted into a trend panel.</p>',
    ]
    return _panel("sweep_footprint", "P16 · Sweep footprint and health",
                  "Did the night actually work?", "".join(body))


# --- zone 5 --------------------------------------------------------------------------

def _render_date_ledger(b: Build) -> str:
    payload, _ = population.panel_date_ledger(b)
    p = payload
    ticks = "".join('<span class="tick on" title="' + d.isoformat() + '"></span>'
                    for d in p["dates"]) or '<span class="tick"></span>'
    rows = [(
        _e(u["need"]),
        ("<span class=\"pass\">unlocked</span>" if p["have"] >= u["need"]
         else f'<span class="unknown">needs {u["need"] - p["have"]} more</span>'),
        _e(u["what"]),
    ) for u in p["unlocks"]]
    blockers = "".join(
        f'<div class="crit"><strong>{_e(name)}</strong><br>{_e(why)}</div>'
        for name, why in p["blockers"])
    body = [
        f'<p>{ticks} <small>'
        f'{_e(", ".join(d.isoformat() for d in p["dates"]) or "no dates")}'
        f'</small></p>',
        f'<p class="headline">{_e(p["have"])} snapshot date'
        f'{"" if p["have"] == 1 else "s"} on file.</p>',
        _table(("dates", "status", "what it unlocks"), rows),
        blockers,
        f'<div class="caveat">{_e(p["consecutiveness"])}</div>',
    ]
    return _panel("date_ledger", "P17 · Date ledger and unlock calendar",
                  "What does the next sweep buy?", "".join(body))


def _render_sell_through(b: Build) -> str:
    payload, reason = population.panel_sell_through(b)
    p = payload
    rows = []
    for r in p["rows"][:60]:
        if r["str_window_bp"] is None:
            window = '<span class="dash">—</span>'
            verdict = '<span class="unknown">unreadable</span>'
        else:
            window = _fig(f'{r["str_window_bp"] / 100:.1f}%',
                          _of(r["n_known"], r["n_listings"],
                              "listings with a known delta"))
            ok = r["str_window_bp"] >= r["threshold_bp"]
            verdict = (f'<span class="{"pass" if ok else "fail"}">'
                       f'{"PASS" if ok else "FAIL"}</span>')
        rows.append((_e(r["niche"]), _e(r["str_source"]), window, verdict))
    banner = (f'sell_through: {p["unreadable"]} of {p["total"]} niches '
              f'unreadable')
    body = [
        f'<p class="{"flag" if p["unreadable"] else "meta"}">{_e(banner)}</p>',
        f'<p class="meta">Threshold: {p["threshold_bp"] / 100:.0f}% '
        '(knobs.str_threshold_bp). A negative delta is kept — a relist resets '
        "eBay's lifetime counter — and an unknown is never coerced to zero.</p>",
        f'<div class="{"scaffold" if reason else ""}">'
        + _table(("niche", "str_source", "windowed sell-through", "verdict"),
                 rows, cls="tall") + "</div>",
    ]
    return _panel("sell_through", "P18 · Sell-through window",
                  "Is anything actually selling?", "".join(body),
                  reason=reason)


def _render_churn(b: Build) -> str:
    payload, reason = population.panel_population_churn(b)
    p = payload
    if reason:
        frames = "".join(
            f'<div><p class="meta">{_e(name)}</p>' + _chart(
                '<svg viewBox="0 0 210 90" role="img">'
                '<line x1="30" y1="6" x2="30" y2="72" stroke="#2a2f3a"/>'
                '<line x1="30" y1="72" x2="200" y2="72" stroke="#2a2f3a"/>'
                '<text x="115" y="86" fill="#5a6270" font-size="8" '
                'text-anchor="middle">snapshot date</text>'
                '<text x="115" y="42" fill="#5a6270" font-size="8" '
                'text-anchor="middle">no marks — needs a 2nd sweep</text>'
                "</svg>", 260) + "</div>"
            for name in p["series"])
        body = (f'<div class="cols2">{frames}</div>'
                f'<p class="meta">{_num(p["would_compare"])} listings are '
                f'on file and would be compared against the next date.</p>')
        return _panel("population_churn", "P19 · Population churn",
                      "What changed since the last sweep?", body, reason=reason)
    body = ('<div class="bands">'
            f'<div class="band">{_fig(_num(p["new"]), _of(p["new"], b.n, "new listings"))}</div>'
            f'<div class="band">{_fig(_num(p["gone"]), _of(p["gone"], p["gone"] + p["persisting"], "gone since the previous date"))}</div>'
            f'<div class="band">{_fig(_num(p["persisting"]), _of(p["persisting"], b.n, "persisting"))}</div>'
            f'<div class="band">{_fig(_num(p["price_moved"]), _of(p["price_moved"], p["price_known"], "with a price on both dates"))}</div>'
            f'<div class="band">{_fig(_num(p["sold_known"]), _of(p["sold_known"], p["sold_listings"], "with a known sold delta"))}</div>'
            '</div>')
    return _panel("population_churn", "P19 · Population churn",
                  "What changed since the last sweep?", body)


def _render_coverage_trend(b: Build) -> str:
    payload, reason = population.panel_coverage_trend(b)
    p = payload
    labels = "".join(
        f'<text x="0" y="{10 + i * 9}" fill="#5a6270" font-size="7">'
        f'{_e(c)}</text>'
        for i, c in enumerate(p["columns"]))
    if reason:
        h = 20 + len(p["columns"]) * 9
        body = (
            f'<p class="meta">One line per stored column, one point per '
            f'snapshot date. The axis and every column label are drawn now; '
            f'only the marks are missing.</p>'
            + _chart(
                f'<svg viewBox="0 0 640 {h}" role="img">'
                f'<g transform="translate(4,0)">{labels}</g>'
                f'<line x1="120" y1="6" x2="120" y2="{h - 14}" '
                f'stroke="#2a2f3a"/>'
                f'<line x1="120" y1="{h - 14}" x2="630" y2="{h - 14}" '
                f'stroke="#2a2f3a"/>'
                f'<text x="375" y="{h - 3}" fill="#5a6270" font-size="8" '
                f'text-anchor="middle">snapshot date → (1 on file)</text>'
                "</svg>", 640)
            + '<div class="caveat">A drop in a column\'s fill rate between two '
            'dates is the earliest possible signal that eBay changed its '
            'markup or started blocking.</div>')
        return _panel("coverage_trend", "P20 · Coverage trend",
                      "Is the scrape degrading?", body, reason=reason)

    rows = [(
        _e(c),
        " ".join(f'{v:.1%}' for v in p["series"][c]),
    ) for c in p["columns"]]
    return _panel("coverage_trend", "P20 · Coverage trend",
                  "Is the scrape degrading?",
                  _table(("column", "fill rate per date"), rows, cls="tall"))


# --- document ------------------------------------------------------------------------

def _document(title: str, body: str) -> str:
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            "initial-scale=1\">"
            f"<title>{_e(title)}</title><style>{_STYLE}</style></head>"
            f"<body>{body}</body></html>")


def render(b: Build, *, include_explorer: bool = True,
           explorer_limit: int | None = None,
           generated_at: datetime | None = None) -> str:
    """The whole page. Zones render top to bottom; a locked panel holds its
    position and size at every date count."""
    if b.day is None:
        return _empty_page(b)

    generated_at = generated_at or datetime.now(UTC)
    zones = [
        ("", [_render_provenance(b)]),
        ("Zone 1 — can I read it",
         [_render_trust_ledger(b), _render_knob_matrix(b),
          _render_green_because_broken(b), _render_coverage_ladder(b),
          _render_category_integrity(b), _render_constants(b)]),
        ("Zone 2 — what the market looks like",
         [_render_density(b), _render_footprint(b), _render_landed_price(b),
          _render_leaf_price_map(b), _render_saturation(b),
          _render_demand(b), _render_origin(b)]),
        ("Zone 3 — the whole population",
         [_render_explorer(b, limit=explorer_limit) if include_explorer
          else _panel("population_explorer", "P14 · Population explorer",
                      "Every listing that was scraped, filterable.",
                      '<p class="meta">Omitted: this page was rendered with '
                      '<code>--summary</code>, which drops the explorer and '
                      'its payload entirely. Re-run without it for the full '
                      'population.</p>')]),
        ("Zone 4 — what to do next",
         [_render_deepen(b), _render_sweep_footprint(b)]),
        ("Zone 5 — locked until more dates land",
         [_render_date_ledger(b), _render_sell_through(b), _render_churn(b),
          _render_coverage_trend(b)]),
    ]
    parts = [_rail(b), '<div class="wrap">',
             "<h1>eBay population review</h1>",
             f'<p class="meta">generated {_e(generated_at.isoformat())} · '
             f'store <code>{_e(b.data_dir)}</code></p>']
    for heading, panels in zones:
        if heading:
            parts.append(f"<h2>{_e(heading)}</h2>")
        parts.extend(panels)
    parts.append("</div>")
    return _document("eBay population review", "".join(parts))


def write_report(b: Build, data_dir: Path | str, *, name: str | None = None,
                 include_explorer: bool = True,
                 explorer_limit: int | None = None) -> Path:
    """Write the page under DATA_DIR/reports/. Returns the path.

    Mirrors `board.write_report` -- same directory, same UTC-stamped name
    shape -- so the two reports land side by side and sort together.
    """
    out_dir = Path(data_dir) / _REPORT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)
    filename = name or f"population-{generated_at.strftime('%Y%m%dT%H%M%SZ')}.html"
    path = out_dir / filename
    path.write_text(
        render(b, include_explorer=include_explorer,
               explorer_limit=explorer_limit, generated_at=generated_at),
        encoding="utf-8")
    return path
