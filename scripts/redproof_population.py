"""Prove each test in tests/test_population.py can actually go red.

A pinning test nobody watched go red is decoration. Only test 1 here was
written before `phoneloop/population.py` existed and watched fail for the
right reason; the other eleven were written against a module that already
worked, so "it passes" is not evidence that any of them guards anything.

This closes that gap mechanically: for each test it injects, one at a time,
exactly the regression that test claims to catch, runs the single test, and
restores the file. A case that stays GREEN is reported as guarding nothing.
Last run: all twelve went red.

    python scripts/redproof_population.py       # from the repo root

MUTATES SOURCE FILES AND RESTORES THEM WITH `git checkout --`. It refuses to
start on a dirty tree, so the worst it can do is restore a committed file to
its committed state -- but do not run it with work you have not committed.
Not a pytest module: it lives under scripts/ precisely so a suite run never
collects it and starts editing phoneloop/ mid-flight.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = r"C:/Users/sampo/AppData/Local/Programs/Python/Python313/python.exe"
POP = ROOT / "phoneloop" / "population.py"
HTML = ROOT / "phoneloop" / "population_html.py"

# (test node id, file, old snippet, new snippet, what regression this is)
CASES = [
    ("TestLockedPanels::test_locked_panel_names_its_requirement", HTML,
     'lock = _locked_body(reason) if reason else ""',
     'lock = ""',
     "the locked card stops naming what it needs"),
    ("TestLockedPanels::test_two_date_fixture_fills_the_same_panel", POP,
     "    if len(b.dates) < 2:\n        return payload, unlock(need=2",
     "    if len(b.dates) < 3:\n        return payload, unlock(need=2",
     "the panel stays locked when a second date HAS landed"),
    ("TestSelfContained::test_self_contained_no_external_resources", HTML,
     '"<meta name=\\"viewport\\" content=\\"width=device-width,"',
     '"<link rel=\\"stylesheet\\" href=\\"https://fonts.example/x\\">"\n'
     '            "<meta name=\\"viewport\\" content=\\"width=device-width,"',
     "an external stylesheet is linked"),
    ("TestSelfContained::test_no_listing_url_reaches_the_output", POP,
     '"brand", "epid")\nEXPLORER_EXTRA_FIELDS',
     '"brand", "epid", "url")\nEXPLORER_EXTRA_FIELDS',
     "`url` joins the explorer whitelist"),
    ("TestSelfContained::test_url_in_a_title_is_stripped", POP,
     '    if not isinstance(value, str):\n        return value',
     '    if isinstance(value, str):\n        return value\n'
     '    if not isinstance(value, str):\n        return value',
     "_sanitise stops stripping urls out of strings"),
    ("TestSelfContained::test_script_close_tag_in_a_title_cannot_break_out", HTML,
     'return dumped.replace("<", "\\\\u003c")',
     "return dumped",
     "`<` is emitted raw in the JSON blob"),
    ("TestEveryFigureCarriesADenominator::test_every_figure_carries_a_denominator",
     HTML,
     "    return (f'<span class=\"figure\">{_e(value)}</span>'\n"
     "            f'<span class=\"denom\">{_e(denom)}</span>')",
     "    return f'<span class=\"figure\">{_e(value)}</span>'",
     "a figure is rendered without its denominator"),
    ("TestMissingDataPasses::test_missing_data_pass_is_flagged", POP,
     "DENSITY_MIN_SELLER_COVERAGE = 0.50",
     "DENSITY_MIN_SELLER_COVERAGE = 0.0",
     "the missing-data-pass flag is switched off"),
    ("TestMissingDataPasses::test_low_seller_coverage_leaf_is_excluded_from_density",
     POP,
     "DENSITY_GATE = 0.90",
     "DENSITY_GATE = 0.0",
     "the density panel stops gating on seller coverage"),
    # Two occurrences: the headline AND the panel title. Replacing only the
    # headline left the title intact and the test stayed green -- which was
    # the mutation being wrong, not the test being weak.
    ("TestEmptyStore::test_empty_store_renders_a_named_state", HTML,
     '+ _panel("empty_store", "no eBay snapshots", "", body) + "</div>")',
     '+ _panel("empty_store", "0 listings", "", '
     'body.replace("no eBay snapshots in ", "0 listings in ")) + "</div>")',
     "the empty store is reported as a zero instead of a named state"),
    ("TestSqlAgreesWithRowsOn::test_sql_and_rows_on_agree", POP,
     "  ) WHERE rn = 1\n)",
     "  )\n)",
     "the newest-per-source_id window is dropped from _LATEST"),
    ("TestWriteReport::test_writes_under_data_dir_reports", HTML,
     'out_dir = Path(data_dir) / _REPORT_DIR_NAME',
     'out_dir = Path(data_dir)',
     "the report is written outside DATA_DIR/reports"),
]


def dirty() -> bool:
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    return any(line for line in out.splitlines()
               if not line.endswith("redproof_population.py"))


def main() -> int:
    if dirty():
        print("refusing: working tree is dirty and the restore is "
              "`git checkout --`")
        return 2

    failures = 0
    for node, path, old, new, why in CASES:
        text = path.read_text(encoding="utf-8")
        if old not in text:
            print(f"!! SNIPPET NOT FOUND for {node}\n   {old[:70]!r}")
            failures += 1
            continue
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        try:
            r = subprocess.run(
                [PY, "-m", "pytest", f"tests/test_population.py::{node}",
                 "-q", "--no-header", "-x"],
                cwd=ROOT, capture_output=True, text=True,
                env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
            went_red = r.returncode != 0
            tail = [ln for ln in r.stdout.splitlines()
                    if ln.startswith(("E ", "FAILED", "assert"))][:3]
            print(f"{'RED  ' if went_red else 'GREEN'} {node}")
            print(f"      regression: {why}")
            for ln in tail:
                print(f"      {ln.strip()[:120]}")
            if not went_red:
                failures += 1
                print("      ^^ THIS TEST GUARDS NOTHING")
        finally:
            subprocess.run(["git", "checkout", "--", str(path)], cwd=ROOT,
                           capture_output=True)
    print()
    print("all twelve went red" if not failures
          else f"{failures} case(s) did not go red")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
