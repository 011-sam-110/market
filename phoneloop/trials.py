"""The trial log: sample orders and test listings, set against what the
board predicted (docs/dropship-process.md, stages 4 to 6).

The board is a calibration instrument. Its figures earn trust only when a
real sale is set beside them. This module records that sale.

A **sample order** records the order and arrival dates, the delivery days
those give, the delivery time you would promise a buyer, the condition the
unit arrived in, whose name was on the slip, and what it cost.

A **test listing** records the price, units sold, the first sale date, returns,
cases and the realised net in pence. When it is first logged with a quote
id, it freezes the board's predictions for that quote from the last
`scripts/niche_board.py --with-quotes` run, so the report sets realised
figures against what was predicted BEFORE the listing went up, not against
whatever the board says later.

**Append-only.** Every `record_*` call adds one JSON line. The latest value
of each field wins, so an update is a new line, and the history stays.

**Unknown is never zero.** A figure with no observation is None, and the
report prints "?" with n=0.

The log lives in `~/.phoneloop/trials/` (override: PHONELOOP_TRIALS_DIR),
outside the repo, for the same reason as supplier quotes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, date, datetime
from pathlib import Path

from phoneloop.supplier import (CHOSEN_ALLOWANCE, JOIN_FILE, REPO_ROOT,
                                ReturnsAllowance, fmt_pence)

TRIALS_FILE = "trials.jsonl"

#: How the sample arrived.
CONDITIONS: tuple[str, ...] = ("as_described", "damaged", "not_as_described",
                               "missing")
#: Whose name was on the packing slip or invoice in the parcel. Amazon's
#: policy wants yours only. eBay's page says you stay responsible for the
#: delivery and the buyer, and does not name the slip. "supplier" means the
#: buyer sees who you buy from.
SLIP_NAMES: tuple[str, ...] = ("mine", "none", "supplier", "other")


def trials_dir(env: Mapping[str, str] | None = None) -> Path:
    """`~/.phoneloop/trials`, or PHONELOOP_TRIALS_DIR. Refuses a path inside
    the checkout."""
    e = os.environ if env is None else env
    override = e.get("PHONELOOP_TRIALS_DIR")
    path = Path(override) if override else Path.home() / ".phoneloop" / "trials"
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise ValueError(f"{resolved} is inside the repo checkout {REPO_ROOT}. "
                         "The trial log stays outside the repo.")
    return path


def _check_choice(name: str, value: str | None, choices: Sequence[str]) -> None:
    if value is not None and value not in choices:
        raise ValueError(f"{name} must be one of {tuple(choices)}, got {value!r}")


def _check_count(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a whole number, 0 or more, got {value!r}")


def _check_pence(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be integer pence, got {value!r}")


@dataclass(frozen=True)
class SampleOrder:
    sample_id: str
    quote_id: str | None = None
    product_key: str | None = None
    ordered_on: date | None = None
    arrived_on: date | None = None
    promised_days: int | None = None     # the delivery time you would promise a buyer
    condition: str | None = None
    name_on_slip: str | None = None
    cost_pence: int | None = None         # unit + postage you paid, as charged

    def __post_init__(self) -> None:
        _check_choice("condition", self.condition, CONDITIONS)
        _check_choice("name_on_slip", self.name_on_slip, SLIP_NAMES)
        _check_count("promised_days", self.promised_days)
        _check_pence("cost_pence", self.cost_pence)
        if self.cost_pence is not None and self.cost_pence < 0:
            raise ValueError("cost_pence is what you paid, 0 or more")
        if (self.ordered_on and self.arrived_on
                and self.arrived_on < self.ordered_on):
            raise ValueError("arrived_on is before ordered_on")

    @property
    def delivery_days(self) -> int | None:
        if self.ordered_on is None or self.arrived_on is None:
            return None
        return (self.arrived_on - self.ordered_on).days

    def gate(self) -> tuple[bool | None, list[str]]:
        """Stage 4's gate. None until the sample has arrived and every field
        the gate reads is filled in. False with the reasons, or True."""
        missing = [n for n in ("arrived_on", "promised_days", "condition",
                               "name_on_slip") if getattr(self, n) is None]
        if self.ordered_on is None:
            missing.insert(0, "ordered_on")
        if missing:
            return None, [f"not recorded yet: {', '.join(missing)}"]
        reasons = []
        if self.delivery_days > self.promised_days:
            reasons.append(f"took {self.delivery_days} days, over the "
                           f"{self.promised_days} you would promise")
        if self.condition != "as_described":
            reasons.append(f"arrived {self.condition}")
        if self.name_on_slip not in ("mine", "none"):
            reasons.append(f"slip shows {self.name_on_slip}'s name")
        return (not reasons), reasons


@dataclass(frozen=True)
class TestListing:
    listing_id: str
    quote_id: str | None = None
    product_key: str | None = None
    listed_on: date | None = None
    price_pence: int | None = None
    units_sold: int | None = None
    first_sale_on: date | None = None
    returns: int | None = None
    cases: int | None = None
    realised_net_pence: int | None = None  # everything in minus everything out, to date
    as_of: date | None = None               # the date these counts are true on
    ended_on: date | None = None
    #: The board's predictions for the quote, frozen when the listing was
    #: first logged. Empty when there was no join to read.
    predicted: dict = field(default_factory=dict)

    __test__ = False                        # not a pytest class

    def __post_init__(self) -> None:
        for name in ("units_sold", "returns", "cases"):
            _check_count(name, getattr(self, name))
        _check_pence("price_pence", self.price_pence)
        _check_pence("realised_net_pence", self.realised_net_pence)
        if (self.returns is not None and self.units_sold is not None
                and self.returns > self.units_sold):
            raise ValueError("returns cannot exceed units_sold")

    @property
    def days_to_first_sale(self) -> int | None:
        if self.listed_on is None or self.first_sale_on is None:
            return None
        return (self.first_sale_on - self.listed_on).days

    @property
    def days_listed(self) -> int | None:
        end = self.ended_on or self.as_of
        if self.listed_on is None or end is None:
            return None
        return (end - self.listed_on).days


_DATE_FIELDS = {"ordered_on", "arrived_on", "listed_on", "first_sale_on",
                "as_of", "ended_on"}


def _encode(record) -> dict:
    d = asdict(record)
    return {k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in d.items()}


def _decode(cls, d: Mapping):
    known = {f.name for f in fields(cls)}
    data = {}
    for k, v in d.items():
        if k not in known:
            continue
        data[k] = date.fromisoformat(v) if (k in _DATE_FIELDS and v) else v
    return cls(**data)


class TrialLog:
    def __init__(self, directory: Path | str):
        self.dir = Path(directory)
        self.path = self.dir / TRIALS_FILE

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _append(self, kind: str, payload: dict, now: datetime | None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = {"kind": kind, "recorded_at": (now or datetime.now(UTC)).isoformat(),
                **payload}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")

    def _fold(self, kind: str, cls, id_field: str) -> dict:
        """Latest non-None value per field, per id, in log order."""
        merged: dict[str, dict] = {}
        for line in self._lines():
            if line.get("kind") != kind:
                continue
            rid = line[id_field]
            cur = merged.setdefault(rid, {})
            for k, v in line.items():
                if k in ("kind", "recorded_at"):
                    continue
                if v is None or (k == "predicted" and not v):
                    continue
                cur[k] = v
        return {rid: _decode(cls, d) for rid, d in merged.items()}

    def samples(self) -> dict[str, SampleOrder]:
        return self._fold("sample", SampleOrder, "sample_id")

    def listings(self) -> dict[str, TestListing]:
        return self._fold("listing", TestListing, "listing_id")

    def record_sample(self, sample_id: str, *, now: datetime | None = None,
                      **updates) -> SampleOrder:
        """Add or update one sample order. Returns the merged record. Raises
        before writing when the merged record is invalid."""
        current = self.samples().get(sample_id)
        merged = _encode(current) if current else {"sample_id": sample_id}
        merged.update({k: v for k, v in _encode_values(updates).items()
                       if v is not None})
        record = _decode(SampleOrder, merged)
        self._append("sample", _encode(record), now)
        return record

    def record_listing(self, listing_id: str, *, now: datetime | None = None,
                       predicted: dict | None = None, **updates) -> TestListing:
        """Add or update one test listing. `predicted` is kept only from the
        FIRST call that supplies it: a prediction is never rewritten after
        the listing is up."""
        current = self.listings().get(listing_id)
        merged = _encode(current) if current else {"listing_id": listing_id}
        merged.update({k: v for k, v in _encode_values(updates).items()
                       if v is not None})
        if predicted and not merged.get("predicted"):
            merged["predicted"] = predicted
        record = _decode(TestListing, merged)
        self._append("listing", _encode(record), now)
        return record


def _encode_values(d: Mapping) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in d.items()}


# --- predictions --------------------------------------------------------------------

def predictions_for_quote(suppliers_directory: Path | str, quote_id: str,
                          allowance: ReturnsAllowance | None = None) -> dict:
    """The board's figures for `quote_id` from the last `--with-quotes` run,
    in the shape a TestListing freezes. Empty when there is no join, or the
    quote is not in it."""
    path = Path(suppliers_directory) / JOIN_FILE
    if not path.exists():
        return {}
    join = json.loads(path.read_text(encoding="utf-8"))
    q = join.get("quotes", {}).get(quote_id)
    if not q:
        return {}
    allowance = allowance or CHOSEN_ALLOWANCE
    str_ok = bool(q.get("str_comparable"))
    tier3 = (f"Tier 3 {join['date_a']} -> {join['date_b']}"
             if join.get("date_a") and join.get("date_b")
             else "no Tier 3 difference (one snapshot date)")
    return {
        "source": f"{JOIN_FILE} generated {join.get('generated_at')}, {tier3}",
        "price_pence": {"value": q.get("price_median_pence"),
                        "n": q.get("price_median_n", 0),
                        "basis": "board median landed price"},
        "net_per_unit_pence": {"value": q.get("net_per_unit_at_median_pence"),
                               "n": q.get("price_median_n", 0),
                               "basis": "median price - fees - landed - returns allowance"},
        "units_per_listing": {
            "value": (q.get("str_window_bp") if str_ok else None),
            "n": (q.get("str_window_n", 0) if str_ok else 0),
            "basis": (f"board sell-through over {q.get('str_window_days')} days, "
                      "basis points of a unit per listing" if str_ok else
                      f"withheld: {q.get('str_note') or 'no window'}")},
        "delivery_days": {"value": q.get("latest_delivery_days"), "n": 1,
                          "basis": "the quote's latest delivery day"},
        "return_rate_bp": {"value": allowance.return_rate_bp, "n": 0,
                           "basis": "returns allowance, chosen"},
    }


# --- the report -----------------------------------------------------------------------

@dataclass(frozen=True)
class Figure:
    name: str
    predicted: str
    predicted_n: int
    realised: str
    realised_n: int
    note: str = ""


def _pred(listing: TestListing, key: str):
    p = listing.predicted.get(key) or {}
    return p.get("value"), int(p.get("n") or 0), p.get("basis", "")


def _pct(bp: int | None) -> str:
    return "?" if bp is None else f"{bp / 100:.1f}%"


def figures_for(listing: TestListing, samples: Sequence[SampleOrder]) -> list[Figure]:
    """Predicted against realised for each figure, each with its n."""
    out = []

    v, n, basis = _pred(listing, "price_pence")
    out.append(Figure("price", fmt_pence(v), n, fmt_pence(listing.price_pence),
                      1 if listing.price_pence is not None else 0, basis))

    v, n, basis = _pred(listing, "net_per_unit_pence")
    sold = listing.units_sold or 0
    realised = (None if listing.realised_net_pence is None or sold == 0
                else _div_half_up(listing.realised_net_pence, sold))
    out.append(Figure("net per unit", fmt_pence(v), n, fmt_pence(realised),
                      sold if realised is not None else 0, basis))

    v, n, basis = _pred(listing, "units_per_listing")
    days = listing.days_listed
    out.append(Figure(
        "units sold",
        "?" if v is None else f"{v / 10000:.2f} per listing",
        n,
        "?" if listing.units_sold is None or days is None
        else f"{listing.units_sold} in {days} days",
        1 if listing.units_sold is not None and days is not None else 0,
        basis))

    v, n, basis = _pred(listing, "delivery_days")
    arrived = [s.delivery_days for s in samples
               if s.quote_id == listing.quote_id and s.delivery_days is not None]
    out.append(Figure("delivery days", "?" if v is None else str(v), n,
                      "?" if not arrived else str(max(arrived)), len(arrived),
                      basis + ("; realised = the slowest sample" if arrived else "")))

    v, n, basis = _pred(listing, "return_rate_bp")
    rate = (None if listing.returns is None or sold == 0
            else _div_half_up(listing.returns * 10000, sold))
    out.append(Figure("return rate", _pct(v), n, _pct(rate),
                      sold if rate is not None else 0, basis))

    out.append(Figure("days to first sale", "-", 0,
                      "?" if listing.days_to_first_sale is None
                      else str(listing.days_to_first_sale),
                      1 if listing.days_to_first_sale is not None else 0,
                      "the board makes no prediction"))
    out.append(Figure("cases", "-", 0,
                      "?" if listing.cases is None else str(listing.cases),
                      sold if listing.cases is not None else 0,
                      "the board makes no prediction"))
    return out


def _div_half_up(a: int, b: int) -> int:
    """a / b rounded half away from zero, integers only."""
    q, r = divmod(abs(a), b)
    if 2 * r >= b:
        q += 1
    return q if a >= 0 else -q


def render_report(log: TrialLog) -> str:
    samples = log.samples()
    listings = log.listings()
    out = []
    if samples:
        out.append("=== SAMPLE ORDERS ===")
        for s in samples.values():
            ok, reasons = s.gate()
            state = {True: "PASS", False: "FAIL", None: "?"}[ok]
            out.append(f"{s.sample_id}  quote {s.quote_id or '?'}  "
                       f"ordered {s.ordered_on or '?'}  arrived {s.arrived_on or '?'}  "
                       f"delivery {s.delivery_days if s.delivery_days is not None else '?'}"
                       f"/{s.promised_days if s.promised_days is not None else '?'} days  "
                       f"cost {fmt_pence(s.cost_pence)}  gate [{state}]")
            for r in reasons:
                out.append(f"      {r}")
    if listings:
        out.append("=== TEST LISTINGS: predicted against realised ===")
        for lst in listings.values():
            out.append(f"{lst.listing_id}  quote {lst.quote_id or '?'}  "
                       f"{lst.product_key or ''}  listed {lst.listed_on or '?'}  "
                       f"as of {lst.ended_on or lst.as_of or '?'}")
            source = lst.predicted.get("source") if lst.predicted else None
            out.append(f"      predictions: {source or 'none frozen (no board join for this quote)'}")
            out.append(f"      {'figure':<20}{'predicted':<22}{'realised':<22}")
            for f in figures_for(lst, list(samples.values())):
                out.append(f"      {f.name:<20}"
                           f"{(f.predicted + f' (n={f.predicted_n})'):<22}"
                           f"{(f.realised + f' (n={f.realised_n})'):<22}"
                           + (f"  {f.note}" if f.note else ""))
    if not samples and not listings:
        return "(no sample orders or test listings logged)"

    spent = [s.cost_pence for s in samples.values() if s.cost_pence is not None]
    nets = [lst.realised_net_pence for lst in listings.values()
            if lst.realised_net_pence is not None]
    total = (sum(nets) - sum(spent)) if (spent or nets) else None
    out.append(f"money: samples {fmt_pence(-sum(spent)) if spent else '?'} "
               f"(n={len(spent)}), listings net {fmt_pence(sum(nets)) if nets else '?'} "
               f"(n={len(nets)}), total {fmt_pence(total)}")
    return "\n".join(out)
