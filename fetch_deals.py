#!/usr/bin/env python3
"""Deal Radar fetcher.

Pulls deals from public deal feeds (Slickdeals, DealNews, Ben's Bargains,
camelcamelcamel price drops, Reddit deal communities), flags laptops, premium
brands, freebies and pricing anomalies, keeps first-seen times and price
history, and writes site/deals.json for the static page.
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
MAX_DEALS = 1500
KEEP_DAYS = 5      # drop deals not seen in the feeds for this long
MAX_AGE_DAYS = 14  # searches also return old threads; skip deals posted before this
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) DealRadar/1.0 (personal feed reader)"

SD = "https://slickdeals.net/newsearch.php?searcharea=deals&searchin=first&rss=1&"
# Fetched every run.
CORE = [
    ("sd", "frontpage", SD + "mode=frontpage"),
    ("sd", "popular", SD + "mode=popdeals"),
    ("sd", "freebies", SD + "forumchoice%5B%5D=4"),
    # Hot Deals forum: community posts land here before they reach the frontpage.
    ("sd", "hotdeals", SD + "forumchoice%5B%5D=9"),
    ("dn", "dn-clothing", "https://www.dealnews.com/c202/Clothing-Accessories/?rss=1&sort=time"),
    ("blog", "9to5toys", "https://9to5toys.com/feed/"),
    ("blog", "macrumors", "https://feeds.macrumors.com/MacRumors-Deals"),
    ("blog", "dappered", "https://dappered.com/feed/"),
    ("dn", "dn-hot", "https://www.dealnews.com/?rss=1&sort=hotness"),
    ("dn", "dn-new", "https://www.dealnews.com/?rss=1"),
    ("dn", "dn-computers", "https://www.dealnews.com/c39/Computers/?rss=1&sort=time"),
    ("bens", "bens", "https://bensbargains.com/rss/"),
    ("camel", "camel", "https://camelcamelcamel.com/top_drops/feed"),
    # One combined request: Reddit rate-limits quickly, and polling every few
    # minutes still catches every new post.
    ("reddit", "reddit", "https://www.reddit.com/r/buildapcsales+LaptopDeals+frugalmalefashion+FrugalFemaleFashion+deals/new/.rss?limit=100"),
]
# Slickdeals searches, split into three groups; each run fetches one group,
# so every search refreshes about every 15 minutes without hammering the site.
SEARCHES = [
    "clearance", "home depot", "lowes", "walmart clearance", "target clearance", "amazon",
    "price mistake", "price error", "glitch", "90% off", "95% off", "penny",
    "gaming laptop", "rtx laptop", "macbook pro", "workstation laptop", "rtx 5090", "rtx 5080",
    "patagonia", "arcteryx", "north face", "fjallraven", "canada goose", "outdoor research",
    "black diamond", "cotopaxi", "filson", "yeti", "salomon", "hoka",
    # Creative machines on a budget
    "macbook air", "open box macbook", "refurbished macbook", "mac mini", "mini pc", "imac",
    # Tailoring, shoes and accessories
    "kiton", "isaia", "cesare attolini", "luigi borrelli", "sartorio", "edward green",
    "crockett jones", "alden shoes", "drakes", "charvet",
    # Off-price and luxury retailer sales
    "saks off 5th", "nordstrom rack", "neiman marcus last call", "gilt", "yoox",
    "mr porter sale", "ssense sale", "end clothing",
]

CATEGORIES = [
    ("Laptops & PCs", r"laptop|notebook|macbook|chromebook|desktop pc|gaming pc|mini pc|workstation"),
    ("Tech", r"ipad|iphone|galaxy|pixel|monitor|\btv\b|oled|qled|ssd|nvme|gpu|rtx|radeon|ryzen|intel|router|headphone|earbud|airpods|speaker|soundbar|camera|kindle|echo|tablet|keyboard|mouse|charger|usb|hdmi|smartwatch|apple watch|ram\b|ddr5|psu|motherboard|\bcpu\b"),
    ("Gaming", r"ps5|playstation|xbox|nintendo|switch 2|steam|\bgame\b|games\b|controller|gaming"),
    ("Outdoor & Apparel", r"patagonia|arc'?teryx|north face|fjallraven|fjällräven|canada goose|mammut|outdoor research|black diamond|cotopaxi|filson|barbour|\brab\b|mountain hardwear|smartwool|icebreaker|kuhl|salomon|hoka|on running|jacket|parka|fleece|hoodie|shirt|shoe|sneaker|boot|jeans|sock|nike|adidas|levi|apparel|dress|backpack"),
    ("Tools & Home", r"drill|saw|tool|dewalt|milwaukee|ryobi|makita|ego |kobalt|craftsman|mower|trimmer|blower|grill|patio|faucet|vacuum|dyson|shark|appliance|washer|dryer|fridge|refrigerator|mattress|furniture|lamp|thermostat|home depot|lowe"),
    ("Grocery & Household", r"coffee|snack|protein|detergent|paper towel|toilet paper|diaper|wipes|trash bag|shampoo|toothpaste|vitamin|pack of|count\)|\bct\b"),
    ("Toys & Kids", r"lego|toy|kids|baby|stroller|car seat|barbie|hot wheels"),
    ("Outdoors & Auto", r"tent|camping|kayak|bike|bicycle|tire|\bcar\b|auto|motor oil|cooler|yeti|stanley|flashlight"),
]

LAPTOP = re.compile(r"laptop|notebook|macbook|zephyrus|legion|alienware|razer blade|omen|predator|rog strix|tuf gaming|xps|zenbook|thinkpad|precision|zbook", re.I)
HIGH_COMPUTE = re.compile(r"rtx\s?(40[6-9]0|50[6-9]0)|m[345]\s?(pro|max)|ryzen\s?(9|ai)|core\s?(i9|ultra\s?9)|\bi9\b|(32|64|96|128)\s?gb|workstation|quadro|rtx\s?a\d000|4090|5090|5080|4080", re.I)
BRANDS = [
    ("Patagonia", r"patagonia"), ("Arc'teryx", r"arc'?teryx"), ("The North Face", r"north face"),
    ("Fjällräven", r"fj[aä]llr[aä]ven"), ("Canada Goose", r"canada goose"), ("Mammut", r"mammut"),
    ("Outdoor Research", r"outdoor research"), ("Black Diamond", r"black diamond"),
    ("Cotopaxi", r"cotopaxi"), ("Filson", r"filson"), ("Barbour", r"barbour"), ("Rab", r"\brab\b"),
    ("Mountain Hardwear", r"mountain hardwear"), ("Smartwool", r"smartwool"), ("Icebreaker", r"icebreaker"),
    ("Kuhl", r"\bk[uü]hl\b"), ("Salomon", r"salomon"), ("Hoka", r"\bhoka\b"), ("On", r"\bon (running|cloud)"),
    ("Yeti", r"\byeti\b"), ("Stanley", r"\bstanley\b"), ("Osprey", r"\bosprey\b"), ("Marmot", r"marmot"),
    ("Columbia", r"columbia sportswear|\bcolumbia\b"), ("Merrell", r"merrell"), ("Vuori", r"vuori"),
    ("Lululemon", r"lululemon"), ("Carhartt", r"carhartt"),
]
FREE_ITEM = re.compile(r"^\s*(\[[^\]]*\]\s*)?free\b(?! shipping)|\bfree after (rebate|credit|cashback)|\$0(\.00)?\b|100\s?% off", re.I)
JUNK_FREE = re.compile(r"buy one|bogo|\bwin\b|sweepstakes|giveaway|w/ (any )?purchase|with (any )?purchase|free shipping|trial|sample|\bevent\b|\bends\b|donat|members?\b|reward|educator|teacher|student|first \d+|ages \d|kids|workshop|class\b|\bapp\b|in-store|in store|burger|fries|pizza|coffee|drink|taco|chicken|salad|restaurant|dairy queen|smashburger|\bday\b", re.I)
TAILORING = [
    ("Kiton", r"\bkiton\b"), ("Isaia", r"\bisaia\b"), ("Cesare Attolini", r"attolini"),
    ("Luigi Borrelli", r"borrelli"), ("Sartorio", r"sartorio"), ("Edward Green", r"edward green"),
    ("Crockett & Jones", r"crockett (&|and) jones"), ("Alden", r"\balden\b"),
    ("Drake's", r"\bdrake'?s\b(?! (hotel|cake))"), ("Charvet", r"charvet"),
]
LUX_STORES = re.compile(r"saks|off 5th|nordstrom|neiman|last call|gilt|yoox|mr ?porter|ssense|end\.? clothing|matches", re.I)
CREATIVE = re.compile(r"macbook|mac mini|imac|mac studio", re.I)
DESKTOP = re.compile(r"mac mini|mac studio|imac|mini pc|desktop|gaming pc|tower\b", re.I)
MENS_FORMAL = re.compile(r"\bsuits?\b|blazer|sport ?coat|dress shirt|\bties?\b|oxford shoe|loafer|brogue|cashmere|tailor|menswear|luxury", re.I)
ACCESSORY = re.compile(r"\bhub\b|dock|\bcase\b|charger|sleeve|\bstand\b|adapter|cable|keyboard|mouse|screen protector|backpack|bag\b|skin\b|cooling pad", re.I)
# Enough CPU/RAM for real video editing: Apple silicon, or a fast x86 chip with 16GB+.
CREATIVE_SPEC = re.compile(r"\bm[1-6]\b(?! ?\.2)|apple silicon|(ryzen\s?[79]|ryzen ai|core\s?(ultra\s?)?[79]|\bi[79]\b).*\b(16|24|32|48|64)\s?gb|\b(16|24|32|48|64)\s?gb\b.*(ryzen\s?[79]|ryzen ai|core\s?(ultra\s?)?[79]|\bi[79]\b)", re.I)
REFURB = re.compile(r"refurb|renewed|open[- ]box|pre-owned|certified", re.I)
HIDE = re.compile(r"windows 1[01]|office (pro|home|20\d\d)|microsoft 365|license|product key|lifetime (license|subscription|access)|subscription|paramount\+|netflix|hulu|disney\+|peacock|max streaming|vpn|free trial|streaming|\bpet\b|\bdog\b|\bcat\b|puppy|kitten|litter", re.I)
ANOMALY_WORDS = re.compile(r"price (mistake|error|glitch)|pricing (error|mistake)|\bmispriced\b|\bpenny (deal|item|list|find)s?\b|\$0?\.01\b|\bPM\b.*\bYMMV\b", re.I)

STORE_NAMES = {
    "amazon": "Amazon", "walmart": "Walmart", "target": "Target", "bestbuy": "Best Buy",
    "best-buy": "Best Buy", "home-depot": "Home Depot", "the-home-depot": "Home Depot",
    "homedepot": "Home Depot", "lowes": "Lowe's", "costco": "Costco", "costco-wholesale": "Costco",
    "woot": "Woot", "ebay": "eBay", "newegg": "Newegg", "kohls": "Kohl's", "macys": "Macy's",
    "microcenter": "Micro Center", "bhphotovideo": "B&H", "adorama": "Adorama", "dell": "Dell",
    "lenovo": "Lenovo", "hp": "HP", "apple": "Apple", "patagonia": "Patagonia", "rei": "REI",
    "backcountry": "Backcountry", "steepandcheap": "Steep & Cheap", "moosejaw": "Moosejaw",
    "sierra": "Sierra", "nordstrom": "Nordstrom", "nordstromrack": "Nordstrom Rack",
    "dickssportinggoods": "Dick's", "asus": "ASUS", "samsung": "Samsung", "gap": "Gap",
    "llbean": "L.L.Bean", "evo": "evo", "zappos": "Zappos", "sierratradingpost": "Sierra",
    "arcteryx": "Arc'teryx", "thenorthface": "The North Face",
}
TITLE_STORES = [("home depot", "Home Depot"), ("lowe's", "Lowe's"), ("lowes", "Lowe's"),
                ("walmart", "Walmart"), ("target", "Target"), ("best buy", "Best Buy"),
                ("costco", "Costco"), ("amazon", "Amazon"), ("micro center", "Micro Center"),
                ("microcenter", "Micro Center"), ("newegg", "Newegg"), ("b&h", "B&H"),
                ("rei", "REI"), ("backcountry", "Backcountry"), ("woot", "Woot"),
                ("ebay", "eBay"), ("dell", "Dell"), ("lenovo", "Lenovo")]

MONEY = r"\$\s?([\d,]+(?:\.\d{1,2})?)"


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def category(title):
    t = title.lower()
    for name, pat in CATEGORIES:
        if re.search(pat, t):
            return name
    return "Other"


def to_num(s):
    try:
        return round(float(s.replace(",", "")), 2)
    except (ValueError, AttributeError):
        return None


def parse_date(s):
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return iso(dt.astimezone(timezone.utc))
        except ValueError:
            continue
    return None


def parse_prices(text):
    """Return (price, was, pct) from a deal headline or blurb."""
    t = text.replace(" ", " ")
    pct = None
    m = re.search(r"(?<!up to )(?<!up to  )\b(\d{1,3})\s?%\s?off", t, re.I)
    if m and 0 < int(m.group(1)) <= 100 and not re.search(r"up to\s*" + m.group(1) + r"\s?%", t, re.I):
        pct = int(m.group(1))
    was = None
    m = re.search(r"(?:reg\.?|was|list(?: price)?|orig(?:inal|\.)?|msrp|retail|compare at)\s*:?\s*" + MONEY, t, re.I)
    if m:
        was = to_num(m.group(1))
    # "$335 off" is a discount, not the price
    prices = [p for p in (to_num(x) for x in re.findall(MONEY + r"(?!\s*off\b)(?![\d,.])", t)) if p is not None]
    if FREE_ITEM.search(t) and not JUNK_FREE.search(t) and not prices:
        return 0.0, was, 100 if was else pct
    price = next((p for p in prices if p != was), prices[0] if prices else None)
    # "$1299 ($1599 - 300)" style from Reddit
    m = re.search(MONEY + r"\s*\(\s*" + MONEY + r"\s*-\s*\$?\s?([\d,.]+)", t)
    if m:
        price, was = to_num(m.group(1)), to_num(m.group(2))
    # "$X off" where the final price is also given
    if was is None and price is not None:
        m = re.search(MONEY + r"\s+off\b", t, re.I)
        if m and to_num(m.group(1)) and to_num(m.group(1)) != price:
            was = round(price + to_num(m.group(1)), 2)
    if was is not None and price is not None and was <= price:
        was = None
    return price, was, pct


def store_from_url(url):
    host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    parts = host.split(".")
    key = parts[-2] if len(parts) >= 2 else host
    return STORE_NAMES.get(key) or (key.title() if key else "")


def store_from_title(title):
    m = re.search(r"\bat ([A-Z][\w&'.\- ]{1,24})$", title)
    if m:
        return m.group(1).strip()
    t = title.lower()
    for key, name in TITLE_STORES:
        if re.search(r"\b" + re.escape(key) + r"\b", t):
            return name
    return ""


def base(did, src, tag, title, url, store, price, was, pct, posted, img=None, **extra):
    d = {"id": did, "src": src, "store": store or "Online", "title": title[:200],
         "price": price, "was": was, "pct0": pct, "url": url, "cat": category(title),
         "score": None, "posted": posted, "tags": [tag], "img": img,
         "clr": "clearance" in title.lower() or tag == "q:clearance"}
    d.update(extra)
    return d


def items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    rss = list(root.iter("item"))
    return rss or list(root.iter("{http://www.w3.org/2005/Atom}entry"))


def parse_sd(tag, raw):
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        m = re.search(r"/f/(\d+)", link)
        if not title or not m:
            continue
        body = item.findtext("content:encoded", default="", namespaces=ns)
        slug = re.search(r'data-store-slug="([^"]+)"', body)
        store = (STORE_NAMES.get(slug.group(1), slug.group(1).replace("-", " ").title())
                 if slug else store_from_title(title))
        score = re.search(r"Thumb Score:\s*([+-]?\d+)", body)
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        price, was, pct = parse_prices(title)
        out.append(base("sd" + m.group(1), "Slickdeals", tag, title, link.split("?")[0], store,
                        price, was, pct, parse_date(item.findtext("pubDate")),
                        img.group(1) if img else None,
                        score=int(score.group(1)) if score else None,
                        free=tag == "freebies"))
    return out


def parse_dn(tag, raw):
    dn = "{https://www.dealnews.com/ns/rss/1.0.htm}"
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").split("?")[0]
        m = re.search(r"/(\d+)\.html", link)
        if not title or not m:
            continue
        desc = html.unescape(re.sub(r"<[^>]+>", " ", item.findtext("description") or ""))
        price_txt = item.findtext(dn + "price")
        p2, was, pct = parse_prices(title + " " + desc[:400])
        price = to_num(price_txt) if price_txt else p2
        if price == 0 and not (FREE_ITEM.search(title) and not JUNK_FREE.search(title)):
            price = p2 or None
        media = item.find("{http://search.yahoo.com/mrss/}content")
        img = media.get("url") if media is not None else None
        if img:
            img = re.sub(r"h=\d+&w=\d+", "h=300&w=300", img)
        out.append(base("dn" + m.group(1), "DealNews", tag, title, link,
                        html.unescape(item.findtext(dn + "retailer") or ""), price, was, pct,
                        parse_date(item.findtext("pubDate")), img,
                        pick=(item.findtext(dn + "staffPick") or "") == "true"))
    return out


def parse_bens(tag, raw):
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("guid") or item.findtext("link") or "").split("#")[0]
        m = re.search(r"-(\d+)/?$", link)
        if not title or not m:
            continue
        body = item.findtext("description") or ""
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        img = img.group(1) if img else None
        if img and img.startswith("//"):
            img = "https:" + img
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
        price, was, pct = parse_prices(title)
        if was is None:
            _, was2, pct2 = parse_prices(text[:500])
            m2 = re.search(r"on sale for " + MONEY, text)
            if m2 and price is not None and (to_num(m2.group(1)) or 0) > price:
                was = to_num(m2.group(1))
            was = was or was2
            pct = pct or pct2
        out.append(base("bn" + m.group(1), "Ben's Bargains", tag, title, link,
                        store_from_title(title), price, was, pct,
                        parse_date(item.findtext("pubDate")), img))
    return out


def parse_camel(tag, raw):
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        m = re.search(r"^(.*) - down ([\d.]+)% \(" + MONEY + r"\) to " + MONEY + r" from " + MONEY, title)
        asin = link.rstrip("/").split("/")[-1]
        if not m or not asin:
            continue
        pct = round(float(m.group(2)))
        if pct < 15:   # the feed is mostly small wobbles; keep the real drops
            continue
        name = m.group(1).replace("...", "…")
        out.append(base("cc" + asin, "camelcamelcamel", tag,
                        f"{name} — down {pct}% to ${m.group(4)}", link, "Amazon",
                        to_num(m.group(4)), to_num(m.group(5)), pct,
                        parse_date(item.findtext("pubDate"))))
    return out


def parse_reddit(tag, raw):
    A = "{http://www.w3.org/2005/Atom}"
    out = []
    for e in items(raw):
        title = html.unescape((e.findtext(A + "title") or "").strip())
        rid = e.findtext(A + "id") or ""
        link_el = e.find(A + "link")
        link = link_el.get("href") if link_el is not None else ""
        if not title or not rid:
            continue
        content = html.unescape(e.findtext(A + "content") or "")
        target = re.search(r'<a href="([^"]+)">\[link\]', content)
        lead = re.match(r"\[([^\]]+)\]", title)
        lead_store = STORE_NAMES.get(re.sub(r"[^a-z]", "", lead.group(1).lower())) if lead else None
        store = lead_store or (store_from_url(target.group(1))
                               if target and "reddit.com" not in target.group(1) else store_from_title(title))
        thumb = e.find("{http://search.yahoo.com/mrss/}thumbnail")
        cat = e.find(A + "category")
        sub = cat.get("term") if cat is not None else "reddit"
        price, was, pct = parse_prices(title)
        out.append(base("rd" + rid, "r/" + sub, tag, title, link, store, price, was, pct,
                        parse_date(e.findtext(A + "published") or e.findtext(A + "updated")),
                        thumb.get("url") if thumb is not None else None))
    return out


BLOG_SRC = {"9to5toys": "9to5Toys", "macrumors": "MacRumors", "dappered": "Dappered"}


def parse_blog(tag, raw):
    """Deal blogs: keep only posts whose headline carries a price or discount."""
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        if not re.search(r"\$\s?\d|\d+\s?% off|\bsale\b|\bdeal", title, re.I) or re.search(r"\bwin it\b|giveaway", title, re.I):
            continue
        body = item.findtext("content:encoded", default="", namespaces=ns) or item.findtext("description") or ""
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        price, was, pct = parse_prices(title)
        slug = re.sub(r"\W+", "", urllib.parse.urlparse(link).path)[-40:]
        out.append(base(tag[:2] + slug, BLOG_SRC.get(tag, tag), tag, title, link,
                        store_from_title(title), price, was, pct,
                        parse_date(item.findtext("pubDate")), img.group(1) if img else None))
    return out


PARSERS = {"sd": parse_sd, "dn": parse_dn, "bens": parse_bens, "camel": parse_camel, "reddit": parse_reddit, "blog": parse_blog}


def flags(d):
    t = d["title"]
    f = []
    if LAPTOP.search(t) and not re.search(r"desktop|tower\b", t, re.I):
        f.append("laptop")
        if HIGH_COMPUTE.search(t):
            f.append("power")
    brand = next((name for name, pat in BRANDS if re.search(pat, t, re.I)), None)
    if brand:
        f.append("brand")
        d["brand"] = brand
    tailor = next((name for name, pat in TAILORING if re.search(pat, t, re.I)), None)
    if tailor and d.get("store") != "eBay":
        f.append("tailor")
        d["brand"] = tailor
    elif (LUX_STORES.search(t + " " + (d.get("store") or "")) and d.get("store") != "eBay"
          and MENS_FORMAL.search(t) and not re.search(r"women|womens|ladies|sneaker|running", t, re.I)):
        f.append("tailor")
    is_pc = ("laptop" in f) or bool(DESKTOP.search(t)) or bool(CREATIVE.search(t))
    if is_pc:
        f.append("pc")
        if DESKTOP.search(t) and HIGH_COMPUTE.search(t) and "power" not in f:
            f.append("power")
    if (is_pc and d.get("price") is not None and 150 <= d["price"] <= 500
            and not ACCESSORY.search(t) and CREATIVE_SPEC.search(t)):
        f.append("creative")
    if REFURB.search(t):
        f.append("refurb")
    if HIDE.search(t) or d.get("cat") == "Toys & Kids":
        f.append("hide")
    pct = d.get("pct") or 0
    genuine_free = FREE_ITEM.search(t) and not JUNK_FREE.search(t)
    if (d.get("free") or d.get("price") == 0) and genuine_free:
        f.append("free")
    if pct >= 95 or "free" in f:
        f.append("p95")
    hist = d.get("hist") or []
    first = hist[0][1] if hist else None
    dropped = bool(first) and d.get("price") is not None and d["price"] <= first * 0.7
    roundup = re.search(r"\bup to\b|\bsale\b|\bsitewide\b", t, re.I)
    if (pct >= 70 and not roundup) or ANOMALY_WORDS.search(t) or dropped or (d.get("price") is not None and 0 < d["price"] <= 0.05):
        f.append("anomaly")
    return f


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

    group = (t.minute // 5) % 3 if known else None   # first run fetches every search
    feeds = list(CORE) + [("sd", "q:" + q, SD + "q=" + urllib.parse.quote_plus(q))
                          for i, q in enumerate(SEARCHES) if group is None or i % 3 == group]

    fresh, errors, sources = {}, [], {}
    for kind, tag, url in feeds:
        try:
            for d in PARSERS[kind](tag, get(url)):
                if d["id"] in fresh:
                    fresh[d["id"]]["tags"] = sorted(set(fresh[d["id"]]["tags"] + d["tags"]))
                else:
                    fresh[d["id"]] = d
            sources[tag] = "ok"
        except Exception as e:  # one bad feed shouldn't stop the rest
            errors.append(f"{tag}: {e}")
            sources[tag] = "error"
        time.sleep(2 if kind == "reddit" else 0.7)

    stamp = iso(t)
    too_old = iso(t - timedelta(days=MAX_AGE_DAYS))
    fresh = {k: v for k, v in fresh.items() if not v.get("posted") or v["posted"] >= too_old}
    for did, d in fresh.items():
        old = known.get(did, {})
        hist = old.get("hist", [])
        if d["price"] is not None and (not hist or hist[-1][1] != d["price"]):
            hist = (hist + [[stamp, d["price"]]])[-20:]
        d["hist"] = hist
        d["seen"] = old.get("seen", stamp)
        d["last"] = stamp
        d["tags"] = sorted(set(d["tags"]) | set(old.get("tags", [])))[:8]
        if d.get("was") and d["price"] is not None and d["was"] > 0:
            d["pct"] = round(100 * (1 - d["price"] / d["was"]))
        else:
            d["pct"] = d.get("pct0")
        d.pop("pct0", None)
        d["flags"] = flags(d)
        known[did] = d

    cutoff = iso(t - timedelta(days=KEEP_DAYS))
    known = {k: v for k, v in known.items()
             if v.get("last", "") >= cutoff and (not v.get("posted") or v["posted"] >= too_old)}
    for d in known.values():   # deals carried over from earlier runs get current flags too
        d["flags"] = flags(d)
    if not STATE_URL:
        json.dump({"deals": known}, open(STATE, "w"))

    ranked = sorted(known.values(), key=lambda d: (d.get("posted") or d["seen"]), reverse=True)[:MAX_DEALS]
    os.makedirs(SITE, exist_ok=True)
    out = os.path.join(SITE, "deals.json")
    json.dump({"updatedAt": stamp, "count": len(ranked), "sources": sources,
               "errors": errors[:8], "deals": ranked}, open(out, "w"), separators=(",", ":"))
    ok = sum(v == "ok" for v in sources.values())
    print(f"{len(ranked)} deals, {ok}/{len(sources)} feeds ok, bytes={os.path.getsize(out)}")
    if errors:
        print("errors:", errors, file=sys.stderr)


if __name__ == "__main__":
    main()
