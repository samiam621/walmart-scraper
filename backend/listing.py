"""Multi-product extraction: search, category, brand and deals pages.

`scraper.py` answers "what product is this page about?" and deliberately
refuses when several objects look equally product-like. On a search page that
refusal is the correct answer to the wrong question — there is no single
product, there are forty.

The grid is found by shape rather than by Walmart's current path for it
(props.pageProps.initialData.searchResult.itemStacks[].items), because that
path is renamed without notice and a scraper pinned to it fails silently — it
returns zero results rather than an error. The durable fact is structural: the
results live in a JSON **array** whose elements are product-shaped and shaped
*alike*. So we look for arrays, score their elements with the same scorer the
single-product path uses, and keep the arrays where that holds for most
elements.

The trap is that carousels ("customers also viewed", "sponsored") are arrays of
product-shaped objects too. They are separated by size and by key homogeneity:
a real results grid is larger and its elements share a key signature, because
they came from one API response.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from models import Product
from scraper import (
    PRODUCT_MIN_SCORE,
    _apply_product_node,
    _apply_rating,
    _clean,
    _index,
    _iter_jsonld_nodes,
    _iter_state_blobs,
    _node_types,
    _score,
    _to_float,
    detect_condition_detail,
    reconcile_condition,
)

# How many products the *merged* grid must have before the page counts as a
# listing. Applied to the total, not to each array: Walmart splits results
# across itemStacks, and a trailing stack of three would otherwise be dropped
# before it could be merged.
MIN_GRID_SIZE = 6

# Per-array floor. Deliberately low so a small tail stack survives to the merge
# step; the signature check is what keeps unrelated arrays out, not this.
MIN_ARRAY_SIZE = 2

# Share of an array's elements that must be product-shaped for it to be a grid.
# Not all of them: sponsored slots and ad tiles are interleaved into the same
# array and carry a different shape.
MIN_PRODUCT_RATIO = 0.6

# Two arrays belong to the same logical grid when their elements share a key
# signature. Walmart splits results across several itemStacks that must be
# concatenated, not treated as competing candidates.
#
# Measured as Jaccard (shared / combined), not as a share of the smaller side.
# A carousel tile carries a strict subset of a grid tile's keys — {id, name,
# price} against the grid's dozen — so "share of the smaller side" scores it a
# perfect 1.0 and merges the recommendations strip into the results.
SIGNATURE_OVERLAP = 0.7

# Bound the walk the same way scraper.py does — listing blobs are just as huge.
MAX_ARRAY_NODES = 60_000
MAX_ARRAY_DEPTH = 24

# Walmart's page kinds by path. /ip/ is a product; search, category (/cp/),
# browse, brand and deals pages are grids.
LISTING_PATHS = re.compile(
    r"/(search|browse|shop|cp|deals|brand|store)(/|$)", re.I,
)
PRODUCT_PATHS = re.compile(r"/ip(/|$)", re.I)


def _walk_lists(root):
    """Depth-first over every list of dicts reachable from root, bounded."""
    stack = [(root, 0)]
    visited = 0
    while stack and visited < MAX_ARRAY_NODES:
        node, depth = stack.pop()
        if depth > MAX_ARRAY_DEPTH:
            continue
        visited += 1
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            if sum(isinstance(item, dict) for item in node) >= MIN_ARRAY_SIZE:
                yield node
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))


def _signature(rows: list[dict]) -> frozenset[str]:
    """Keys shared by most elements — the array's structural fingerprint."""
    if not rows:
        return frozenset()
    counts: dict[str, int] = {}
    for row in rows:
        for key in _index(row):
            counts[key] = counts.get(key, 0) + 1
    threshold = len(rows) * 0.5
    return frozenset(key for key, count in counts.items() if count >= threshold)


def _grid_candidates(blob) -> list[tuple[list[dict], frozenset[str]]]:
    """Arrays that look like a results grid, largest first."""
    found = []
    for array in _walk_lists(blob):
        rows = [item for item in array if isinstance(item, dict)]
        scored = [row for row in rows if _score(row, _index(row)) >= PRODUCT_MIN_SCORE]
        if len(scored) < MIN_ARRAY_SIZE:
            continue
        if len(scored) / len(rows) < MIN_PRODUCT_RATIO:
            continue
        found.append((scored, _signature(scored)))
    found.sort(key=lambda item: -len(item[0]))
    return found


def _merge_same_shape(
    candidates: list[tuple[list[dict], frozenset[str]]]
) -> list[dict]:
    """Concatenate every array sharing the largest one's key signature.

    Walmart returns results in multiple itemStacks — one grid split across
    several arrays, which would otherwise lose everything but the biggest
    chunk.
    """
    if not candidates:
        return []
    rows, primary = candidates[0]
    merged = list(rows)
    for other_rows, signature in candidates[1:]:
        if not primary or not signature:
            continue
        overlap = len(primary & signature) / len(primary | signature)
        if overlap >= SIGNATURE_OVERLAP:
            merged.extend(other_rows)
    return merged


def _row_to_product(row: dict, base_url: str | None) -> Product:
    keys = _index(row)
    product = Product()
    _apply_product_node(row, keys, product)
    _apply_rating(row, keys, product)

    # Tiles carry their own link, which is how a listing row becomes a URL you
    # can revisit. It is usually relative.
    for alias in ("canonicalurl", "productpageurl", "producturl", "itemurl",
                  "seodescription", "url", "href", "link"):
        if alias not in keys:
            continue
        href = _clean(row[keys[alias]])
        if href and ("/" in href):
            product.url = urljoin(base_url, href) if base_url else href
            product.sources["url"] = "listing-row"
            break

    condition = reconcile_condition(product.condition, product.title, product.description)
    if condition != product.condition:
        product.condition = condition
        product.sources["condition"] = "inferred-from-text"

    if product.conditionDetail is None:
        # Tiles put the whole thing in the title: "Restored Apple iPhone 13 -
        # Like New". There is no variant picker on a grid to read instead.
        detail = detect_condition_detail(product.title, condition=product.condition)
        if detail:
            product.conditionDetail = detail
            product.sources["conditionDetail"] = "inferred-from-text"

    return product


def _from_jsonld_itemlist(soup: BeautifulSoup, base_url: str | None) -> list[Product]:
    """Some listing pages publish a proper schema.org ItemList."""
    products: list[Product] = []
    for node in _iter_jsonld_nodes(soup):
        if "itemlist" not in _node_types(node):
            continue
        for element in node.get("itemListElement") or []:
            if not isinstance(element, dict):
                continue
            item = element.get("item") if isinstance(element.get("item"), dict) else element
            if "product" not in _node_types(item):
                continue
            product = _row_to_product(item, base_url)
            # JSON-LD spells these the schema.org way, which JSON_ALIASES
            # already covers except for offers.
            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and product.price is None:
                product.price = _to_float(offers.get("price"))
                product.currency = _clean(offers.get("priceCurrency"))
                product.sources["price"] = "jsonld-itemlist"
            if product.title:
                products.append(product)
    return products


def _identity(product: Product) -> str | None:
    for value in (product.itemId, product.upc, product.gtin, product.sku):
        if value:
            return str(value)
    return product.title.lower() if product.title else None


def dedupe(products: list[Product]) -> list[Product]:
    """Grids repeat items across stacks, and sponsored slots duplicate organic
    ones. Keep the first occurrence, which is the highest-placed."""
    seen: set[str] = set()
    unique = []
    for product in products:
        key = _identity(product)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(product)
    return unique


def url_page_type(url: str | None) -> str:
    """'product', 'listing' or 'unknown' from the URL alone."""
    if not url:
        return "unknown"
    parsed = urlparse(url)
    if PRODUCT_PATHS.search(parsed.path):
        return "product"
    if LISTING_PATHS.search(parsed.path) or parsed.query.startswith("q=") \
            or "&q=" in parsed.query:
        return "listing"
    return "unknown"


def page_type(html: str | None = None, url: str | None = None,
              soup: BeautifulSoup | None = None) -> str:
    """'product', 'listing', or 'unknown'. URL shape is checked first because
    it is cheap and unambiguous when it matches; otherwise fall back to asking
    whether the page actually contains a grid."""
    kind = url_page_type(url)
    if kind != "unknown":
        return kind
    if soup is None:
        soup = BeautifulSoup(html or "", "lxml")
    return "listing" if scrape_listing_soup(soup, url, limit=MIN_GRID_SIZE) else "unknown"


def scrape_listing_soup(soup: BeautifulSoup, url: str | None = None,
                        limit: int | None = None,
                        force: bool = False) -> list[Product]:
    """Extract the results grid, or [] if the page does not have one.

    A product page's "similar items" carousel is structurally indistinguishable
    from a search grid — same keys, same size range, same JSON blob — so no
    amount of scoring separates them. The URL does: /ip/ is a product page, and
    its arrays are recommendations no matter how grid-shaped they look. Pass
    force=True to extract them anyway.
    """
    if not force and url_page_type(url) == "product":
        return []

    products = _from_jsonld_itemlist(soup, url)

    if len(products) < MIN_GRID_SIZE:
        candidates: list[tuple[list[dict], frozenset[str]]] = []
        for blob in _iter_state_blobs(soup):
            candidates.extend(_grid_candidates(blob))
        candidates.sort(key=lambda item: -len(item[0]))
        for row in _merge_same_shape(candidates):
            product = _row_to_product(row, url)
            if product.title:
                products.append(product)

    products = dedupe(products)
    # A row with neither price nor identifier is a filter chip or a banner that
    # happened to carry a name, not a result.
    products = [
        p for p in products
        if p.price is not None or p.itemId or p.upc or p.sku or p.gtin
    ]

    # The size floor belongs here, on the merged and deduped total. A handful
    # of products is a recommendations strip on a product page, not a grid —
    # and calling that a listing would save carousel neighbours as results.
    if len(products) < MIN_GRID_SIZE:
        return []

    return products[:limit] if limit else products


def scrape_listing(html: str, url: str | None = None,
                   force: bool = False) -> list[Product]:
    """Extract every product from a search / category / browse page."""
    return scrape_listing_soup(BeautifulSoup(html, "lxml"), url, force=force)
