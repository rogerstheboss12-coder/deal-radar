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
from concurrent.futures import ThreadPoolExecutor
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
    ("blog", "windowscentral", "https://www.windowscentral.com/feeds/tag/deals"),
    ("blog", "putthison", "https://putthison.com/feed/"),
    ("blog", "dealcatcher", "https://www.dealcatcher.com/rss"),
    ("blog", "theinventory", "https://theinventory.com/rss"),
    ("blog", "hardforum", "https://hardforum.com/forums/h-ot-deals.28/index.rss"),
    ("blog", "macprices", "https://www.macprices.net/feed/"),
    ("blog", "appleinsider", "https://appleinsider.com/rss/news"),
    ("dn", "dn-mens", "https://www.dealnews.com/c202/Clothing-Accessories/Mens/?rss=1&sort=time"),
    ("dn", "dn-laptops", "https://www.dealnews.com/c39/Computers/Laptops/?rss=1"),
    ("blog", "gearpatrol", "https://www.gearpatrol.com/feed/"),
    ("blog", "stitchdown", "https://www.stitchdown.com/feed"),
    # Open-box stock sells out within hours, so these searches run every time.
    ("sd", "q:open box", SD + "q=open+box"),
    ("sd", "q:open box laptop", SD + "q=open+box+laptop"),
    ("dn", "dn-clearance", "https://www.dealnews.com/f1906/Clearance/?rss=1"),
    ("dn", "dn-staffpicks", "https://www.dealnews.com/f1682/Staff-Pick/?rss=1"),
    ("shopify", "nmwa", "https://www.nomanwalksalone.com/collections/sale/products.json?limit=250"),
    ("shopify", "skoa", "https://www.skoaktiebolaget.com/collections/sale/products.json?limit=250"),
    ("shopify", "orazio", "https://www.orazioluciano.com/collections/sale/products.json?limit=250"),
    ("shopify", "herring", "https://www.herringshoes.co.uk/collections/sale/products.json?limit=250"),
    ("shopify", "tanda", "https://www.turnbullandasser.co.uk/collections/sale/products.json?limit=250"),
    ("shopify", "evo-patagonia", "https://www.evo.com/collections/patagonia/products.json?limit=250"),
    ("shopify", "evo-tnf", "https://www.evo.com/collections/the-north-face/products.json?limit=250"),
    ("shopify", "evo-mh", "https://www.evo.com/collections/mountain-hardwear/products.json?limit=250"),
    ("shopify", "cotopaxi", "https://cotopaxi.com/collections/sale/products.json?limit=250"),
    ("shopify", "filson", "https://www.filson.com/collections/sale/products.json?limit=250"),
    ("shopify", "or", "https://www.outdoorresearch.com/collections/sale/products.json?limit=250"),
    ("shopify", "sanpetuna", "https://sanpetuna.com/en-us/collections/sale/products.json?limit=250"),
    ("shopify", "sartoriale", "https://www.sartoriale.com/collections/sale/products.json?limit=250"),
    ("shopify", "johnstons", "https://johnstonsofelgin.com/collections/mens-sale/products.json?limit=250"),
    ("shopify", "osweeney", "https://www.oliversweeney.com/collections/sale-footwear/products.json?limit=250"),
    ("shopify", "sb-patagonia", "https://www.sportsbasement.com/collections/patagonia/products.json?limit=250"),
    ("shopify", "sb-tnf", "https://www.sportsbasement.com/collections/the-north-face/products.json?limit=250"),
    ("shopify", "tiso", "https://www.tiso.com/collections/sale/products.json?limit=250"),
    ("shopify", "techable", "https://techable.com/products.json?limit=250"),
    ("shopify", "refurbio", "https://us.refurb.io/products.json?limit=250"),
    ("shopify", "sysliq", "https://systemliquidation.com/products.json?limit=250"),
    ("dn", "dn-hot", "https://www.dealnews.com/?rss=1&sort=hotness"),
    ("dn", "dn-new", "https://www.dealnews.com/?rss=1"),
    ("dn", "dn-computers", "https://www.dealnews.com/c39/Computers/?rss=1&sort=time"),
    ("bens", "bens", "https://bensbargains.com/rss/"),
    ("camel", "camel", "https://camelcamelcamel.com/top_drops/feed"),
    # One combined request: Reddit rate-limits quickly, and polling every few
    # minutes still catches every new post.
    ("reddit", "reddit", "https://www.reddit.com/r/buildapcsales+LaptopDeals+deals+DealsReddit/new/.rss?limit=100"),
    ("reddit", "reddit-fashion", "https://www.reddit.com/r/frugalmalefashion+FrugalFemaleFashion/new/.rss?limit=50"),
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
    "780m", "m1 pro", "refurbished laptop", "monitor", "macbook open box", "dell outlet", "lenovo outlet", "micro center",
    # Tailoring, shoes and accessories
    "kiton", "isaia", "cesare attolini", "luigi borrelli", "sartorio", "edward green",
    "crockett jones", "alden shoes", "drakes", "charvet",
    # Off-price and luxury retailer sales
    "nordstrom rack", "neiman marcus last call", "gilt", "yoox",
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

LAPTOP = re.compile(r"laptop|notebook|macbook|zephyrus|legion|alienware|razer blade|\bomen\b|predator|rog strix|tuf gaming|xps|zenbook|thinkpad|precision|zbook", re.I)
HIGH_COMPUTE = re.compile(r"rtx\s?(40[6-9]0|50[6-9]0)|m[345]\s?(pro|max)|ryzen\s?(9|ai)|core\s?(i9|ultra\s?9)|\bi9\b|(32|64|96|128)\s?gb|workstation|quadro|rtx\s?a\d000|4090|5090|5080|4080", re.I)
BRANDS = [
    ("Patagonia", r"patagonia"), ("Arc'teryx", r"arc'?teryx"), ("The North Face", r"north face"),
    ("Fjällräven", r"fj[aä]llr[aä]ven"), ("Canada Goose", r"canada goose"), ("Mammut", r"mammut"),
    ("Outdoor Research", r"outdoor research"), ("Black Diamond", r"black diamond"),
    ("Cotopaxi", r"cotopaxi"), ("Filson", r"filson"), ("Barbour", r"barbour"), ("Rab", r"\brab\b"),
    ("Mountain Hardwear", r"mountain hardwear"), ("Smartwool", r"smartwool"), ("Icebreaker", r"icebreaker"),
    ("Kuhl", r"\bk[uü]hl\b"), ("Salomon", r"salomon"), ("Hoka", r"\bhoka\b"), ("On", r"\bon (running|cloud)"),
    ("Yeti", r"(?<!logitech )(?<!logitech g )\byeti\b(?! (mic|microphone|x|nano|gx))"), ("Stanley", r"\bstanley\b"), ("Osprey", r"\bosprey\b"), ("Marmot", r"marmot"),
    ("Columbia", r"columbia sportswear|columbia (men|women)'?s"), ("Merrell", r"merrell"), ("Vuori", r"vuori"),
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
DESKTOP = re.compile(r"mac mini|mac studio|imac|mini pc|\bnuc\b|beelink|minisforum|acemagic|acemagician|gmktec|bosgame|kamrui|desktop|gaming pc|tower\b", re.I)
MENS_FORMAL = re.compile(r"\bsuits?\b|blazer|sport ?coat|dress shirt|\bties?\b|oxford shoe|loafer|brogue|cashmere|tailor|menswear|luxury", re.I)
TAILORED_PIECE = re.compile(r"blazer|sport ?coat|suit\b|jacket|overcoat|\bcoat\b|trouser|pleated|\bties?\b|cashmere|loafer|oxford|derby|brogue|\bboots?\b|chelsea|monk strap|overshirt", re.I)
ACCESSORY = re.compile(r"\bhub\b|dock|\bcase\b|charger|sleeve|\bstand\b|adapter|cable|keyboard|mouse|screen protector|backpack|bag\b|skin\b|cooling pad", re.I)
# Enough CPU/RAM for real video editing: Apple silicon, or a fast x86 chip with 16GB+.
# Video-capable: Apple silicon Pro/Max or 16GB+, or a modern x86 chip/GPU with 16GB+.
APPLE_OK = re.compile(r"\bm[1-6]\s?(pro|max|ultra)\b|\bm[1-6]\b(?! ?\.2).*\b(16|24|32)\s?gb", re.I)
APPLE_LITE = re.compile(r"\bm[1-6]\b(?! ?\.2)", re.I)
X86_OK = re.compile(r"ryzen\s?[79]\s?[5-9]\d{3}|ryzen\s?[79]\s?(2[05]\d|3[05]\d)|ryzen ai|core ultra\s?[579]|i[79]-1[2-4]\d{3}|780m|880m|890m|8060s|arc\s?(140|a\d)|rtx\s?\d{4}", re.I)
RAM16 = re.compile(r"\b(16|24|32|48|64)\s?gb\b(?!\s?(ssd|storage|emmc))", re.I)
WEAK_PC = re.compile(r"chromebook|celeron|pentium|\bn1[05]0\b|\bn9[57]\b|\bi3\b|ryzen\s?3|monitor", re.I)
REFURB = re.compile(r"refurb|renewed|open[- ]box|pre-owned|certified", re.I)
HIDE = re.compile(r"windows 1[01] (pro |home )?(key|license|oem|activation)|office (pro|home|20\d\d)|microsoft 365|license|product key|lifetime (license|subscription|access)|subscription|paramount\+|netflix|hulu|disney\+|peacock|max streaming|vpn|free trial|streaming|\bpet\b|\bdog\b|\bcat\b|puppy|kitten|litter", re.I)
ROUNDUP = re.compile(r"\bup to \$?\d+%?\s*off\b|\bsale\b(?!.*\$\d)|\bsitewide\b|\bextra \d+|\bgift card|\bcredit\b|\bevent\b|\bsavings\b|select (styles|items)|\bdeals? (on|at)\b|\bdeals from\b|\bfrom \$\d", re.I)
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
SHIP_MIN = re.compile(r"(?:free\s+)?(?:shipping|s&h|store pickup)\s+(?:w/|with|on)\s+(?:orders?\s+(?:of\s+)?)?\$[\d,.]+\+?", re.I)


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
    # Shipping minimums, "$335 off", "save $200", "$50 credit" and "$1.2k" are not the price
    t = SHIP_MIN.sub(" ", t)
    prices = [p for p in (to_num(x) for x in re.findall(
        r"(?<!save )(?<!up to )" + MONEY + r"(?![.,]?\d)(?!\s*(?:off|credit|gift card|back|in rewards)\b)(?!\s*k\b)", t, re.I))
        if p is not None]
    if FREE_ITEM.search(t) and not JUNK_FREE.search(t) and not prices:
        return 0.0, was, 100 if was else pct
    price = next((p for p in prices if p != was), prices[0] if prices else None)
    m = re.search(r"=\s*" + MONEY, t)   # "($1299-$719 = $580)"
    if m:
        price = to_num(m.group(1))
    # "$1299 ($1599 - 300)" style from Reddit
    m = re.search(MONEY + r"\s*\(\s*" + MONEY + r"\s*-\s*\$?\s?([\d,.]+)", t)
    if m:
        price, was = to_num(m.group(1)), to_num(m.group(2))
    # "$X off" where the final price is also given
    if was is None and price is not None:
        m = re.search(r"(?<!up to )" + MONEY + r"\s+off\b", t, re.I)
        if m and to_num(m.group(1)) and to_num(m.group(1)) != price:
            was = round(price + to_num(m.group(1)), 2)
    if was is not None and price is not None and was <= price:
        was = None
    return price, was, pct


WAS_PATTERNS = [
    # Slickdeals blurb: "on sale for $89.99 - $24.30 off" -> list price is the first number
    re.compile(r"\b(?:on sale for|priced at)\s+\*?\$\s?([\d,.]+)\*?\s*-\s*(?:\$[\d,.]+|\d+\s?%)", re.I),
    re.compile(r"\b(?:reg(?:ular)?(?:ly)?\.?(?:\s+price)?(?:\s+of)?|normally|usually|originally|msrp|list(?:\s+price)?|(?:down|dropped|marked down)\s+from)\s*:?\s*\$\s?([\d,.]+)", re.I),
    re.compile(r"\$\s?([\d,.]+)\s*(?:list|regular|full)(?:\s+price)?\b", re.I),
]
BUY_REG = re.compile(r"Buy (?:for|at) \$([\d,.]+)\s*\(\s*Reg\.?\s*\$([\d,.]+)\s*\)", re.I)
SAVE_AMT = re.compile(r"\$([\d,.]+)\s+(?:off|discount|in savings)\b", re.I)


def extract_was(text, price):
    """Find the original price in a deal blurb. Only trusted when it is above the deal
    price and not absurdly so (list prices on marketplaces can be fantasy)."""
    if price is None or price <= 0 or not text:
        return None
    for pat in WAS_PATTERNS:
        for m in pat.finditer(text):
            was = to_num(m.group(1).rstrip("."))
            if was and price < was <= price * 20:
                return was
    m = SAVE_AMT.search(text)
    if m and to_num(m.group(1).rstrip(".")):
        was = round(price + to_num(m.group(1).rstrip(".")), 2)
        if was <= price * 20:
            return was
    return None


def plain(html_text):
    return html.unescape(re.sub(r"<[^>]+>", " ", html_text or ""))


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
        if was is None and not ROUNDUP.search(title):
            was = extract_was(plain(body)[:1500], price)
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
        if was is None and not ROUNDUP.search(title):
            was = extract_was(desc[:1500], price)
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


BLOG_SRC = {"putthison": "Put This On", "dealcatcher": "DealCatcher", "theinventory": "The Inventory",
            "hardforum": "HardForum", "macprices": "MacPrices", "appleinsider": "AppleInsider", "gearpatrol": "Gear Patrol", "stitchdown": "Stitchdown", "9to5toys": "9to5Toys", "macrumors": "MacRumors", "dappered": "Dappered", "windowscentral": "Windows Central"}


def parse_blog(tag, raw):
    """Deal blogs: keep only posts whose headline carries a price or discount."""
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    out = []
    for item in items(raw):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        if tag != "putthison" and (not re.search(r"\$\s?\d|\d+\s?% off|\bsale\b|\bdeal", title, re.I) or re.search(r"\bwin it\b|giveaway", title, re.I)):
            continue
        if tag in ("putthison", "gearpatrol", "stitchdown") and not re.search(r"inside track|\bsale\b|\bdeals?\b|% off|\$\d", title, re.I):
            continue
        body = item.findtext("content:encoded", default="", namespaces=ns) or item.findtext("description") or ""
        img = re.search(r'<img[^>]+src="([^"]+)"', body)
        enc = item.find("enclosure")
        img_url = enc.get("url") if enc is not None and (enc.get("type") or "").startswith("image") else (img.group(1) if img else None)
        if tag == "hardforum" and re.search(r"\[(dead|ended|expired|oos)\]", title, re.I):
            continue
        price, was, pct = parse_prices(title)
        text = plain(body)[:2000]
        br = BUY_REG.search(text)
        if br:
            price, was = to_num(br.group(1)), to_num(br.group(2))
        elif was is None and not ROUNDUP.search(title):
            was = extract_was(text, price)
        slug = re.sub(r"\W+", "", urllib.parse.urlparse(link).path)[-40:]
        out.append(base(tag[:2] + slug, BLOG_SRC.get(tag, tag), tag, title, link,
                        store_from_title(title), price, was, pct,
                        parse_date(item.findtext("pubDate")), img_url))
    return out


# tag: (store name, host, currency, vendors that count as fine tailoring/shoes — None = all)
SHOPIFY_STORES = {"nmwa": ("No Man Walks Alone", "https://www.nomanwalksalone.com", "USD", None),
                  "skoa": ("Skoaktiebolaget", "https://www.skoaktiebolaget.com", "USD", None),
                  "orazio": ("Orazio Luciano", "https://www.orazioluciano.com", "EUR", None),
                  "herring": ("Herring Shoes", "https://www.herringshoes.co.uk", "GBP",
                              r"church|tricker|carlos santos|crockett|edward green|alden"),
                  "tanda": ("Turnbull & Asser", "https://www.turnbullandasser.co.uk", "GBP", None),
                  "tiedeals": ("TieDeals", "https://tiedeals.com", "USD", None),
                  "evo-patagonia": ("evo", "https://www.evo.com", "USD", None),
                  "evo-tnf": ("evo", "https://www.evo.com", "USD", None),
                  "evo-mh": ("evo", "https://www.evo.com", "USD", None),
                  "cotopaxi": ("Cotopaxi", "https://cotopaxi.com", "USD", None),
                  "filson": ("Filson", "https://www.filson.com", "USD", None),
                  "or": ("Outdoor Research", "https://www.outdoorresearch.com", "USD", None)}
SHOPIFY_STORES.update({
    "sanpetuna": ("San Petuna", "https://sanpetuna.com/en-us", "USD", None),
    "sartoriale": ("Sartoriale", "https://www.sartoriale.com", "USD", None),
    "johnstons": ("Johnstons of Elgin", "https://johnstonsofelgin.com", "GBP", None),
    "osweeney": ("Oliver Sweeney", "https://www.oliversweeney.com", "GBP", None),
    "sb-patagonia": ("Sports Basement", "https://www.sportsbasement.com", "USD", None),
    "sb-tnf": ("Sports Basement", "https://www.sportsbasement.com", "USD", None),
    "tiso": ("Tiso", "https://www.tiso.com", "GBP", None),
    "techable": ("Techable", "https://techable.com", "USD", None),
    "refurbio": ("refurb.io", "https://us.refurb.io", "USD", None),
    "sysliq": ("System Liquidation", "https://systemliquidation.com", "USD", None),
})
OUTDOOR_SHOPS = {"evo-patagonia", "evo-tnf", "evo-mh", "cotopaxi", "filson", "or", "sb-patagonia", "sb-tnf", "tiso"}
MULTI_BRAND_OUTDOOR = {"tiso"}                 # only named premium brands count as "brand" here
REFURB_SHOPS = {"techable", "refurbio", "sysliq"}
PAGES = {"herring": 3, "tiedeals": 4, "tiso": 3, "sanpetuna": 2, "sb-patagonia": 2, "sb-tnf": 2,
         "techable": 2, "sysliq": 2}
PREOWNED = re.compile(r"pre-?owned|\bused\b|vintage|\bworn\b|second-?hand|consign", re.I)


# The shopper's sizes. Pants: any size. Unknown sizes (most feed posts) are not filtered.
SHOE_WORDS = re.compile(r"shoe|boot|loafer|oxford|derby|brogue|sneaker|trainer|chukka|slipper|moccasin|monk|mule|sandal", re.I)
PANT_WORDS = re.compile(r"\bpants?\b|trouser|jeans?\b|chino|shorts?\b|slacks", re.I)
SUIT_WORDS = re.compile(r"\bsuit\b|blazer|sport ?coat|jacket|tuxedo|overcoat|topcoat", re.I)
SHIRT_WORDS = re.compile(r"shirt", re.I)
ALPHA_OK = re.compile(r"^(s|m|l|small|medium|large|sm|md|lg)$", re.I)
ALPHA_ANY = re.compile(r"^(\d?x{0,4}[sl]|m|xs|xxs|\d?xl|x+l|small|medium|large|sm|md|lg|one size|os)$", re.I)


SHOE_SHOPS = {"herring", "osweeney"}
WAIST_OK = (27, 29)          # inches
INSEAM_OK = (27.5, 29)       # inches; shorts ignore inseam
EU_WAIST_OK = (42, 44)       # Italian/EU trouser sizes (44 = 28-29")


def pants_fit(raw, title):
    """Pants and shorts: waist 27-29 and inseam 27.5-29 when stated."""
    shorts = bool(re.search(r"\bshorts?\b", title, re.I))
    if re.search(r"\btall\b", title, re.I) and not shorts:
        return False
    u = raw.strip()
    if re.fullmatch(r"(?i)xs|s|small|x-small|xsmall", u):
        return True
    if ALPHA_ANY.match(u):
        return False
    m = re.search(r"(?i)W?\s?(\d{2})\s*(?:[xX/]|\s+L)\s*L?\s?(\d{2}(?:\.5)?)", u)
    if m:
        waist, inseam = int(m.group(1)), float(m.group(2))
        in_waist = WAIST_OK[0] <= waist <= WAIST_OK[1] or EU_WAIST_OK[0] <= waist <= EU_WAIST_OK[1]
        return in_waist and (shorts or INSEAM_OK[0] <= inseam <= INSEAM_OK[1] + 0.99)
    m = re.search(r"(\d{2})", u)
    if not m:
        return None
    n = int(m.group(1))
    if 40 <= n <= 60:
        ok = EU_WAIST_OK[0] <= n <= EU_WAIST_OK[1]
    elif 24 <= n <= 44:
        ok = WAIST_OK[0] <= n <= WAIST_OK[1]
    else:
        return None
    if ok and not shorts and re.search(r"(?i)\b(regular|long|reg|tall|\bl\b)\b", u):
        return False
    return ok


def size_fits(size, title, store_tag=""):
    """True/False when this size label can be judged for the shopper, None when unclear."""
    raw = size.strip()
    if not raw or raw.lower() in ("default title", "one size", "os", "o/s"):
        return None
    if PANT_WORDS.search(title) and not SUIT_WORDS.search(title):
        return pants_fit(raw, title)
    if ALPHA_ANY.match(raw):
        return bool(ALPHA_OK.match(raw))
    m = re.search(r"(\d{1,2}(?:\.5)?)", raw)
    if not m:
        return None
    n = float(m.group(1))
    u = raw.upper()
    if PANT_WORDS.search(title) and not SUIT_WORDS.search(title):
        return pants_fit(raw, title)
    us = re.search(r"\bUS[-\s]?(\d{1,2}(?:\.5)?)\b", u)
    if us and (SHOE_WORDS.search(title) or store_tag in SHOE_SHOPS):   # "UK-8 ** US-9 ** EU-42"
        return 6.5 <= float(us.group(1)) <= 10
    if SHOE_WORDS.search(title) or store_tag in SHOE_SHOPS:
        if "UK" in u or (store_tag == "herring" and n < 20):   # Herring lists UK sizes
            return 6 <= n <= 9.5
        if "EU" in u or n >= 35:
            return 39.5 <= n <= 43
        return 6.5 <= n <= 10          # bare small numbers at US stores are US sizes
    if SHIRT_WORDS.search(title) and 13 <= n <= 19:
        return 14.5 <= n <= 16.5       # collar sizes ~ S-L
    if 44 <= n <= 60:
        return 46 <= n <= 50           # EU/IT tailoring and knitwear
    if 34 <= n <= 44 and SUIT_WORDS.search(title):
        return n in (36, 38, 40)
    return None


def judge_sizes(sizes, title, store_tag=""):
    verdicts = [(sz, size_fits(sz, title, store_tag)) for sz in sizes]
    known = [(sz, v) for sz, v in verdicts if v is not None]
    if not known:
        return None, []
    ok = [sz for sz, v in known if v]
    return bool(ok), ok


def title_size_fit(title):
    """Single-size listings that state the size in the title, e.g. 'EU 58 / US 48'."""
    m = re.search(r"\bEU\s?(\d{2})\s?/\s?US\s?(\d{2})", title)
    if m and SUIT_WORDS.search(title):
        return int(m.group(2)) in (36, 38, 40), [f"US {m.group(2)}"]
    m = re.search(r"\bsize\s?(\d{1,2}(?:\.5)?)\b", title, re.I)
    if m and SHOE_WORDS.search(title):
        n = float(m.group(1))
        return 6.5 <= n <= 10, [m.group(1)]
    return None, []


def parse_shopify(tag, raw):
    """Public Shopify sale collections: compare_at_price vs price is a real markdown."""
    store, host, cur, fine = SHOPIFY_STORES[tag]
    out = []
    for p in json.loads(raw).get("products", []):
        live = [v for v in p.get("variants", []) if v.get("available")]
        prices = [to_num(v.get("price")) for v in live if to_num(v.get("price"))]
        compares = [to_num(v.get("compare_at_price")) for v in live if to_num(v.get("compare_at_price"))]
        if not prices or not compares:
            continue
        price, was = min(prices), max(compares)
        if was <= price or price > was * 0.7:     # keep 30%+ markdowns only
            continue
        tags = p.get("tags") or []
        tags = " ".join(tags) if isinstance(tags, list) else str(tags)
        if PREOWNED.search(p.get("title", "") + " " + tags):
            continue
        vendor = p.get("vendor") or ""
        title = f"{vendor} {p.get('title', '')}".strip() if vendor and vendor.lower() not in p.get("title", "").lower() else p.get("title", "")
        img = (p.get("images") or [{}])[0].get("src")
        names = [o.get("name", "").lower() for o in p.get("options", [])]
        si = next((i for i, nm in enumerate(names) if "size" in nm), None)
        sizes = [v.get(f"option{si + 1}") or "" for v in live] if si is not None else []
        fit, fit_sizes = judge_sizes(sizes, title, tag) if sizes else (None, [])
        if fit is None:
            fit, fit_sizes = title_size_fit(title)
        out.append(base(f"sh{tag}{p['id']}", store, tag, title, f"{host}/products/{p['handle']}",
                        store, price, was, None, None,
                        img, vendor=vendor, apparel=tag not in OUTDOOR_SHOPS | REFURB_SHOPS,
                        outdoor=tag in OUTDOOR_SHOPS, multi=tag in MULTI_BRAND_OUTDOOR,
                        refurb_shop=tag in REFURB_SHOPS, cur=cur,
                        fine=not fine or bool(re.search(fine, vendor, re.I)),
                        fit=fit, sizes=list(dict.fromkeys(fit_sizes))[:6]))
    return out


def parse_apple(tag, raw):
    m = re.search(rb"window\.REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\});\s*</script>", raw, re.S)
    if not m:
        raise ValueError("refurb data not found")
    out = []
    for tile in json.loads(m.group(1)).get("tiles", []):
        price = to_num(((tile.get("price") or {}).get("currentPrice") or {}).get("raw_amount"))
        title = tile.get("title") or ""
        if not price or not re.search(r"mac", title, re.I):
            continue
        srcs = ((tile.get("image") or {}).get("sources") or [{}])
        img = (srcs[0].get("srcSet") or "").split(" ")[0] or None
        out.append(base("ap" + (tile.get("partNumber") or title).replace("/", ""), "Apple Refurbished", tag,
                        title, "https://www.apple.com" + (tile.get("productDetailsUrl") or "").split("?")[0],
                        "Apple", price, None, None, None, img))
    return out


PARSERS = {"sd": parse_sd, "dn": parse_dn, "bens": parse_bens, "camel": parse_camel, "reddit": parse_reddit, "blog": parse_blog, "shopify": parse_shopify, "apple": parse_apple}


def flags(d):
    t = d["title"]
    f = []
    if LAPTOP.search(t) and not re.search(r"desktop|tower\b", t, re.I) and not ACCESSORY.search(t):
        f.append("laptop")
        if HIGH_COMPUTE.search(t):
            f.append("power")
    brand = next((name for name, pat in BRANDS if re.search(pat, t + " " + (d.get("vendor") or ""), re.I)), None)
    if not brand and d.get("outdoor") and not d.get("multi"):
        brand = d.get("vendor") or d.get("store")
    if brand:
        f.append("brand")
        d["brand"] = brand
    tailor = next((name for name, pat in TAILORING if re.search(pat, t + " " + (d.get("vendor") or ""), re.I)), None)
    if tailor and d.get("store") != "eBay":
        f.append("tailor")
        d["brand"] = tailor
    elif d.get("apparel"):
        # Casual pieces from these shops are "menswear"; tailored pieces and fine shoes
        # (from the shop's approved makers, where it has a list) count as tailoring.
        d["brand"] = d.get("vendor") or d.get("store")
        fine_piece = TAILORED_PIECE.search(t) or d.get("src") == "Orazio Luciano"
        womens = re.search(r"\bladies\b|\bwomen'?s\b|\bwomens\b", t, re.I)
        if not womens:
            f.append("tailor" if fine_piece and d.get("fine", True) else "menswear")
    elif d.get("src") in ("Put This On", "Stitchdown") and not re.search(r"ebay", t, re.I):
        f.append("tailor")
    elif (LUX_STORES.search(t + " " + (d.get("store") or "")) and d.get("store") != "eBay"
          and MENS_FORMAL.search(t) and not re.search(r"women|womens|ladies|sneaker|running", t, re.I)):
        f.append("tailor")
    is_pc = (("laptop" in f) or bool(DESKTOP.search(t)) or bool(CREATIVE.search(t))) and not ACCESSORY.search(t)
    if is_pc:
        f.append("pc")
        if DESKTOP.search(t) and HIGH_COMPUTE.search(t) and "power" not in f:
            f.append("power")
    price = d.get("price")
    refurb = bool(REFURB.search(t)) or bool(d.get("refurb_shop"))
    budget = 550 if refurb else 500
    if (is_pc and price is not None and 150 <= price <= budget and not ACCESSORY.search(t)
            and not WEAK_PC.search(t)):
        is_mac = bool(re.search(r"mac", t, re.I))
        if (is_mac and APPLE_OK.search(t)) or (not is_mac and X86_OK.search(t) and RAM16.search(t)):
            f.append("creative")
        elif is_mac and APPLE_LITE.search(t):
            f.append("creative-lite")
        if price > 500 and ("creative" in f or "creative-lite" in f):
            f.append("over-budget")
    if d.get("fit") is None and "fit" not in d:
        fit, sz = title_size_fit(t)
        if fit is not None:
            d["fit"], d["sizes"] = fit, sz
    if d.get("fit") is False:
        f.append("nofit")
    if REFURB.search(t) or d.get("refurb_shop"):
        f.append("refurb")
    if is_pc and not refurb and not re.search(r"\bused\b|for parts|broken|replacement|motherboard|screen|keyboard|battery|charger|case\b|skin", t, re.I):
        floor = spec_floor(t)
        if floor and price is not None and 0 < price <= 0.4 * floor:
            d["spec_floor"] = floor
            f.append("anomaly")
    if re.search(r"\bladies\b|\bwomen'?s\b|\bwomens\b|\bw's\b|\bkids'?\b|\bgirls'?\b|\bboys'?\b|\byouth\b|\bjunior\b|\btoddler|\bbaby\b|\binfant|\blittle kids\b|\bbig kids\b", t, re.I):
        f.append("womens")
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
    roundup = ROUNDUP.search(t)
    if roundup:
        f.append("roundup")
    if ANOMALY_WORDS.search(t) or (not roundup and (pct >= 70 or dropped)) or (d.get("price") is not None and 0 < d["price"] <= 0.05):
        f.append("anomaly")
    return f


GREEN_MAX, GREEN_PER_SOURCE, GREEN_MIN_PRICE = 20, 4, 40


def green_candidate(d):
    """A real 50%+ markdown on a men's item in the shopper's lanes."""
    f = set(d.get("flags") or [])
    if "refurb" in f and "pc" in f and "creative" not in f and not {"tailor", "brand"} & f:
        return False
    return ((d.get("pct") or 0) >= 50 and d.get("was") and (d.get("price") or 0) >= GREEN_MIN_PRICE
            and not {"roundup", "hide", "womens", "nofit"} & f and bool({"pc", "tailor", "brand"} & f))


def mark_green(deals):
    """Only the very best candidates turn green: ranked by % off plus dollars saved,
    capped per source so one big sale can't take over."""
    ranked = sorted((d for d in deals if green_candidate(d)),
                    key=lambda d: -(d["pct"] + min(d["was"] - d["price"], 1500) / 30))
    per, n = {}, 0
    for d in deals:
        d.pop("green", None)
    for d in ranked:
        if n >= GREEN_MAX or per.get(d["src"], 0) >= GREEN_PER_SOURCE:
            continue
        d["green"] = True
        per[d["src"]] = per.get(d["src"], 0) + 1
        n += 1


# The least a NEW machine with these specs realistically sells for. A listing far below
# this floor is almost always a price error (or a scam listing, which the card warns about).
SPEC_FLOORS = [
    (r"rtx\s?(4090|5090)", 1800), (r"rtx\s?(4080|5080)", 1200), (r"rtx\s?(4070|5070)", 800),
    (r"rtx\s?(4060|5060|3070|3080)", 600), (r"rtx\s?(4050|5050|3060)", 500),
    (r"\bm[1-6]\s?(max|ultra)\b", 1500), (r"\bm[1-6]\s?pro\b", 900),
    (r"macbook pro", 900), (r"macbook air|mac mini|imac", 450),
    (r"alienware|razer blade|rog (strix|zephyrus)|legion (pro|7|9)|msi (raider|stealth|titan|vector|crosshair|katana|sword|pulse)|omen (16|17|max)|predator helios", 700),
    (r"gaming (laptop|desktop|pc)", 450), (r"core ultra [79]|ryzen (ai )?9|\bi9\b", 600),
    (r"(32|64)\s?gb.*(laptop|notebook)|(laptop|notebook).*(32|64)\s?gb", 600),
]


def spec_floor(title):
    floors = [v for pat, v in SPEC_FLOORS if re.search(pat, title, re.I)]
    return max(floors) if floors else None


def golden_candidate(d):
    """Pick-it-up-now tier: a real 90%+ markdown on a men's item worth $100+ at full price,
    in the shopper's lanes; or a reported price error on a computer with a known price."""
    f = set(d.get("flags") or [])
    if {"roundup", "hide", "womens", "nofit"} & f or d.get("price") is None:
        return False
    in_lane = bool({"pc", "tailor", "brand"} & f)
    real_90 = (d.get("pct") or 0) >= 90 and (d.get("was") or 0) >= 100
    pc_error = "pc" in f and "anomaly" in f and bool(ANOMALY_WORDS.search(d["title"])) and d["price"] > 0
    below_spec = bool(d.get("spec_floor")) and 20 <= d["price"] <= 0.25 * d["spec_floor"]
    return in_lane and (real_90 or pc_error or below_spec)


def mark_golden(deals):
    for d in deals:
        d.pop("golden", None)
        if golden_candidate(d):
            d["golden"] = True
            d["green"] = True


def is_green(d):
    return bool(d.get("green"))


def _old_is_green(d):
    f = set(d.get("flags") or [])
    return ((d.get("pct") or 0) >= 50 and d.get("was") and d.get("price") is not None
            and not {"roundup", "hide", "womens", "nofit"} & f and bool({"pc", "tailor", "brand"} & f))


def send_pushes(deals, golden=False):
    """Web Push to every subscribed device. Needs VAPID_PRIVATE_KEY and PUSH_SUBSCRIPTIONS
    (a JSON list of browser subscriptions) in the environment; silently skips otherwise."""
    key, subs = os.environ.get("VAPID_PRIVATE_KEY"), os.environ.get("PUSH_SUBSCRIPTIONS")
    if not deals or not key or not subs:
        return
    try:
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid
    except ImportError:
        print("pywebpush not installed; skipping push", file=sys.stderr)
        return
    vapid = Vapid.from_pem(key.encode())
    subs = json.loads(subs)
    for sub in subs if isinstance(subs, list) else [subs]:
        for d in deals[:5]:
            price = {"EUR": "€", "GBP": "£"}.get(d.get("cur"), "$") + f"{d['price']:,.2f}".replace(".00", "")
            head = f"{price} · {d.get('pct') or '?'}% off · {d['store']}"
            msg = {"title": "Deal Radar is connected" if d["id"] == "test" else ("GOLDEN FIND · " + head if golden else head),
                   "body": d["title"][:140], "url": d["url"], "icon": d.get("img"), "tag": d["id"]}
            try:
                webpush(sub, json.dumps(msg), vapid_private_key=vapid,
                        vapid_claims={"sub": "mailto:deal-radar@users.noreply.github.com"}, ttl=3600)
            except WebPushException as e:
                print(f"push failed: {e}", file=sys.stderr)


def main():
    t = now()
    known, prev_updated = {}, None
    if STATE_URL:
        try:
            prev = json.loads(get(STATE_URL + "?t=" + str(int(time.time()))))
            known = {d["id"]: d for d in prev.get("deals", [])}
            prev_updated = prev.get("updatedAt")
        except Exception as e:
            print(f"no previous state ({e}); starting fresh", file=sys.stderr)
    elif os.path.exists(STATE):
        known = json.load(open(STATE))["deals"]
    was_green = {k for k, v in known.items() if is_green(v)}
    was_golden = {k for k, v in known.items() if v.get("golden")}
    known_prev_ranked = [dict(v) for v in known.values()]

    group = (t.minute // 5) % 3 if known else None   # first run fetches every search
    # Hourly: Apple's refurb page, and TieDeals (it rate-limits, so it gets a light touch;
    # a bot-check response just fails this source for the run, with no retry).
    hourly = [("apple", "apple-refurb", "https://www.apple.com/shop/refurbished/mac"),
              ("shopify", "tiedeals", "https://tiedeals.com/products.json?limit=250")]
    feeds = list(CORE) + (hourly if group is None or t.minute < 5 else []) + [("sd", "q:" + q, SD + "q=" + urllib.parse.quote_plus(q))
                          for i, q in enumerate(SEARCHES) if group is None or i % 3 == group]

    # Different sites are fetched in parallel; requests to the same site stay
    # sequential with a short pause, so no single site sees a burst.
    by_host = {}
    for kind, tag, url in feeds:
        by_host.setdefault(urllib.parse.urlparse(url).netloc, []).append((kind, tag, url))

    def run_host(jobs):
        results = []
        expanded = []
        for kind, tag, url in jobs:
            expanded.append((kind, tag, url))
            expanded += [(kind, tag, url + f"&page={n}") for n in range(2, PAGES.get(tag, 1) + 1)]
        for i, (kind, tag, url) in enumerate(expanded):
            if i:
                time.sleep(4 if kind == "reddit" else 2 if tag == "tiedeals" else 0.5)
            try:
                results.append((tag, PARSERS[kind](tag, get(url)), None))
            except Exception as e:  # one bad feed shouldn't stop the rest
                results.append((tag, [], e))
        return results

    fresh, errors, sources = {}, [], {}
    with ThreadPoolExecutor(max_workers=len(by_host)) as pool:
        for results in pool.map(run_host, by_host.values()):
            for tag, found, err in results:
                if err:
                    errors.append(f"{tag}: {err}")
                    sources[tag] = "error"
                    continue
                sources[tag] = "ok"
                for d in found:
                    if d["id"] in fresh:
                        fresh[d["id"]]["tags"] = sorted(set(fresh[d["id"]]["tags"] + d["tags"]))
                    else:
                        fresh[d["id"]] = d

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
    mark_green(ranked)
    mark_golden(ranked)
    os.makedirs(SITE, exist_ok=True)
    out = os.path.join(SITE, "deals.json")
    for d in ranked:   # keep the file lean
        if len(d.get("hist") or []) < 2:
            d.pop("hist", None)
        d.pop("tags", None)
    status = {"updatedAt": stamp, "count": len(ranked), "sources": sources, "errors": errors[:8]}
    json.dump({**status, "deals": ranked}, open(out, "w"), separators=(",", ":"))
    json.dump(status, open(os.path.join(SITE, "meta.json"), "w"), separators=(",", ":"))
    if os.environ.get("TEST_PUSH") == "true":
        # A real deal: the top green one, else the biggest known discount in the shopper's lanes.
        pool = [d for d in ranked if is_green(d)] or sorted(
            [d for d in ranked if d.get("pct") and d.get("price") is not None
             and {"pc", "tailor", "brand"} & set(d.get("flags") or []) and "roundup" not in d.get("flags", [])],
            key=lambda d: -d["pct"])
        send_pushes(pool[:1])
    fingerprint = sorted((d["id"], d.get("price"), tuple(d.get("flags") or [])) for d in ranked)
    prev_fp = sorted((d["id"], d.get("price"), tuple(d.get("flags") or [])) for d in known_prev_ranked)
    fresh_enough = prev_updated and (t - datetime.strptime(prev_updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)) < timedelta(minutes=10)
    changed = not (fingerprint == prev_fp and fresh_enough)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
    print("changed" if changed else "no changes; skipping deploy")
    if known and STATE_URL:   # only from the live job, never on the very first run
        # Golden finds always notify, first and with their own title.
        send_pushes([d for d in ranked if d.get("golden") and d["id"] not in was_golden][:3], golden=True)
        new_green = [d for d in ranked if is_green(d) and not d.get("golden") and d["id"] not in was_green]
        # A burst means a new source was just added, not a wave of new deals: stay quiet.
        if len(new_green) <= 15:
            send_pushes(new_green)
        else:
            print(f"{len(new_green)} new green deals at once; skipping notifications this run")
    ok = sum(v == "ok" for v in sources.values())
    print(f"{len(ranked)} deals, {ok}/{len(sources)} feeds ok, bytes={os.path.getsize(out)}")
    if errors:
        print("errors:", errors, file=sys.stderr)


if __name__ == "__main__":
    main()
