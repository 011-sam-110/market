"""Measure the rules parser against the live Vinted feed.

Fixtures cannot fail the parser that was written to satisfy them. This runs it
over whatever the market is listing right now and reports the unresolved tail —
the honest accuracy number, and the size of the job an LLM fallback would take.
"""
import json, urllib.request, http.cookiejar, sys, pathlib

# Vinted titles routinely contain emoji; Windows stdout defaults to cp1252
# and dies on them. Reconfigure before printing anything from the feed.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from phoneloop.parser import parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
ACCESSORY_WORDS = ["case","cover","protector","charger","cable","glass","holder",
                   "strap","airpods","watch","lens","stand","pouch","wallet"]

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.addheaders = [("User-Agent", UA), ("Accept", "application/json"),
                 ("Referer", "https://www.vinted.co.uk/")]
op.open("https://www.vinted.co.uk/", timeout=30).read()
url = ("https://www.vinted.co.uk/api/v2/catalog/items?catalog_ids=3565"
       "&per_page=96&order=newest_first&price_from=80")
items = json.loads(op.open(url, timeout=30).read())["items"]

phones = accessories = unknown = no_storage = disq = 0
misses = []
for it in items:
    s = parse(it["title"])
    if s.is_phone:
        phones += 1
        no_storage += s.storage_gb is None
        disq += bool(s.disqualifiers)
    elif any(w in it["title"].lower() for w in ACCESSORY_WORDS):
        accessories += 1
    else:
        unknown += 1
        misses.append(it["title"])

n = len(items)
print(f"live sample: {n} listings, category 3565, price_from=80")
print(f"  parsed as phones      {phones:3}  ({phones/n:.0%})")
print(f"  rejected accessories  {accessories:3}  ({accessories/n:.0%})")
print(f"  UNRESOLVED            {unknown:3}  ({unknown/n:.0%})   <- the LLM tail")
print(f"\n  of those phones: {no_storage} lack storage ({no_storage/max(phones,1):.0%}), {disq} disqualified")
print("\nUNRESOLVED titles:")
for m in misses:
    print("   ", m[:74])
