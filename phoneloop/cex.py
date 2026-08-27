"""CeX price parsing: an outside anchor on what a handset is worth.

Why this exists. The pricebook derives "market value" from Vinted asking prices,
which is circular -- if the whole category is asking too much, every listing in
it reads as fair, and the scorer has no way to know. CeX is an independent
reference with a property no Vinted-derived median has: the prices are
*committed*. CeX will actually pay the cash price, to anyone, today.

That gives two hard numbers per handset:

    cash    what CeX pays you. A floor. A Vinted listing priced under this is
            either a genuine steal or a scam, and nothing in between -- which is
            exactly the discrimination the scoring bands exist to make.
    sell    what CeX charges for the same handset. A retail ceiling. A realistic
            resale on eBay or Vinted lands somewhere below it, never above.

Worth being blunt about what the first measurement showed: an iPhone 12 128GB
pays £110 cash at grade B while typical Vinted asks sit around £130. Flipping
Vinted -> CeX is usually a loss. CeX is not the exit; it is the yardstick.

This module is pure -- it parses text and knows nothing about browsers. Fetching
lives in scripts/refresh_cex.py, because CeX sits behind a Cloudflare rule that
refuses every scripted client (plain requests and Chrome-TLS impersonation both
get a 403) and only a real browser gets through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "Apple iPhone 12 128GB Black, Unlocked B"
#  make  model      storage colour   network  grade
_BOX_RE = re.compile(
    r"^(?P<make>[A-Za-z]+)\s+"
    r"(?P<model>.+?)\s+"
    r"(?P<storage>\d+)\s*(?P<unit>GB|TB)\s+"
    r"(?P<colour>[^,]+),\s*"
    r"(?P<network>.+?)\s+"
    r"(?P<grade>[ABC])$",
    re.IGNORECASE,
)

_MONEY_RE = re.compile(r"£\s*([\d,]+(?:\.\d{2})?)")


@dataclass(frozen=True)
class CexBox:
    """One CeX SKU. Money is integer pence, as everywhere else in this project."""

    box_name: str
    cash_pence: int
    voucher_pence: int
    make: str = ""
    model: str = ""
    storage_gb: int | None = None
    colour: str = ""
    network: str = ""
    grade: str = ""

    @property
    def unlocked(self) -> bool:
        """Carrier-locked stock trades lower and is not comparable.

        A "3 B" box is a Three-locked handset, and pricing an unlocked Vinted
        listing against it would understate the phone by roughly a grade.
        """
        return self.network.strip().lower() == "unlocked"


def to_pence(text: str) -> int | None:
    """Parse a money string to integer pence. None if there is no number in it."""
    match = _MONEY_RE.search(text)
    if not match:
        return None
    return round(float(match.group(1).replace(",", "")) * 100)


def parse_box_name(name: str) -> dict:
    """Split a CeX box name into its parts.

    Returns an empty dict when the name does not match, rather than guessing.
    CeX sells a great deal that is not a phone, and a half-parsed box is worse
    than a skipped one.
    """
    match = _BOX_RE.match(name.strip())
    if not match:
        return {}
    gb = int(match.group("storage"))
    if match.group("unit").upper() == "TB":
        gb *= 1024
    return {
        "make": match.group("make").lower(),
        "model": " ".join(match.group("model").lower().split()),
        "storage_gb": gb,
        "colour": match.group("colour").strip(),
        "network": match.group("network").strip(),
        "grade": match.group("grade").upper(),
    }


def parse_sell_page(text: str) -> list[CexBox]:
    """Pull every priced box out of a CeX sell-search page's visible text.

    Deliberately line-based rather than one large regex. The page renders each
    result as a box name followed by CASH and CeXVOUCHER blocks, and a line scan
    survives the whitespace and markup churn that a big pattern would not.
    """
    lines = [line.strip() for line in text.splitlines()]
    boxes: list[CexBox] = []

    for i, line in enumerate(lines):
        if line.upper() != "CASH":
            continue

        # The box name is the nearest non-empty line above CASH. The line above
        # that is the category heading, which we do not want.
        name = ""
        for back in range(i - 1, max(-1, i - 6), -1):
            if lines[back]:
                name = lines[back]
                break
        if not name:
            continue

        cash = voucher = None
        for fwd in range(i + 1, min(len(lines), i + 10)):
            token = lines[fwd]
            if not token:
                continue
            if cash is None:
                cash = to_pence(token)
                continue
            if token.upper().endswith("VOUCHER"):
                continue
            voucher = to_pence(token)
            break

        if cash is None:
            continue

        parts = parse_box_name(name)
        boxes.append(
            CexBox(
                box_name=name,
                cash_pence=cash,
                voucher_pence=voucher or 0,
                make=parts.get("make", ""),
                model=parts.get("model", ""),
                storage_gb=parts.get("storage_gb"),
                colour=parts.get("colour", ""),
                network=parts.get("network", ""),
                grade=parts.get("grade", ""),
            )
        )
    return boxes


def best_cash(boxes: list[CexBox], grade: str = "B") -> int | None:
    """The cash price for unlocked stock at a given grade.

    Grade B is the default because it is CeX's "good condition" tier and maps
    most closely to how a used Vinted handset is usually described. Returns the
    lowest matching price: where several colours differ, the conservative one is
    the one that will not overstate a margin.
    """
    matching = [
        b.cash_pence for b in boxes
        if b.unlocked and b.grade == grade.upper() and b.cash_pence > 0
    ]
    return min(matching) if matching else None
