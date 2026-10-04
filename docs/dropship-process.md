# Dropship process, eBay UK

The niche board finds demand and prices the highest supplier cost that clears
the margin. This runbook takes one product from a board row to a realised sale
record, in six stages. Each stage has a gate. A product goes to the next stage
only when it passes the gate.

Every figure here has one of three labels:

- **measured**: read from data. The line says where and when.
- **chosen**: a setting that someone picked. Change it when the data says so.
- **published**: from a primary source. The line links it.

## Before stage 1: your decisions

The build does not make these four decisions. Each one sets a figure that the
later stages use.

| Decision | What it sets | Status |
|---|---|---|
| eBay business seller account | eBay requires it to resell. It also opens Terapeak. | open |
| VAT registration | `vat_on_fees_bp` in `~/.phoneloop/knobs.json`: 12000 if not registered (the default), 10000 if registered and reclaiming | open |
| UK stock or overseas stock | `import_vat_term_bp`. It is 0 and unverified, so `max_cost` for overseas stock can read too high until you set it. | open |
| Spend cap for samples and test stock | The trial log totals every penny against it. Cap: ______ | open |

## Where to run the commands

The store is on provenance-1 (`~/.phoneloop/prod`). Run the commands there:

    ssh provenance-1
    cd ~/market
    export PHONELOOP_DATA_DIR=~/.phoneloop/prod
    .venv/bin/python scripts/niche_board.py --products --with-quotes

Run them outside the nightly, which ran from 03:00 to 05:52 on 2026-10-04.
The board step took 1 minute 31 seconds that night (measured, nightly log).
With `--with-quotes` it also ranks the quoted products' leaves.

Quotes, the returns allowance and the trial log stay outside the repo:

| File | What |
|---|---|
| `~/.phoneloop/suppliers/quotes.jsonl` | supplier quotes, one per line, never edited |
| `~/.phoneloop/suppliers/returns.json` | the returns allowance, when you set one |
| `~/.phoneloop/suppliers/board-join.json` | the last `--with-quotes` run, which the trial log reads |
| `~/.phoneloop/trials/trials.jsonl` | sample orders and test listings |

The code refuses a path inside the checkout. `PHONELOOP_SUPPLIERS_DIR` and
`PHONELOOP_TRIALS_DIR` move them.

## Stage 1: demand

    python scripts/niche_board.py --products

**Gate.** A product row passes when all of these are true:

1. `max_cost` at the floor price is above £0.
2. No knob reads FAIL.
3. It is not in an excluded category (stage 3, last item).

From the rows that pass, take the 3 with the highest knob coverage (chosen).
A knob that reads `?` is unknown, not a pass. Write down which knobs were `?`,
because the trial measures them.

**Figures.**

- Margin target: 25% of the sale price or £6, whichever is more (chosen:
  `min_margin_pct_bp` 2500, `min_margin_fixed_pence` 600).
- eBay fees: the business final value fee by category, the 0.35% regulatory
  fee and the per-order fee, all ex-VAT (published, eBay help id=4809 as quoted
  in the design spec section 7).
- Sell-through threshold: 50% (chosen, `str_threshold_bp` 5000), judged only
  on a window of 24.0 days or more (chosen: 30 days x 0.8).
- On the 4 Oct board, 8 of 20 niche rows had a `max_cost` at the floor, from
  -£5.50 to £9.41. All 20 product rows had one, from £2.29 to £30.63. All of
  these are before returns (measured, `web/reports/board-2026-10-04.html`).
- Sell-through read `?` on every row of the 4 Oct board, because the board
  compared two nights 1 day apart. The fix (branch `fix/tier3-baseline`)
  compares against a snapshot 25 or more days older. On 2026-10-04 that pair
  was 2026-09-08 to 2026-10-04, with 4,524 listings seen on both dates
  (measured, prod store, read-only).
- Only 65.6% of listings came back after one night (measured, 2026-08-28 to
  2026-08-29), so a long window sees fewer listings. The board prints that
  count.

## Stage 2: supplier quote

Get 2 quotes for each product (chosen) from a manufacturer, a wholesaler or a
dropship agent. Enter each one:

    python scripts/supplier_quote.py add --product-key "cluster:9355:12" \
        --leaf 9355 --label "<the board label>" --supplier "<name>" \
        --kind wholesale --unit-cost 6.40 --postage 2.10 --ship-from GB \
        --latest-delivery-days 4 --returns-terms "<their terms>" \
        --source-url https://...
    python scripts/supplier_quote.py list
    python scripts/niche_board.py --products --with-quotes

**Gate.** A quote passes when all of these are true:

1. It reads CLEARS at the floor price, after the returns allowance.
2. Its kind is not `marketplace_retail`. The code refuses that kind before it
   stores the quote.
3. Its latest delivery day is at or under `max_eta_days`, 7 (chosen, knob 4).
   The board prints a note when it is over.
4. It ships from GB, or you have set `import_vat_term_bp`. The board prints a
   note when it ships from abroad and the term is still 0.

Each quote reads `clears`, `fails` or `unknown` at the floor price and at the
median price. Unknown means the board has no `max_cost` for the product (no
business price seen, or the key is not on the newest snapshot). Unknown is
never read as a zero ceiling. The board prints the n of listings under each
`max_cost`.

A cluster key (`cluster:<leaf>:<n>`) can name a different product on another
night. Give `--label`, and the board flags a label that has changed.

**Figures.**

- Unit cost, postage, delivery days and returns terms: measured, from the
  supplier, with the source URL stored on the quote.
- Returns allowance: chosen, not measured. Return rate 10%, return postage
  £3.50, unsellable share 50%. Per unit sold, the check deducts:

      per return = return postage + the quote's postage + unsellable share x unit cost
      allowance  = return rate x per return

  eBay's fee credit on a refund is not modelled, so the allowance leans high.
  To change it, write `~/.phoneloop/suppliers/returns.json`:

      {"return_rate_bp": 1000, "return_postage_pence": 350, "unsellable_bp": 5000}

- The refusal comes from eBay's policy: "Listing an item on eBay and then
  purchasing the item from another retailer or marketplace that ships directly
  to your customer is not allowed on eBay." Dropshipping from a wholesale
  supplier is allowed (published, [eBay drop shipping policy][ebay-dropship]).

## Stage 3: policy

Answer each line yes, with its evidence, before you order a sample.

1. **eBay drop shipping.** The supplier is a wholesaler, a manufacturer or an
   agent, not a retailer or marketplace that ships to your buyer. You stay
   responsible for delivery and for the buyer. eBay's page does not define
   "wholesale supplier", and whether an agent counts is not checked, so keep a
   written agreement that names you as the seller
   ([eBay drop shipping policy][ebay-dropship], published).
2. **eBay seller standards.** You can meet the delivery time you promise.
   eBay counts an item late when the delivery scan is after the latest
   estimated date and no carrier scan falls inside your dispatch time. Below
   Standard starts above a 2% defect rate, or above 2 cases (or 0.3%) closed
   without seller resolution. It brings higher fees and payment holds
   ([eBay seller standards][ebay-standards], published).
3. **Returns and refunds.** The buyer can cancel up to 14 days after delivery
   ([GOV.UK distance selling][gov-distance], published). You refund within 14
   days of getting the goods back or proof of return, outbound postage
   included. You pay return postage unless you told the buyer before the sale
   ([Consumer Contracts Regulations 2013, regulations 34 and 35][ccr], published).
   Faulty goods add a 30-day right to reject
   ([Consumer Rights Act 2015, section 22][cra], published). The supplier's
   returns terms do not change any of this.
4. **VAT.** For consignments of £135 or less from outside the UK, an online
   marketplace accounts for the VAT at the point of sale
   ([GOV.UK marketplaces][gov-vat-market], published). On a direct sale, the
   seller does ([GOV.UK direct sales][gov-vat-direct], published). When the
   supplier leg and the sale both go through marketplaces, VAT can apply
   twice. A seller who is not VAT registered cannot reclaim it.
5. **Product safety.** In Great Britain, a UK-based importer counts as a
   producer. It must sell only safe products and show a name and address on
   the product or its packaging
   ([General Product Safety Regulations 2005][gpsr], published).
6. **Category.** The product is not in the design spec section 9 exclusions:
   clothing and shoes, branded electronics, fragile goods, consumables and
   cosmetics, or anything with safety compliance (car seats, mains electrical
   devices).
7. **Listing claims.** UK law bans false limited-time claims and fake consumer
   reviews in all circumstances
   ([DMCC Act 2024, Schedule 20, paragraphs 7 and 13][dmcc], published). The
   listing makes no review count, warranty or "airline approved" claim that
   you cannot back.
8. **Amazon, only if you list there.** You are the seller of record and the
   only name on packing slips, invoices and packaging. You need a supplier
   agreement, and you process returns
   ([Amazon drop shipping policy PDF][amazon], published).

## Stage 4: sample

Order one unit of the best clearing quote to your own address. Log the order,
then log the arrival:

    python scripts/trial.py sample --id s1 --quote q1 --ordered 2026-10-06 \
        --promised-days 7 --cost 8.50
    python scripts/trial.py sample --id s1 --arrived 2026-10-11 \
        --condition as_described --slip mine

**Gate.** The sample passes when all of these are true. `trial.py` and
`trial_report.py` print the gate.

1. It arrived within the delivery time you would promise a buyer.
2. It arrived as described.
3. The slip in the parcel shows your name, or no name.

**Figures.**

- Delivery days, condition and the name on the slip: measured, n=1 per sample.
- Promised days: chosen by you. The knob 4 default is 7.
- Cost: measured. It counts against the spend cap.

## Stage 5: test listing

Draft the listing from your own photos and the supplier's spec sheet. List one
unit. Run `scripts/niche_board.py --with-quotes` first, because the first
`listing` call freezes the board's predictions for the quote:

    python scripts/trial.py listing --id l1 --quote q1 --listed 2026-10-14 --price 24.99

Log every change after that. The newest value of each field wins:

    python scripts/trial.py listing --id l1 --as-of 2026-10-21 --units-sold 2 \
        --first-sale 2026-10-16 --returns 0 --cases 0 --net 7.40

`--net` is the realised net to date: all money in minus all money out for the
listing, in pounds and pence. It is stored as integer pence.

**Gate.** The listing is live, every claim in it is backed (stage 3, item 7),
every event is in the log, and the total spend is under the cap.

**Figures.**

- Price: chosen by you.
- Units sold, first sale date, returns, cases and realised net: measured.

## Stage 6: calibrate

    python scripts/trial_report.py

The report prints each sample with its gate, then each listing's predicted
figure against its realised figure, each with its n:

| Figure | Predicted (frozen at listing) | Realised |
|---|---|---|
| price | board median landed price, n = listings | your price, n = 1 |
| net per unit | median price - fees - landed cost - returns allowance | realised net / units sold, n = units |
| units sold | board sell-through over its window, only when the knob gave a verdict | units sold in the days listed |
| delivery days | the quote's latest delivery day, n = 1 | the slowest sample, n = samples |
| return rate | the allowance's return rate, chosen, n = 0 | returns / units sold, n = units |
| days to first sale | none | measured, n = 1 |
| cases | none | measured, n = units |

The last line totals the money: sample spend, listing net, and the sum.

**Gate.** Decide per product (chosen rules, change them as n grows):

1. Below 10 units sold, keep the listing up and keep logging. The n is too
   small to read.
2. At 10 units or more, scale the product when realised net per unit is above
   £0. Stop it when it is £0 or less.
3. When the realised return rate has an n of 10 or more, put it in
   `returns.json` in place of the chosen 10%.

A figure with no observation prints `?` with n=0. It is never read as zero.

## Sources

- [eBay UK drop shipping policy][ebay-dropship]
- [eBay UK seller standards policy][ebay-standards]
- [Amazon drop shipping policy (PDF on Amazon's CDN)][amazon]
- [GOV.UK: VAT on overseas goods sold through online marketplaces][gov-vat-market]
- [GOV.UK: VAT on overseas goods sold directly to UK customers][gov-vat-direct]
- [Consumer Contracts Regulations 2013, regulation 34][ccr]
- [GOV.UK: online and distance selling for businesses][gov-distance]
- [Consumer Rights Act 2015, section 22][cra]
- [GOV.UK: General Product Safety Regulations 2005, Great Britain][gpsr]
- [DMCC Act 2024, Schedule 20][dmcc]
- [CJdropshipping API introduction][cj]: a candidate for a later quote
  adapter through `phoneloop/harness.py`. Not part of this build.

[ebay-dropship]: https://www.ebay.co.uk/help/policies/selling-policies/drop-shipping-policy?id=4176
[ebay-standards]: https://www.ebay.co.uk/help/policies/selling-policies/seller-standards-policy?id=4347
[amazon]: https://m.media-amazon.com/images/G/41/rainier/help/legal/Drop_Shipping_Policy_Update.pdf
[gov-vat-market]: https://www.gov.uk/guidance/vat-and-overseas-goods-sold-to-customers-in-the-uk-using-online-marketplaces
[gov-vat-direct]: https://www.gov.uk/guidance/vat-and-overseas-goods-sold-directly-to-customers-in-the-uk
[ccr]: https://www.legislation.gov.uk/uksi/2013/3134/regulation/34
[gov-distance]: https://www.gov.uk/online-and-distance-selling-for-businesses
[cra]: https://www.legislation.gov.uk/ukpga/2015/15/section/22
[gpsr]: https://www.gov.uk/government/publications/general-product-safety-regulations-2005/general-product-safety-regulations-2005-great-britain
[dmcc]: https://www.legislation.gov.uk/ukpga/2024/13/schedule/20
[cj]: https://developers.cjdropshipping.com/en/api/introduction.html
