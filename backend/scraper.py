"""Walmart product scraper.

Walmart publishes the same product through several formats at once, and which
ones are populated varies by page, by A/B bucket, and over time. So rather than
committing to one, each layer fills only the fields still empty and a weak
layer never overwrites a strong one:

  1. JSON-LD  (<script type="application/ld+json">, schema.org/Product)
  2. Embedded app state (__NEXT_DATA__, __WML_REDUX_INITIAL_STATE__)
  3. Microdata (itemprop="..." attributes)
  4. OpenGraph (<meta property="og:..."> / product:price:amount)
  5. URL shape (canonical link, item id in the path)
  6. CSS selectors (last resort, and the first thing to break)

These are all Walmart sources — not other retailers'. Layers 1, 3 and 4 are
open standards Walmart implements, and they are the reason a change to the
hydration blob degrades a scrape instead of ending it.

Layer 2 carries the most, because Walmart renders from a JSON blob and exposes
only a shallow OpenGraph summary: price, UPC and item id are in the blob and
nowhere else. It is found by *shape* — every nested object is scored on how
product-like its keys are — rather than by the path
props.pageProps.initialData.data.product, which Walmart changes without notice.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from models import Product

# Walmart will challenge a server-side request no matter what headers it
# carries — these only make the request well-formed, not trusted. The extension
# is the path that works; see scrape_url.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Ordered: the first pattern that matches the title/description wins, so
# "certified refurbished" must be checked before bare "used". The groups are
# non-capturing because CONDITION_DETAIL below is assembled out of them.
CONDITION_PATTERNS = [
    # "Restored" is Walmart's own word for refurbished, and the one its
    # listings actually use.
    ("refurbished",
     r"\b(?:restored|refurb(?:ished)?|renewed|reconditioned|certified\s+pre[\s-]?owned)\b"),
    ("open_box", r"\b(?:open[\s-]?box|opened[\s-]?box)\b"),
    ("used", r"\b(?:used|pre[\s-]?owned|second[\s-]?hand)\b"),
    ("new", r"\b(?:brand[\s-]?new|new)\b"),
]

# Walmart's display wording for each bucket, and the only copy of it: these
# strings end up in front of a human reading the sheet, and "used" is not what
# the site calls it.
CONDITION_LABELS = {
    "new": "New",
    "used": "Pre-Owned",
    "refurbished": "Restored",
    "open_box": "Open Box",
}


CONDITION_GRADES = r"premium|like[\s-]?new|very\s+good|excellent|good|fair|acceptable"

# Condition word plus grade. The separator is optional because Walmart writes
# every form of it, and the family half is built from CONDITION_PATTERNS so the
# two cannot drift apart. "new" is left out: a new item has no grade.
CONDITION_DETAIL = re.compile(
    r"(?P<family>{})\s*[:,\u2013\u2014-]?\s*(?P<grade>{})\b".format(
        "|".join(pattern for name, pattern in CONDITION_PATTERNS if name != "new"),
        CONDITION_GRADES,
    ),
    re.I,
)

# A picker grouped under a "Condition" heading labels its options with the
# grade alone, leaving the family word to come from the condition itself.
CONDITION_GRADE_ONLY = re.compile(rf"\s*(?:{CONDITION_GRADES})\s*", re.I)

# Bot walls answer with HTTP 200 and a challenge page, so a non-2xx check is
# not enough — Walmart redirects to /blocked and serves "Robot or human?".
# Match on the page title, which is where every one of these announces itself.
BLOCK_TITLES = re.compile(
    r"robot or human|are you a human|access denied|captcha|"
    r"pardon our interruption|verify you are|attention required|blocked",
    re.I,
)

SCHEMA_CONDITION = {
    "refurbishedcondition": "refurbished",
    "usedcondition": "used",
    "newcondition": "new",
    "damagedcondition": "used",
}

# Availability arrives as schema.org CamelCase ("InStock"), Walmart's SCREAMING
# _SNAKE ("IN_STOCK"), or free text. Normalize to one snake_case vocabulary so
# a CSV column means the same thing across sites. Substring match, longest
# first, because "out_of_stock" contains "stock".
AVAILABILITY_PATTERNS = [
    ("out_of_stock", r"out[\s_-]?of[\s_-]?stock|soldout|sold[\s_-]out|unavailable"),
    ("preorder", r"pre[\s_-]?order"),
    ("backorder", r"back[\s_-]?order"),
    ("discontinued", r"discontinued|retired"),
    ("limited_stock", r"limited[\s_-]?(stock|availability|quantity)|only\s+\d+\s+left"),
    ("in_stock", r"in[\s_-]?stock|instore[\s_-]?only|available"),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return None
    if isinstance(value, dict):
        # schema.org often nests: {"@type": "Brand", "name": "Apple"}
        value = value.get("name") or value.get("value") or value.get("url")
        if value is None:
            return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _to_float(value) -> float | None:
    text = _clean(value)
    if not text:
        return None
    # "$1,299.00" -> 1299.00 ; "1.299,00 €" is not handled, add if you need EU
    match = re.search(r"\d[\d,]*\.?\d*", text.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def _to_int(value) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def detect_condition(*texts: str | None) -> str | None:
    """Infer condition from free text. This is what catches 'Restored Apple
    iPhone 13' on Walmart, where the schema says nothing about condition."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for name, pattern in CONDITION_PATTERNS:
        if re.search(pattern, blob):
            return name
    return None


def reconcile_condition(condition: str | None, *texts: str | None) -> str | None:
    """Settle the bucket between what a page's data says and what its text says.
    """
    from_text = detect_condition(*texts)
    if from_text and from_text != "new" and condition in (None, "new"):
        return from_text
    return condition or from_text


def _grade(text: str) -> str:
    """"like  new" -> "Like New"."""
    return re.sub(r"[\s-]+", " ", text).strip().title()


def detect_condition_detail(*texts: str | None,
                            condition: str | None = None) -> str | None:
    for text in texts:
        if not text:
            continue
        match = CONDITION_DETAIL.search(text)
        if match:
            label = CONDITION_LABELS.get(detect_condition(match.group("family")))
            if label:
                return f"{label}: {_grade(match.group('grade'))}"
        elif CONDITION_GRADE_ONLY.fullmatch(text) and condition != "new":
            label = CONDITION_LABELS.get(condition)
            if label:
                return f"{label}: {_grade(text)}"
    return None


def normalize_availability(value) -> str | None:
    """Collapse "InStock", "IN_STOCK" and "Out of stock" onto one vocabulary."""
    text = _clean(value)
    if not text:
        return None
    text = text.rsplit("/", 1)[-1]  # schema.org ships https://schema.org/InStock
    blob = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", text).lower()
    for name, pattern in AVAILABILITY_PATTERNS:
        if re.search(pattern, blob):
            return name
    return blob or None


def _fill(product: Product, field: str, value, source: str) -> None:
    """Set a field only if it is still empty, and record where it came from."""
    if value is None or getattr(product, field) is not None:
        return
    setattr(product, field, value)
    product.sources[field] = source


# --------------------------------------------------------------------------
# layer 1: JSON-LD
# --------------------------------------------------------------------------

def _iter_jsonld_nodes(soup: BeautifulSoup):
    """Yield every dict in every ld+json block, flattening @graph and lists.
    Real pages nest Product inside @graph, inside arrays, or both."""
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites emit trailing commas or embedded newlines in strings.
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for key in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
                    if key in node:
                        stack.append(node[key])


def _node_types(node: dict) -> set[str]:
    raw = node.get("@type") or node.get("type") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(t).lower() for t in raw}


# A Walmart variant page is a ProductGroup wrapping one fully-described
# Product — the variant the shopper has selected — plus a url-only stub for
# every variant they have not. The group is read as well as the variant
# because the rating covers the whole family and is stated only up there.
PRODUCT_NODE_TYPES = frozenset({"product", "productgroup"})


def from_jsonld(soup: BeautifulSoup, product: Product) -> None:
    for node in _iter_jsonld_nodes(soup):
        if not _node_types(node) & PRODUCT_NODE_TYPES:
            continue

        _fill(product, "title", _clean(node.get("name")), "jsonld")
        _fill(product, "brand", _clean(node.get("brand")), "jsonld")
        _fill(product, "description", _clean(node.get("description")), "jsonld")
        _fill(product, "sku", _clean(node.get("sku")), "jsonld")
        # schema.org spells the manufacturer's number `model`, and that is the
        # one Walmart fills. Without it a variant page has no MPN to offer.
        _fill(product, "mpn", _clean(node.get("mpn") or node.get("model")), "jsonld")
        _fill(product, "gtin", _clean(node.get("gtin13") or node.get("gtin")), "jsonld")
        _fill(product, "upc", _clean(node.get("gtin12") or node.get("upc")), "jsonld")
        _fill(product, "imageLink", _clean(node.get("image")), "jsonld")
        _fill(product, "url", _clean(node.get("url")), "jsonld")

        # offers is a dict, a list, or an AggregateOffer wrapping more offers.
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
            if price is None and isinstance(offers.get("priceSpecification"), dict):
                price = offers["priceSpecification"].get("price")
            _fill(product, "price", _to_float(price), "jsonld")
            _fill(product, "currency", _clean(offers.get("priceCurrency")), "jsonld")

            _fill(product, "availability",
                  normalize_availability(offers.get("availability")), "jsonld")

            condition = _clean(offers.get("itemCondition"))
            if condition:
                key = condition.rsplit("/", 1)[-1].lower()
                _fill(product, "condition", SCHEMA_CONDITION.get(key), "jsonld")

        rating = node.get("aggregateRating")
        if isinstance(rating, dict):
            _fill(product, "rating", _to_float(rating.get("ratingValue")), "jsonld")
            _fill(product, "reviewCount",
                  _to_int(rating.get("reviewCount") or rating.get("ratingCount")), "jsonld")


# --------------------------------------------------------------------------
# layer 2: embedded app state
# --------------------------------------------------------------------------
# Modern retail SPAs hydrate from a JSON blob in the HTML. There is no shared
# standard for where it lives or what it is shaped like, so instead of hard-
# coding a path per site ("props.pageProps.initialData.data.product") we walk
# every nested object and score it on how product-like its keys are. The
# highest scorer wins; lower scorers then fill whatever gaps are left, because
# sites routinely split price, reviews and identifiers across sibling objects.

# Key names seen in the wild, lowercased. Order matters: the first alias
# present on a node wins, so the most specific name goes first.
JSON_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("productname", "producttitle", "name", "title"),
    "brand": ("brandname", "brand", "manufacturername", "manufacturer"),
    "description": ("shortdescription", "productdescription", "description"),
    "sku": ("skuid", "sku"),
    "mpn": ("modelnumber", "manufacturerproductid", "model", "mpn", "partnumber"),
    "gtin": ("gtin13", "gtin14", "gtin", "ean"),
    "upc": ("gtin12", "upc", "upca", "upcnumber"),
    "itemId": ("usitemid", "itemid", "productid", "offerid", "wpsku"),
    "imageLink": ("primaryimageurl", "imageinfo", "thumbnailurl", "largeurl",
                  "imageurl", "image"),
    "availability": ("availabilitystatus", "availability", "stockstatus",
                     "inventorystatus"),
    # Walmart states the grade in its own field rather than in the title, and
    # states it fully formed ("Restored: Like New"). Ahead of "condition" so
    # the dedicated field wins over a grade parsed out of a condition string.
    "conditionDetail": ("preownedcondition", "conditiondetail", "conditiongrade"),
    "condition": ("condition", "itemcondition", "conditiontype"),
}

# Price nests arbitrarily deep — Walmart is priceInfo.currentPrice.price, other
# sites are price.value or offer.amount. Resolved by descending these keys.
PRICE_ALIASES = ("priceinfo", "currentprice", "lineprice", "price",
                 "saleprice", "finalprice")
# Only valid once we are already inside a price wrapper. A bare `value` or
# `amount` anywhere else on the page is not a price, so these are excluded
# from scoring and from the top-level lookup.
PRICE_DESCEND_ALIASES = PRICE_ALIASES + ("amount", "value")
CURRENCY_ALIASES = ("currencyunit", "pricecurrency", "currencycode", "currency")
RATING_ALIASES = ("averagerating", "averageoverallrating", "ratingvalue", "rating")
REVIEW_COUNT_ALIASES = ("numberofreviews", "totalreviewcount", "reviewcount",
                        "ratingcount", "reviewssubmitted")

# An identifier is the strongest signal that an object is *the* product and not
# a breadcrumb, a carousel tile, or an analytics payload.
STRONG_KEYS = frozenset(
    {"usitemid", "upc", "gtin12", "gtin13", "sku", "skuid", "modelnumber",
     "model", "mpn", "manufacturerproductid"}
)
NAME_KEYS = frozenset({"productname", "producttitle", "name", "title"})
PRICE_KEYS = frozenset(PRICE_ALIASES)

# A node needs a name plus real corroboration. Name alone scores 2, which is
# every breadcrumb and category tile on the page.
PRODUCT_MIN_SCORE = 5

# Walmart's state blob is multi-megabyte. These bound the walk so a hostile or
# merely enormous page cannot hang the request.
MAX_NODES = 60_000
MAX_DEPTH = 24
# How many product-like nodes get to contribute, best first. Past a handful it
# is carousel neighbours, and _fill means they can only add wrong values.
MAX_CONTRIBUTORS = 4

# How far the winner must outscore the runner-up describing a *different*
# product before we trust it. A product page's own object beats the "customers
# also viewed" tiles by a wide margin, because tiles carry a name, an id and a
# price and nothing else. A search or category page is a flat list of tiles
# that all score alike — there is no winner there, and guessing produces a row
# that looks real but describes an arbitrary grid item.
DOMINANCE_MARGIN = 2

# Matches `window.__NEXT_DATA__ =`, `self.__PRELOADED_STATE__=`, and the
# bracket form `window["__APOLLO_STATE__"] =`. The object itself is extracted
# by brace matching, not regex — nested braces inside strings break regex.
STATE_ASSIGNMENT = re.compile(
    r"""(?:window|self|globalThis)\s*
        (?:\.\s*|\[\s*["'])
        (__[A-Za-z0-9_]+__|[A-Za-z_][A-Za-z0-9_]*(?:State|Data|Config|Product)\b)
        (?:["']\s*\])?\s*=\s*""",
    re.X,
)


def _balanced_object(text: str, start: int) -> str | None:
    """Return the `{...}` beginning at text[start], respecting string
    literals so a `}` inside a product description does not end it early."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None  # truncated HTML


def _iter_state_blobs(soup: BeautifulSoup):
    """Yield parsed JSON from every embedded blob we can find."""
    for tag in soup.find_all("script"):
        source = tag.string or tag.get_text() or ""
        if not source:
            continue
        mime = (tag.get("type") or "").strip().lower()

        if mime in ("application/json", "text/json"):
            # <script type="application/json" id="__NEXT_DATA__">{...}</script>
            raw = source.strip()
            if raw.startswith(("{", "[")):
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    pass
            continue

        if mime and "javascript" not in mime and mime != "module":
            continue  # ld+json is layer 1's; importmap, templates, etc. are noise

        # <script>window.__PRELOADED_STATE__ = {...};</script>
        for match in STATE_ASSIGNMENT.finditer(source):
            brace = source.find("{", match.end())
            # Bail if the assignment is not followed closely by an object
            # literal — `= JSON.parse(` and `= someVar ||` are not ours.
            if brace == -1 or brace - match.end() > 4:
                continue
            blob = _balanced_object(source, brace)
            if not blob:
                continue
            try:
                yield json.loads(blob)
            except json.JSONDecodeError:
                # Real JS, not JSON: trailing commas, `undefined`, functions.
                continue


def _walk_dicts(root):
    """Depth-first over every dict reachable from root, bounded."""
    stack = [(root, 0)]
    visited = 0
    while stack and visited < MAX_NODES:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if isinstance(node, dict):
            visited += 1
            yield node
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))


def _index(node: dict) -> dict[str, str]:
    """Lowercased key -> original key, so `usItemId` and `usitemid` both hit."""
    return {str(key).lower(): key for key in node}


def _lookup(node: dict, keys: dict[str, str], aliases: tuple[str, ...]):
    for alias in aliases:
        if alias in keys:
            value = node[keys[alias]]
            if value not in (None, "", [], {}):
                return value
    return None


def _score(node: dict, keys: dict[str, str]) -> int:
    if not keys.keys() & NAME_KEYS:
        return 0  # no name, not a product
    score = 2
    score += 3 * len(keys.keys() & STRONG_KEYS)
    if keys.keys() & PRICE_KEYS:
        score += 3
    # Weak corroboration: brand, image, availability, description.
    for field in ("brand", "imageLink", "availability", "description"):
        if any(alias in keys for alias in JSON_ALIASES[field]):
            score += 1
    return score


def _descend_price(value, depth: int = 0) -> tuple[float | None, str | None]:
    """Pull (price, currency) out of a scalar or an arbitrarily nested wrapper."""
    if depth > 4:
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), None
    if isinstance(value, str):
        return _to_float(value), None
    if isinstance(value, list):
        for item in value:
            price, currency = _descend_price(item, depth + 1)
            if price is not None:
                return price, currency
        return None, None
    if isinstance(value, dict):
        keys = _index(value)
        currency = _clean(_lookup(value, keys, CURRENCY_ALIASES))
        for alias in PRICE_DESCEND_ALIASES:
            if alias not in keys:
                continue
            price, nested_currency = _descend_price(value[keys[alias]], depth + 1)
            if price is not None:
                return price, currency or nested_currency
        # Some payloads only carry the formatted string: {"priceString": "$249.00"}
        for alias in ("pricestring", "displayprice", "formattedprice"):
            if alias in keys:
                price = _to_float(value[keys[alias]])
                if price is not None:
                    return price, currency
    return None, None


def _descend_image(value, depth: int = 0) -> str | None:
    if depth > 3:
        return None
    if isinstance(value, str):
        return value if value.startswith(("http", "//", "/")) else None
    if isinstance(value, list):
        for item in value:
            found = _descend_image(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, dict):
        keys = _index(value)
        for alias in ("thumbnailurl", "largeurl", "primaryimageurl", "url",
                      "src", "allimages", "images"):
            if alias in keys:
                found = _descend_image(value[keys[alias]], depth + 1)
                if found:
                    return found
    return None


def _apply_product_node(node: dict, keys: dict[str, str], product: Product) -> None:
    for field, aliases in JSON_ALIASES.items():
        value = _lookup(node, keys, aliases)
        if value is None:
            continue
        if field == "imageLink":
            value = _descend_image(value)
        elif field == "conditionDetail":
            value = detect_condition_detail(_clean(value))
        elif field == "availability":
            value = normalize_availability(value)
        elif field == "condition":
            # Sites write "Refurbished", "Pre-Owned", "NEW" — route through the
            # same matcher the title uses instead of trusting the raw string.
            # The grade, when the field carries one, is kept alongside it.
            _fill(product, "conditionDetail",
                  detect_condition_detail(_clean(value)), "embedded-json")
            value = detect_condition(_clean(value))
        else:
            value = _clean(value)
            # Identifiers must stay strings but arrive as ints in JSON.
            if field in ("gtin", "upc", "itemId", "sku", "mpn") and value:
                value = value.split(".")[0] if re.fullmatch(r"\d+\.0", value) else value
        _fill(product, field, value, "embedded-json")


    price_value = _lookup(node, keys, PRICE_ALIASES)
    if price_value is not None:
        price, currency = _descend_price(price_value)
        _fill(product, "price", price, "embedded-json")
        _fill(product, "currency", currency, "embedded-json")


def _apply_rating(node: dict, keys: dict[str, str], product: Product) -> None:
    _fill(product, "rating", _to_float(_lookup(node, keys, RATING_ALIASES)),
          "embedded-json")
    _fill(product, "reviewCount", _to_int(_lookup(node, keys, REVIEW_COUNT_ALIASES)),
          "embedded-json")


IDENTITY_ALIASES = ("usitemid", "upc", "gtin13", "gtin12", "sku", "skuid",
                    "itemid", "productid")

# Scoring finds objects that are product-shaped; it cannot tell which product
# the *page* is about. On a product page the carousel tiles are just as
# well-described as the product itself, so the tie is broken by the page's own
# self-description: the item id in the URL and the name in og:title. These
# bonuses are deliberately far larger than any score _score can produce, so a
# hint match is decisive rather than merely persuasive.
URL_ID_BONUS = 100
NAME_MATCH_BONUS = 50
# Share of the shorter token set that must overlap to call two names the same
# product. og:title is often a truncation of the full name, so this compares
# against the smaller side rather than the union.
NAME_MATCH_RATIO = 0.6


def _signals(node: dict, keys: dict[str, str]) -> set[str]:
    """The names and ids by which a node claims to be a particular product.

    Compared as sets, not as a single key, because sites routinely split one
    product across sibling objects — identifiers on one, price on another —
    and those two overlap on the name even when only one carries the id.
    """
    found = set()
    for alias in IDENTITY_ALIASES:
        if alias in keys:
            value = _clean(node[keys[alias]])
            if value:
                found.add(f"id:{value}")
    name = _clean(_lookup(node, keys, JSON_ALIASES["title"]))
    if name:
        found.add(f"name:{name.lower()}")
    return found


# Ids by which a node claims to be the page's *subject* rather than merely a
# product it mentions. Deliberately far narrower than IDENTITY_ALIASES: a
# Walmart blob names dozens of item ids across carousels, bundle tiles and ad
# payloads, and any of those would otherwise look like the page's own.
SUBJECT_ID_ALIASES = ("usitemid", "primaryusitemid")
SUBJECT_URL_ALIASES = ("canonicalurl", "canonicalurlwithvariant")


def _subject_ids(node: dict, keys: dict[str, str]) -> set[str]:
    """The item ids a node asserts the surrounding blob was rendered for.

    Only two things count as that assertion: an item id sitting beside a
    product name, and a canonical /ip/ path. Both are things a page says about
    itself; a carousel tile says neither.
    """
    found: set[str] = set()
    for alias in SUBJECT_URL_ALIASES:
        if alias in keys:
            href = _clean(node[keys[alias]])
            match = PATH_ID.search(urlparse(href).path) if href else None
            if match:
                found.add(match.group(1))
    if _lookup(node, keys, JSON_ALIASES["title"]):
        for alias in SUBJECT_ID_ALIASES:
            if alias in keys:
                value = _clean(node[keys[alias]])
                if value:
                    found.add(value)
    return found


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    # "Apple iPad 10.2-inch" -> {apple, ipad, 10, 2, inch}
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _names_match(candidate: str | None, hint: set[str]) -> bool:
    tokens = _tokens(candidate)
    if not tokens or not hint:
        return False
    overlap = len(tokens & hint)
    return overlap / min(len(tokens), len(hint)) >= NAME_MATCH_RATIO


def _page_hints(soup: BeautifulSoup, url: str | None) -> tuple[set[str], set[str]]:
    """What the page says it is about: (item ids, name tokens)."""
    ids: set[str] = set()
    canonical = soup.find("link", rel=lambda r: r and "canonical" in str(r).lower())
    for candidate in (canonical.get("href") if canonical else None, url):
        if not candidate:
            continue
        match = PATH_ID.search(urlparse(candidate).path)
        if match:
            ids.add(match.group(1))

    name = None
    for tag in (soup.find("meta", attrs={"property": "og:title"}),
                soup.find("meta", attrs={"name": "twitter:title"})):
        if tag and tag.get("content"):
            name = tag["content"]
            break
    if not name and soup.title:
        # "Apple iPad ... - Walmart.com" — drop the site suffix, or every
        # candidate name matches on the retailer's own name.
        name = re.split(r"\s+[|–—-]\s+", soup.title.get_text())[0]

    return ids, _tokens(_clean(name))


# Variant pickers mark the shopper's current choice, and on a Restored listing
# that choice is the grade ("Restored - Like New") even when the title says only
# "Restored". Found by shape — a selected flag plus a label — because the path
# to Walmart's variant list is renamed without notice.
SELECTED_KEYS = ("selected", "isselected", "isvariantselected")
VARIANT_LABEL_ALIASES = ("name", "value", "displayname", "label", "variantvalue")


def _selected_variant_text(node: dict, keys: dict[str, str]) -> str | None:
    """The label of a selected variant option, if it speaks about condition.

    Returned in the page's own words: the family word may be missing, and it
    cannot be supplied until the condition itself is settled.
    """
    if not any(node[keys[key]] in (True, "true", "True")
               for key in SELECTED_KEYS if key in keys):
        return None
    for alias in VARIANT_LABEL_ALIASES:
        if alias not in keys:
            continue
        text = _clean(node[keys[alias]])
        if text and (CONDITION_DETAIL.search(text)
                     or CONDITION_GRADE_ONLY.fullmatch(text)):
            return text
    return None


def from_embedded_json(soup: BeautifulSoup, product: Product,
                       url: str | None = None) -> None:
    hint_ids, hint_name = _page_hints(soup, url)
    candidates: list[tuple[int, dict, dict[str, str]]] = []
    rating_nodes: list[tuple[dict, dict[str, str]]] = []
    variant_texts: list[str] = []

    # Accumulated per blob rather than across all of them, because a blob is
    # accepted or rejected whole: whether it describes the item currently on
    # screen is a fact about the blob, not about each node in it.
    for blob in _iter_state_blobs(soup):
        blob_candidates: list[tuple[int, dict, dict[str, str]]] = []
        blob_ratings: list[tuple[dict, dict[str, str]]] = []
        blob_variants: list[str] = []
        subjects: set[str] = set()

        for node in _walk_dicts(blob):
            keys = _index(node)
            subjects |= _subject_ids(node, keys)
            text = _selected_variant_text(node, keys)
            if text:
                blob_variants.append(text)
            score = _score(node, keys)
            if score >= PRODUCT_MIN_SCORE:
                signals = _signals(node, keys)
                if hint_ids and any(s[3:] in hint_ids for s in signals
                                    if s.startswith("id:")):
                    score += URL_ID_BONUS
                if _names_match(_lookup(node, keys, JSON_ALIASES["title"]), hint_name):
                    score += NAME_MATCH_BONUS
                blob_candidates.append((score, node, keys))
            # Ratings usually live on their own object with no product name,
            # so they are collected separately rather than scored out.
            elif any(alias in keys for alias in RATING_ALIASES):
                blob_ratings.append((node, keys))

        # Walmart swaps variants client-side: picking another colour rewrites
        # the URL, the canonical link, og:title and the JSON-LD, but never
        # __NEXT_DATA__, which keeps describing the item the page was first
        # served for. Trusting it then stamps that item's id and barcode onto
        # the row for the one actually on screen — and since the sheet keys on
        # item id, the new variant silently merges into the old variant's row.
        #
        # Only a blob that positively claims a *different* subject is dropped.
        # One that claims no subject at all is the ordinary case for the
        # sibling payloads Walmart ships alongside, and still contributes.
        if hint_ids and subjects and not subjects & hint_ids:
            product.sources["_state_blob"] = (
                f"skipped: describes item {sorted(subjects)[0]}, page is "
                f"{sorted(hint_ids)[0]} (variant switched without a reload)"
            )
            continue

        candidates.extend(blob_candidates)
        rating_nodes.extend(blob_ratings)
        variant_texts.extend(blob_variants)

    for text in variant_texts:
        _fill(product, "conditionDetail", text, "variant-selected")

    if candidates:
        # sort is stable, so equal scores keep document order and the first
        # match on the page wins rather than an arbitrary one.
        candidates.sort(key=lambda item: -item[0])
        top_score, top_node, top_keys = candidates[0]
        winner = _signals(top_node, top_keys)
        others = [(score, node, keys) for score, node, keys in candidates[1:]
                  if not (_signals(node, keys) & winner)]

        if others and top_score - others[0][0] < DOMINANCE_MARGIN:
            # A listing page, not a product page: several unrelated objects are
            # equally product-like. Refuse rather than describe whichever tile
            # happened to sort first.
            product.sources["_embedded_json"] = (
                f"ambiguous: {len(candidates)} product-like objects, top score "
                f"{top_score} vs {others[0][0]} for a different product"
            )
        else:
            # Only the winner and the siblings describing the same product; a
            # neighbouring carousel tile must not fill the gaps it leaves.
            same = [item for item in candidates if _signals(item[1], item[2]) & winner]
            for _, node, keys in same[:MAX_CONTRIBUTORS]:
                _apply_product_node(node, keys, product)
                _apply_rating(node, keys, product)

    for node, keys in rating_nodes:
        if product.rating is not None and product.reviewCount is not None:
            break
        _apply_rating(node, keys, product)


# --------------------------------------------------------------------------
# layer 3: microdata
# --------------------------------------------------------------------------

MICRODATA_FIELDS = {
    "name": "title",
    "brand": "brand",
    "sku": "sku",
    "mpn": "mpn",
    "gtin12": "upc",
    "gtin13": "gtin",
    "price": "price",
    "priceCurrency": "currency",
    "description": "description",
}


def from_microdata(soup: BeautifulSoup, product: Product) -> None:
    for prop, field in MICRODATA_FIELDS.items():
        tag = soup.find(attrs={"itemprop": prop})
        if not tag:
            continue
        # itemprop value lives in content=, datetime=, or the text node.
        value = tag.get("content") or tag.get("datetime") or tag.get_text()
        value = _to_float(value) if field == "price" else _clean(value)
        _fill(product, field, value, "microdata")


# --------------------------------------------------------------------------
# layer 4: OpenGraph / meta
# --------------------------------------------------------------------------

META_FIELDS = {
    "og:title": ("title", _clean),
    "og:image": ("imageLink", _clean),
    "og:url": ("url", _clean),
    "og:description": ("description", _clean),
    "product:price:amount": ("price", _to_float),
    "product:price:currency": ("currency", _clean),
    "product:brand": ("brand", _clean),
    "product:retailer_item_id": ("itemId", _clean),
}


def from_opengraph(soup: BeautifulSoup, product: Product) -> None:
    for key, (field, convert) in META_FIELDS.items():
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content"):
            _fill(product, field, convert(tag["content"]), "opengraph")

    if product.title is None and soup.title:
        _fill(product, "title", _clean(soup.title.get_text()), "title-tag")

    availability = soup.find("meta", attrs={"property": "product:availability"}) or \
        soup.find("meta", attrs={"property": "og:availability"})
    if availability and availability.get("content"):
        _fill(product, "availability",
              normalize_availability(availability["content"]), "opengraph")


# --------------------------------------------------------------------------
# layer 5: the URL itself
# --------------------------------------------------------------------------
# Walmart puts the item id at the end of a product path: /ip/<slug>/20539670270
# (occasionally /ip/20539670270 with no slug). This is often the one field the
# markup omits. Anchoring on /ip/ rather than "trailing digits anywhere" keeps
# a numeric category path from being read as an item id.
PATH_ID = re.compile(r"/ip/(?:[^/]+/)?(\d{4,})/?$")


def from_url_shape(soup: BeautifulSoup, product: Product, url: str | None) -> None:
    # A canonical link is a better `url` than whatever the tab was on, which
    # usually carries tracking params and a session id.
    canonical = soup.find("link", rel=lambda r: r and "canonical" in str(r).lower())
    if canonical and canonical.get("href"):
        href = _clean(canonical["href"])
        if href and href.startswith("http"):
            product.url = href
            product.sources["url"] = "canonical"

    for candidate in (product.url, url):
        if not candidate:
            continue
        path = urlparse(candidate).path
        match = PATH_ID.search(path)
        if match:
            _fill(product, "itemId", match.group(1), "url-path")
            break


# --------------------------------------------------------------------------
# layer 6: CSS selectors
# --------------------------------------------------------------------------
# Reached only when every structured source above came back empty for a field.
# These are rendered-markup hooks and the first thing a Walmart redesign
# breaks, which is why they are last: treat a scrape that depends on them as a
# warning that the layer above it has stopped working.

SELECTORS: dict[str, list[str]] = {
    "price": ['[itemprop="price"]', '[data-seo-id="hero-price"]',
              '[data-testid="price-wrap"] span'],
    "title": ["h1#main-title", "h1[itemprop='name']", "#main-title"],
    # Walmart prints the item id into the page as a hidden span. It is the one
    # identifier that is re-rendered when a variant is picked, so it is right
    # even when the tab's URL still carries the previous variant's slug.
    "itemId": ['[data-testid="us-item-id"]'],
}

# The visible "Condition" card, which states the grade the shopper is buying.
# Walmart puts no test id on it and its classes are utility soup, so it is
# found by its heading and read from the nearest ancestor that also names a
# grade — structure outlives styling. The reviews filter carries the same
# heading and no grade, which is why the grade has to be there to match.
CONDITION_HEADING = re.compile(r"^\s*condition\s*$", re.I)
CONDITION_CARD_LEVELS = 5


def _condition_card_detail(soup: BeautifulSoup) -> str | None:
    for heading in soup.find_all(string=CONDITION_HEADING):
        node = heading.parent
        for _ in range(CONDITION_CARD_LEVELS):
            if node is None:
                break
            match = CONDITION_DETAIL.search(node.get_text(" ", strip=True))
            if match:
                # The smallest enclosing box wins: widen far enough and the
                # "more conditions from other sellers" panel comes with it.
                return match.group(0)
            node = node.parent
    return None


def from_selectors(soup: BeautifulSoup, product: Product, url: str | None) -> None:
    # A URL is not required — the extension can hand over HTML alone — but when
    # there is one, refuse to read Walmart's markup out of some other site's
    # page and label it as Walmart data.
    if url and not urlparse(url).netloc.lower().endswith("walmart.com"):
        return
    for field, selectors in SELECTORS.items():
        for selector in selectors:
            tag = soup.select_one(selector)
            if not tag:
                continue
            text = tag.get("content") or tag.get_text()
            value = _to_float(text) if field == "price" else _clean(text)
            _fill(product, field, value, "selector")
            break

    # Guarded rather than left to _fill: finding the card means walking every
    # string on a multi-megabyte page, and a graded condition is the common
    # case for a page whose state blob was usable.
    if product.conditionDetail is None:
        _fill(product, "conditionDetail", _condition_card_detail(soup), "condition-card")


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def scrape_html(html: str, url: str | None = None) -> Product:
    """Parse a product out of raw HTML. This is the general script — it does
    not care which site the HTML came from or how you obtained it."""
    soup = BeautifulSoup(html, "lxml")
    product = Product(url=url)
    if url:
        product.sources["url"] = "request"

    from_jsonld(soup, product)
    from_embedded_json(soup, product)
    from_microdata(soup, product)
    from_opengraph(soup, product)
    from_url_shape(soup, product, url)
    from_selectors(soup, product, product.url or url)

    condition = reconcile_condition(product.condition, product.title, product.description)

    # conditionDetail may still hold a variant option in the page's own words
    # ("Restored - Like New", or just "Like New"). Canonicalize it here, and
    # fall back to the title, because only now is the bucket that a bare grade
    # needs for its family word settled.
    product.conditionDetail = detect_condition_detail(
        product.conditionDetail, product.title, condition=condition,
    )
    # A grade names its own family, so it can be more specific than anything
    # the title said: "Restored: Very Good" off a variant makes it refurbished.
    condition = reconcile_condition(condition, product.conditionDetail)
    if condition != product.condition:
        product.condition = condition
        product.sources["condition"] = "inferred-from-text"
    if product.conditionDetail:
        product.sources.setdefault("conditionDetail", "inferred-from-text")
    else:
        product.sources.pop("conditionDetail", None)

    return product


class BlockedError(RuntimeError):
    """The site served a bot challenge instead of the product page."""


def _assert_not_blocked(soup: BeautifulSoup, url: str) -> None:
    title = _clean(soup.title.get_text()) if soup.title else ""
    if title and BLOCK_TITLES.search(title):
        raise BlockedError(f"{urlparse(url).netloc} served a bot challenge: {title!r}")


async def scrape_url(url: str, timeout: float = 20.0) -> Product:
    """Fetch and parse a Walmart URL server-side.

    Kept deliberately, though Walmart blocks it in practice: people reach for
    "give it a URL" first, and a named BlockedError pointing at the extension
    is a better answer than a missing endpoint. Raises httpx.HTTPStatusError on
    a non-2xx response, BlockedError on the 200 bot wall.
    """
    async with httpx.AsyncClient(
        headers=BROWSER_HEADERS, follow_redirects=True, timeout=timeout
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    final_url = str(response.url)
    soup = BeautifulSoup(response.text, "lxml")
    _assert_not_blocked(soup, final_url)
    return scrape_html(response.text, url=final_url)
