# Recorded category trees

`ebay_categories.json` in the repo root is the LIVE tree. It is what
`scripts/bulk_ebay.py --leaf-sweep` shards on, so changing it changes the
swept population and makes the next day-over-day difference non-comparable.
`phoneloop/footprint.py` reports that on the board rather than presenting it
as overnight market movement.

Every tree a live crawl produced is kept here, dated, so a leaf set can be
compared or rolled back without re-crawling. A crawl costs one live fetch per
node.

| file | leaves | crawl |
|---|---|---|
| `ebay-depth1-all-2026-08-31.json` | 215 | depth 1, no per-node cap. 220 pages fetched, 0 blocked, 5 nodes carried no numeric id and were dropped rather than guessed. |

The live tree at the time of writing holds 43 leaves. That number was never a
measurement of the market: it is `MAX_LEAVES_PER_DEPARTMENT = 5` times nine
departments, a cap on the first crawl. Removing the cap at the same depth
found 215, and every one of the original 43 survives -- the change is purely
additive.

The same crawl counted 2,222 children one level below the leaves it recorded.
Walking them is `--depth 2` and costs 2,222 more fetches, about six hours.

## Installing one

    cp category_trees/<file> ebay_categories.json

Then read the first difference after it as non-comparable, which the board
says by itself. Do it on a night that changes nothing else, or two step
changes land under one banner and neither is readable afterwards.
