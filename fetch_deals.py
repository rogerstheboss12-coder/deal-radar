#!/usr/bin/env python3
"""Deal Radar fetcher.

Pulls deals from public deal RSS feeds (no key needed) and, when a key is set
in ~/DealRadar/.env as BESTBUY_API_KEY=..., from the Best Buy Products API.
Keeps a rolling state with first-seen times and price history, then writes
site/deals.json for the static page.
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
STATE = os.path.join(ROOT, "state.json")
# In GitHub Actions the previous run's output is read back from the live site,
# so seen-times and price history carry over without committing state.
STATE_URL = os.environ.get("STATE_URL")
MAX_DEALS = 1000
KEEP_DAYS = 5      # drop deals not seen in the feeds for this long
UA = "Mozilla/5.0 (DealRadar personal feed reader)"

SD = "https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&"
RSS_FEEDS = [
    ("frontpage", SD + "mode=frontpage"),
    ("popular", SD + "mode=popdeals"),
    ("clearance", SD + "q=clearance"),
    ("home-depot", SD + "q=home+depot"),
    ("lowes", SD + "q=lowes"),
    ("walmart", SD + "q=walmart+clearance"),
    ("target", SD + "q=target+clearance"),
    ("amazon", SD + "q=amazon"),
    ("price-mistake", SD + "q=price+mistake"),
]
DN_FEEDS = [
    ("dn-hot", "https://www.dealnews.com/?rss=1&sort=hotness"),
    ("dn-new", "https://www.dealnews.com/?rss=1"),
]

CATEGORIES = [
    ("Tools & Home", r"drill|saw|tool|dewalt|milwaukee|ryobi|makita|ego |kobalt|craftsman|mower|trimmer|blower|grill|patio|faucet|vacuum|dyson|shark|appliance|washer|dryer|fridge|refrigerator|mattress|furniture|lamp|light bulb|thermostat|home depot|lowe"),
    ("Tech", r"laptop|macbook|ipad|iphone|galaxy|pixel|monitor|tv\b|oled|qled|ssd|nvme|gpu|rtx|ryzen|intel|router|headphone|earbud|airpods|speaker|soundbar|camera|kindle|echo|tablet|chromebook|keyboard|mouse|charger|usb|hdmi|smartwatch|apple watch"),
    ("Gaming", r"ps5|playstation|xbox|nintendo|switch|steam|game\b|games\b|controller|gaming"),
    ("Clothing", r"shirt|jacket|shoe|sneaker|boot|jeans|hoodie|sock|nike|adidas|levi|underwear|dress|apparel|watch\b"),
    ("Grocery & Household", r"coffee|snack|protein|detergent|paper towel|toilet paper|diaper|wipes|trash bag|shampoo|toothpaste|vitamin|pack of|count\)|ct\b"),
    ("Toys & Kids", r"lego|toy|kids|baby|stroller|car seat|barbie|hot wheels"),
    ("Outdoors & Auto", r"tent|camping|kayak|bike|bicycle|tire|car |auto|motor oil|cooler|yeti|stanley|flashlight|backpack"),
]

STORE_NAMES = {
    "amazon": "Amazon", "walmart": "Walmart", "target": "Target", "bestbuy": "Best Buy",
    "best-buy": "Best Buy", "home-depot": "Home Depot", "the-home-depot": "Home Depot",
    "homedepot": "Home Depot", "lowes": "Lowe's", "costco": "Costco", "costco-wholesale": "Costco", "woot": "Woot",
    "ebay": "eBay", "newegg": "Newegg", "kohls": "Kohl's", "macys": "Macy's",
}

MONEY = r"\$\s?([\d,]+(?:\.\d{1,2})?)"


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def category(title):
    t = title.lower()
    for name, pat in CATEGORIES:
        if re.search(pat, t):
            return name
    return "Other"


def to_num(s):
    try:
        return round(float(s.replace(",", "")), 2)
    except ValueError:
        return None


def parse_prices(title):
    """Return (price, was) from a deal headline like '$57 (Reg. $100)'."""
    t = title.replace(" ", " ")
    if re.search(r"\bfree\b", t, re.I) and not re.search(MONEY, t):
        return 0.0, None
    was = None
    m = re.search(r"(?:reg\.?|was|list|orig\.?|msrp|retail)\s*:?\s*" + MONEY, t, re.I)
    if m:
        was = to_num(m.group(1))
    prices = [to_num(p) for p in re.findall(MONEY, t)]
    prices = [p for p in prices if p is not None]
    price = None
    for p in prices:
        if p != was:
            price = p
            break
    if price is None and prices:
        price = prices[0]
    if was is not None and price is not None and was <= price:
        was = None
    return price, was


def fetch_rss(tag, url):
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    root = ET.fromstring(get(url))
    deals = []
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        body = item.findtext("content:encoded", default="", namespaces=ns)
        m = re.search(r"/f/(\d+)", link)
        did = "sd" + (m.group(1) if m else str(abs(hash(link))))
        store_slug = re.search(r'data-store-slug="([^"]+)"', body)
        slug = store_slug.group(1) if store_slug else ""
        store = STORE_NAMES.get(slug, slug.replace("-", " ").title() if slug else "")
        if not store:
            for key, name in [("home depot", "Home Depot"), ("lowe", "Lowe's"),
                              ("walmart", "Walmart"), ("target", "Target"),
                              ("best buy", "Best Buy"), ("costco", "Costco"),
                              ("amazon", "Amazon")]:
                if key in title.lower():
                    store = name
                    break
        score = re.search(r"Thumb Score:\s*([+-]?\d+)", body)
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        pub = item.findtext("pubDate")
        try:
            posted = iso(datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(timezone.utc))
        except (TypeError, ValueError):
            posted = None
        price, was = parse_prices(title)
        deals.append({
            "id": did, "src": "Slickdeals", "store": store or "Online",
            "title": title[:180], "price": price, "was": was,
            "url": link.split("?")[0], "cat": category(title),
            "score": int(score.group(1)) if score else None,
            "posted": posted, "tags": [tag], "img": img.group(1) if img else None,
            "clr": "clearance" in title.lower() or tag == "clearance",
        })
    return deals


def fetch_dealnews(tag, url):
    dn = "{https://www.dealnews.com/ns/rss/1.0.htm}"
    root = ET.fromstring(get(url))
    deals = []
    for item in root.iter("item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").split("?")[0]
        m = re.search(r"/(\d+)\.html", link)
        if not title or not m:
            continue
        price_txt = item.findtext(dn + "price")
        price = to_num(price_txt) if price_txt else parse_prices(title)[0]
        pub = item.findtext("pubDate")
        try:
            posted = iso(datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").astimezone(timezone.utc))
        except (TypeError, ValueError):
            posted = None
        retailer = html.unescape(item.findtext(dn + "retailer") or "Online")
        media = item.find("{http://search.yahoo.com/mrss/}content")
        img = media.get("url") if media is not None else None
        if img:
            img = re.sub(r"h=\d+&w=\d+", "h=300&w=300", img)
        deals.append({
            "id": "dn" + m.group(1), "src": "DealNews", "store": retailer,
            "title": title[:180], "price": price, "was": None, "url": link,
            "cat": category(title), "score": None, "posted": posted, "tags": [tag],
            "pick": (item.findtext(dn + "staffPick") or "") == "true", "img": img,
            "clr": "clearance" in title.lower(),
        })
    return deals


def fetch_bestbuy(key):
    fields = ("sku,name,salePrice,regularPrice,percentSavings,url,manufacturer,"
              "categoryPath.name,customerReviewAverage,clearance,priceUpdateDate")
    deals = []
    for page in range(1, 6):
        q = "(onSale=true&active=true&percentSavings>=30)"
        url = (f"https://api.bestbuy.com/v1/products{urllib.parse.quote(q, safe='()=&>')}"
               f"?apiKey={key}&format=json&pageSize=100&page={page}"
               f"&sort=percentSavings.dsc&show={fields}")
        data = json.loads(get(url))
        for p in data.get("products", []):
            cats = [c.get("name") for c in (p.get("categoryPath") or [])]
            deals.append({
                "id": f"bb{p['sku']}", "src": "Best Buy API", "store": "Best Buy",
                "title": (p.get("name") or "")[:180],
                "price": p.get("salePrice"), "was": p.get("regularPrice"),
                "url": p.get("url"), "cat": category(" ".join([p.get("name") or ""] + cats)),
                "score": None, "rating": p.get("customerReviewAverage"),
                "posted": None, "tags": ["bestbuy"], "clr": bool(p.get("clearance")),
            })
        if page >= data.get("totalPages", 0):
            break
        time.sleep(0.4)  # Best Buy allows ~5 requests/second
    return deals


def main():
    t = now()
    known = {}
    if STATE_URL:
        try:
            prev = json.loads(get(STATE_URL + "?t=" + str(int(time.time()))))
            known = {d["id"]: d for d in prev.get("deals", [])}
        except Exception as e:
            print(f"no previous state ({e}); starting fresh", file=sys.stderr)
    elif os.path.exists(STATE):
        known = json.load(open(STATE))["deals"]
    fresh, errors, sources = {}, [], {}

    for tag, url in RSS_FEEDS:
        try:
            for d in fetch_rss(tag, url):
                if d["id"] in fresh:
                    fresh[d["id"]]["tags"] = sorted(set(fresh[d["id"]]["tags"] + d["tags"]))
                else:
                    fresh[d["id"]] = d
            sources[tag] = "ok"
        except Exception as e:  # one bad feed shouldn't stop the rest
            errors.append(f"{tag}: {e}")
            sources[tag] = "error"
        time.sleep(1)

    for tag, url in DN_FEEDS:
        try:
            for d in fetch_dealnews(tag, url):
                fresh.setdefault(d["id"], d)
            sources[tag] = "ok"
        except Exception as e:
            errors.append(f"{tag}: {e}")
            sources[tag] = "error"
        time.sleep(1)

    key = load_env().get("BESTBUY_API_KEY") or os.environ.get("BESTBUY_API_KEY")
    if key:
        try:
            for d in fetch_bestbuy(key):
                fresh[d["id"]] = d
            sources["bestbuy"] = "ok"
        except Exception as e:
            errors.append(f"bestbuy: {e}")
            sources["bestbuy"] = "error"

    stamp = iso(t)
    for did, d in fresh.items():
        old = known.get(did, {})
        hist = old.get("hist", [])
        if d["price"] is not None and (not hist or hist[-1][1] != d["price"]):
            hist = (hist + [[stamp, d["price"]]])[-20:]
        d["hist"] = hist
        d["seen"] = old.get("seen", stamp)
        d["last"] = stamp
        d["low"] = min([h[1] for h in hist], default=d["price"])
        d["pct"] = (round(100 * (1 - d["price"] / d["was"]))
                    if d.get("was") and d["price"] is not None and d["was"] > 0 else None)
        known[did] = d

    cutoff = iso(t - timedelta(days=KEEP_DAYS))
    known = {k: v for k, v in known.items() if v.get("last", "") >= cutoff}
    if not STATE_URL:
        json.dump({"deals": known}, open(STATE, "w"))

    ranked = sorted(known.values(), key=lambda d: (d["last"], d.get("seen", "")), reverse=True)
    ranked = ranked[:MAX_DEALS]
    os.makedirs(SITE, exist_ok=True)
    out = os.path.join(SITE, "deals.json")
    json.dump({"updatedAt": stamp, "count": len(ranked), "sources": sources,
               "errors": errors[:5], "deals": ranked}, open(out, "w"), separators=(",", ":"))
    print(f"{len(ranked)} deals, sources={sources}, bytes={os.path.getsize(out)}")
    if errors:
        print("errors:", errors, file=sys.stderr)


if __name__ == "__main__":
    main()
