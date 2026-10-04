"""Supplier quotes, and whether each one clears the margin knob after returns
(docs/dropship-process.md, stage 2).

`fees.max_cost` answers knob 1 as a ceiling: the highest landed supplier
cost that still clears 25% or GBP 6 at a sale price. A ceiling is not a
test until a real supplier price is set against it. This module holds the
quote record and the test.

**Money is integer pence.** Every figure a quote carries is entered by hand
and parsed with `parse_pence`, which never goes through a float.

**Three states, never two.** A quote `clears`, `fails` or is `unknown`
against max_cost at the floor price and at the median price. An unknown
max_cost gives `unknown`. It is never read as zero, because a zero ceiling
would make every quote fail and a reader would take that as "no supplier
works" when the truth is "no price was observed".

**The returns allowance is not a knob.** It lives in this module and in
`~/.phoneloop/suppliers/returns.json`, never in `phoneloop.knobs.Knobs`. The
dashboard re-implements Knobs and pins it with a golden fixture
(dashboard/test/fixtures/knob-golden.json), and the allowance is not part of
that contract.

**A retail marketplace is refused for eBay.** eBay UK's drop shipping
policy: "Listing an item on eBay and then purchasing the item from another
retailer or marketplace that ships directly to your customer is not allowed
on eBay." Dropshipping from a wholesale supplier is allowed. So a quote of
kind `marketplace_retail` raises `QuoteRefused` before it is stored.
https://www.ebay.co.uk/help/policies/selling-policies/drop-shipping-policy?id=4176

**Quotes live outside the repo**, in `~/.phoneloop/suppliers/` (override:
PHONELOOP_SUPPLIERS_DIR). They are commercial terms, and this repo pushes
web/ to GitHub every night. `suppliers_dir()` refuses a path inside the
checkout.

Entry is by hand. A supplier API (CJdropshipping documents one) can later
write the same `Quote` record through `phoneloop.harness`, so tests replay
it with no network.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime
from pathlib import Path

from phoneloop import board, catalog_ebay, fees
from phoneloop.knobs import Knobs

REPO_ROOT = Path(__file__).resolve().parent.parent

#: What kind of business sells the stock. Only `marketplace_retail` (an
#: AliExpress, Amazon or Temu listing shipped straight to your buyer) is
#: refused. eBay's policy page does not define "wholesale supplier", and
#: whether a dropship agent counts is not checked: keep a written agreement
#: that names you as the seller.
SUPPLIER_KINDS: tuple[str, ...] = (
    "manufacturer", "wholesale", "dropship_agent", "marketplace_retail")
REFUSED_FOR_EBAY: frozenset[str] = frozenset({"marketplace_retail"})
EBAY_DROPSHIP_POLICY_URL = (
    "https://www.ebay.co.uk/help/policies/selling-policies/"
    "drop-shipping-policy?id=4176")

CLEARS, FAILS, UNKNOWN = "clears", "fails", "unknown"

QUOTES_FILE = "quotes.jsonl"
RETURNS_FILE = "returns.json"
JOIN_FILE = "board-join.json"


class QuoteRefused(ValueError):
    """A quote that breaks a selling policy. It is never stored."""


# --- money --------------------------------------------------------------------

_MONEY = re.compile(r"^(-?)£?(\d+)(?:\.(\d{1,2}))?$")


def parse_pence(text: str | int) -> int:
    """'12.34', '£12.34', '12.3' or '12' as integer pence. No float anywhere:
    '12.345' is refused, never rounded. An int passes through as pence."""
    if isinstance(text, bool):
        raise ValueError(f"not an amount of money: {text!r}")
    if isinstance(text, int):
        return text
    m = _MONEY.match(str(text).strip().replace(",", ""))
    if not m:
        raise ValueError(f"not an amount of money in pounds and pence: {text!r}")
    sign, pounds, frac = m.groups()
    pence = int(pounds) * 100 + int((frac or "0").ljust(2, "0"))
    return -pence if sign else pence


def fmt_pence(pence: int | None) -> str:
    """GBP for a reader. None prints "?", never "£0.00"."""
    if pence is None:
        return "?"
    sign = "-" if pence < 0 else ""
    p = abs(pence)
    return f"{sign}£{p // 100}.{p % 100:02d}"


def _int_field(name: str, value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name} must be {minimum} or more, got {value}")
    return value


# --- the quote record -----------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    """One supplier's price for one board product, entered by hand.

    `product_key` is the board's product key. A `cluster:<leaf>:<n>` key is
    positional: the clustering can number a product differently on another
    night. Store the board `label` with the quote, and the join flags a
    label that has changed.
    """

    product_key: str
    supplier: str
    supplier_kind: str
    unit_cost_pence: int
    postage_pence: int               # supplier's charge to post one unit to your buyer
    ship_from: str                   # ISO 3166 two-letter country code, e.g. GB, CN
    latest_delivery_days: int        # latest day after the order the supplier says it arrives
    returns_terms: str
    quote_date: date
    source_url: str
    leaf_category_id: str | None = None
    label: str | None = None
    notes: str = ""
    quote_id: str | None = None      # assigned by QuoteBook.add

    def __post_init__(self) -> None:
        for name in ("product_key", "supplier", "returns_terms"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        if self.supplier_kind not in SUPPLIER_KINDS:
            raise ValueError(f"supplier_kind must be one of {SUPPLIER_KINDS}, "
                             f"got {self.supplier_kind!r}")
        if self.supplier_kind in REFUSED_FOR_EBAY:
            raise QuoteRefused(
                f"supplier kind {self.supplier_kind!r} is refused for eBay: a "
                "retailer or marketplace that ships directly to your buyer is "
                f"not allowed ({EBAY_DROPSHIP_POLICY_URL}). Use a wholesale "
                "supplier, a manufacturer or a dropship agent with a written "
                "agreement that names you as the seller.")
        _int_field("unit_cost_pence", self.unit_cost_pence)
        _int_field("postage_pence", self.postage_pence)
        _int_field("latest_delivery_days", self.latest_delivery_days)
        if not re.fullmatch(r"[A-Z]{2}", self.ship_from or ""):
            raise ValueError("ship_from must be a two-letter country code in "
                             f"capitals (GB, CN), got {self.ship_from!r}")
        if not isinstance(self.quote_date, date) or isinstance(self.quote_date, datetime):
            raise ValueError(f"quote_date must be a date, got {self.quote_date!r}")
        if not re.match(r"^https?://\S+$", self.source_url or ""):
            raise ValueError("source_url must be the http(s) address the "
                             f"quote came from, got {self.source_url!r}")

    @property
    def landed_pence(self) -> int:
        """What one unit costs you delivered to your buyer."""
        return self.unit_cost_pence + self.postage_pence

    def to_json(self) -> dict:
        d = asdict(self)
        d["quote_date"] = self.quote_date.isoformat()
        return d

    @classmethod
    def from_json(cls, d: Mapping) -> "Quote":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["quote_date"] = date.fromisoformat(str(data["quote_date"]))
        return cls(**data)


def suppliers_dir(env: Mapping[str, str] | None = None) -> Path:
    """`~/.phoneloop/suppliers`, or PHONELOOP_SUPPLIERS_DIR. Refuses any path
    inside this checkout, so a quote cannot be committed by accident."""
    e = os.environ if env is None else env
    override = e.get("PHONELOOP_SUPPLIERS_DIR")
    path = Path(override) if override else Path.home() / ".phoneloop" / "suppliers"
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError(f"{resolved} is inside the repo checkout {REPO_ROOT}. "
                         "Supplier quotes stay outside the repo.")
    return path


class QuoteBook:
    """Append-only JSON lines, one quote per line. A quote is never edited:
    a new price is a new quote with its own date."""

    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.path = self.dir / QUOTES_FILE

    def list(self) -> list[Quote]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Quote.from_json(json.loads(line)))
        return out

    def add(self, quote: Quote) -> Quote:
        """Store `quote` with the next id (q1, q2, ...). Returns the stored quote."""
        existing = self.list()
        stored = replace(quote, quote_id=f"q{len(existing) + 1}")
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(stored.to_json(), sort_keys=True) + "\n")
        return stored

    def get(self, quote_id: str) -> Quote | None:
        return next((q for q in self.list() if q.quote_id == quote_id), None)


# --- the returns allowance --------------------------------------------------------

@dataclass(frozen=True)
class ReturnsAllowance:
    """What returns cost per unit SOLD, before any return happens.

    Under the Consumer Contracts Regulations 2013 the buyer can cancel up to
    14 days after delivery, and you refund within 14 days of getting the
    goods back, outbound postage included. So a returned unit loses at least
    the return postage and the postage you paid the supplier, and a returned
    unit you cannot resell loses its unit cost too.

        per return = return_postage + quote postage + unsellable x unit cost
        allowance  = return_rate x per return          (per unit sold)

    eBay's fee credit on a refund is not modelled, so the allowance leans
    high on purpose.
    """

    return_rate_bp: int          # share of units sold that come back
    return_postage_pence: int    # what you pay to get one unit back
    unsellable_bp: int           # share of returned units you cannot resell

    def __post_init__(self) -> None:
        for name in ("return_rate_bp", "unsellable_bp"):
            v = _int_field(name, getattr(self, name))
            if v > 10000:
                raise ValueError(f"{name} is basis points, 0 to 10000, got {v}")
        _int_field("return_postage_pence", self.return_postage_pence)


#: CHOSEN, not measured. No return has been observed yet. Replace these with
#: the trial log's realised figures (docs/dropship-process.md, stage 6).
CHOSEN_ALLOWANCE = ReturnsAllowance(
    return_rate_bp=1000,          # 10% of units sold come back
    return_postage_pence=350,     # GBP 3.50 to get one back
    unsellable_bp=5000,           # half of those cannot be resold
)


def load_allowance(directory: Path | str) -> tuple[ReturnsAllowance, str]:
    """The allowance in `returns.json`, or CHOSEN_ALLOWANCE when there is no
    file. The second value says which, so every printout can name it."""
    path = Path(directory) / RETURNS_FILE
    if not path.exists():
        return CHOSEN_ALLOWANCE, "chosen default (no returns.json yet)"
    data = json.loads(path.read_text(encoding="utf-8"))
    return (ReturnsAllowance(**{f.name: data[f.name]
                                for f in fields(ReturnsAllowance)}),
            str(path))


def allowance_pence(quote: Quote, allowance: ReturnsAllowance) -> int:
    """The returns cost to set against one unit sold, integer pence."""
    unsellable = fees.bp(quote.unit_cost_pence, allowance.unsellable_bp)
    per_return = allowance.return_postage_pence + quote.postage_pence + unsellable
    return fees.bp(per_return, allowance.return_rate_bp)


# --- the verdict --------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    basis: str                      # "floor" | "median"
    state: str                      # clears | fails | unknown
    max_cost_pence: int | None
    denominator: int                # listings the price under max_cost rests on
    landed_pence: int
    allowance_pence: int
    headroom_pence: int | None      # max_cost - landed - allowance; None when unknown
    reason: str = ""


def verdict(quote: Quote, max_cost_pence: int | None, denominator: int, *,
            basis: str, allowance: ReturnsAllowance) -> Verdict:
    """Does `quote` clear `max_cost_pence` after the returns allowance?

    `clears` when landed + allowance <= max_cost (max_cost is the highest
    cost that still clears, so equal clears). `fails` otherwise, including
    every negative max_cost. `unknown` when max_cost is None, whatever the
    quote says: no price was observed, so there is no ceiling to test.
    """
    landed = quote.landed_pence
    allow = allowance_pence(quote, allowance)
    if max_cost_pence is None:
        return Verdict(basis=basis, state=UNKNOWN, max_cost_pence=None,
                       denominator=denominator, landed_pence=landed,
                       allowance_pence=allow, headroom_pence=None,
                       reason=f"no max_cost at the {basis} price "
                              f"(n={denominator} listings)")
    headroom = max_cost_pence - landed - allow
    return Verdict(basis=basis, state=CLEARS if headroom >= 0 else FAILS,
                   max_cost_pence=max_cost_pence, denominator=denominator,
                   landed_pence=landed, allowance_pence=allow,
                   headroom_pence=headroom)


# --- the board join -----------------------------------------------------------------

@dataclass(frozen=True)
class QuoteLine:
    quote: Quote
    found: bool                     # the product key is on the newest snapshot
    label: str | None               # the board label tonight
    floor: Verdict
    median: Verdict
    notes: tuple[str, ...] = ()
    figures: dict = field(default_factory=dict)   # predictions the trial log freezes


def _product_rows(store, knobs: Knobs, difference, leaves: Iterable[str] | None):
    """Every product row in `leaves` (None = every leaf on the newest date),
    by key. Uses board.rank_products, so a quote is judged on exactly the
    figures the board prints."""
    if leaves is None:
        dates = store.snapshot_dates("ebay")
        rows = store.rows_on(dates[-1]) if dates else []
        leaves = {r.get("leaf_category_id") for r in rows
                  if r.get("leaf_category_id")}
    leaves = sorted(set(leaves))
    if not leaves:
        return {}
    ranked = board.rank_products(store, knobs, leaves, difference=difference,
                                 limit=10 ** 9)
    return {r.key: r for r in ranked}


def _knob(row, name: str):
    return next((k for k in row.knobs if k.knob == name), None)


def join_quotes(store, knobs: Knobs, difference, quotes: Sequence[Quote],
                allowance: ReturnsAllowance) -> list[QuoteLine]:
    """Each quote against its product's max_cost on the newest snapshot."""
    if not quotes:
        return []
    need_all = any(not q.leaf_category_id for q in quotes)
    leaves = None if need_all else {q.leaf_category_id for q in quotes}
    rows = _product_rows(store, knobs, difference, leaves)
    top_level_of = catalog_ebay.top_level_of()

    lines = []
    for q in quotes:
        row = rows.get(q.product_key)
        notes = []
        figures: dict = {}
        if row is None:
            floor = verdict(q, None, 0, basis="floor", allowance=allowance)
            median = verdict(q, None, 0, basis="median", allowance=allowance)
            notes.append("product key not on the newest snapshot, so no "
                         "max_cost to test against")
        else:
            m = row.metrics
            floor = verdict(q, row.max_cost_at_floor_pence,
                            m.price_floor_denominator, basis="floor",
                            allowance=allowance)
            median = verdict(q, row.max_cost_at_median_pence,
                             m.price_median_denominator, basis="median",
                             allowance=allowance)
            if m.price_median_pence is None:
                notes.append("no business seller's landed price seen for this "
                             "product, so its max_cost is unknown")
            if q.label and row.label != q.label:
                notes.append(f"board label changed since the quote "
                             f"({q.label!r} -> {row.label!r}): a cluster key "
                             "can name a different product on another night")
            st = _knob(row, "sell_through")
            net_at_median = None
            if m.price_median_pence is not None:
                net_at_median = (
                    m.price_median_pence
                    - fees.fees(m.price_median_pence, row.leaf_category_id,
                                knobs, top_level_of=top_level_of)
                    - q.landed_pence - allowance_pence(q, allowance))
            figures = {
                "price_median_pence": m.price_median_pence,
                "price_median_n": m.price_median_denominator,
                "net_per_unit_at_median_pence": net_at_median,
                "str_window_bp": m.str_window_bp,
                "str_window_n": m.str_window_denominator,
                "str_window_days": m.str_window_days,
                "str_comparable": (st is not None and st.passed is not None),
                "str_note": st.note if st is not None else "",
                "eta_median_days": m.eta_median_days,
                "eta_n": m.eta_median_denominator,
            }
        if q.ship_from != "GB" and knobs.import_vat_term_bp == 0:
            notes.append("ships from outside GB and import_vat_term_bp is 0 "
                         "(unverified), so max_cost can read too high")
        if q.latest_delivery_days > knobs.max_eta_days:
            notes.append(f"latest delivery {q.latest_delivery_days} days is "
                         f"over max_eta_days {knobs.max_eta_days} (knob 4)")
        lines.append(QuoteLine(quote=q, found=row is not None,
                               label=row.label if row is not None else None,
                               floor=floor, median=median, notes=tuple(notes),
                               figures=figures))
    return lines


def _fmt_verdict(v: Verdict) -> str:
    if v.state == UNKNOWN:
        return f"{v.basis} UNKNOWN max_cost=? (n={v.denominator})"
    sign = "+" if v.headroom_pence >= 0 else ""
    return (f"{v.basis} {v.state.upper()} max_cost={fmt_pence(v.max_cost_pence)} "
            f"(n={v.denominator}) headroom {sign}{fmt_pence(v.headroom_pence)}")


def render_quote_table(lines: Sequence[QuoteLine], allowance: ReturnsAllowance,
                       allowance_source: str) -> str:
    """The quoted products, each with both verdicts and the n under each
    max_cost. clears, fails and unknown are counted apart, never summed."""
    out = [f"returns allowance: return rate {allowance.return_rate_bp / 100:g}%, "
           f"return postage {fmt_pence(allowance.return_postage_pence)}, "
           f"unsellable {allowance.unsellable_bp / 100:g}% -- {allowance_source}"]
    if not lines:
        out.append("(no supplier quotes on file)")
        return "\n".join(out)
    for ln in lines:
        q = ln.quote
        out.append(f"{q.quote_id}  {q.product_key}  {q.supplier} ({q.supplier_kind}, "
                   f"from {q.ship_from}, quoted {q.quote_date})")
        out.append(f"      {ln.label or q.label or '(no label)'}")
        out.append(f"      landed {fmt_pence(q.landed_pence)} + returns allowance "
                   f"{fmt_pence(ln.floor.allowance_pence)}")
        out.append(f"      {_fmt_verdict(ln.floor)}")
        out.append(f"      {_fmt_verdict(ln.median)}")
        for note in ln.notes:
            out.append(f"      note: {note}")
    counts = {basis: {s: sum(1 for ln in lines if getattr(ln, basis).state == s)
                      for s in (CLEARS, FAILS, UNKNOWN)}
              for basis in ("floor", "median")}
    for basis, c in counts.items():
        out.append(f"at the {basis}: {c[CLEARS]} clear, {c[FAILS]} fail, "
                   f"{c[UNKNOWN]} unknown (of {len(lines)} quotes)")
    return "\n".join(out)


def write_join(directory: Path | str, lines: Sequence[QuoteLine], *,
               date_a: date | None, date_b: date | None,
               now: datetime | None = None) -> Path:
    """Save tonight's join, so `scripts/trial.py` can freeze the predictions
    a test listing is judged against. Overwritten on every run."""
    now = now or datetime.now(UTC)
    payload = {
        "generated_at": now.isoformat(),
        "date_a": date_a.isoformat() if date_a else None,
        "date_b": date_b.isoformat() if date_b else None,
        "quotes": {
            ln.quote.quote_id: {
                "product_key": ln.quote.product_key,
                "found": ln.found,
                "label": ln.label,
                "landed_pence": ln.quote.landed_pence,
                "allowance_pence": ln.floor.allowance_pence,
                "latest_delivery_days": ln.quote.latest_delivery_days,
                "verdict_floor": asdict(ln.floor),
                "verdict_median": asdict(ln.median),
                **ln.figures,
            } for ln in lines},
    }
    path = Path(directory) / JOIN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str),
                    encoding="utf-8")
    return path


def print_quote_board(store, knobs: Knobs, difference, *,
                      env: Mapping[str, str] | None = None) -> str:
    """What `scripts/niche_board.py --with-quotes` prints. Also saves the
    join for the trial log. Kept here so the board script changes by a few
    lines only."""
    directory = suppliers_dir(env)
    allowance, source = load_allowance(directory)
    lines = join_quotes(store, knobs, difference, QuoteBook(directory).list(),
                        allowance)
    text = render_quote_table(lines, allowance, source)
    if lines:
        path = write_join(directory, lines,
                          date_a=getattr(difference, "date_a", None),
                          date_b=getattr(difference, "date_b", None))
        text += f"\nsaved the join for scripts/trial.py: {path}"
    return text
