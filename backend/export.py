"""Maps scraped products onto the flat sheet layout.

The sheet is not a dump of the CSV. Four columns are derived rather than
copied, because the value a listing tool needs is not the value the page
carries:

  GTIN         a UPC-A *is* a GTIN, just left-unpadded. Feeds expect 14
               digits, so "469139796107" has to become "00469139796107".
  Listing URL  the scraped URL is an SEO slug with tracking query params that
               rot; /ip/{itemId} is the stable address for the same item.
  Image URL    Walmart serves whatever size the page asked for. Pinning the
               odn* params gets the 2000px original instead of a thumbnail.
  Condition    see resolve_condition() — Walmart's JSON-LD lies about this,
               and a grade ("Restored: Like New") outranks the bare bucket.

Everything here works on plain dicts as well as Product objects, because the
CSV round-trip hands back dicts of strings and both paths feed the same sheet.
"""

import re
from urllib.parse import urlparse

# Column order follows example.xlsx, with Other Identifier inserted after the
# GTIN it stands in for.
COLUMNS = [
    "Product Title",
    "Item Condition",
    "Image URL",
    "Item ID",
    "GTIN",
    "Other Identifier",
    "Listing URL",
    "English Title",
]

# Item ids are not all numbers. walmart.com uses a numeric id; walmart.ca uses
# an alphanumeric token ("5O9UZWKNZ4LC"). Stripping one of those to its digits
# invents a different item ("594"), so a number is only pulled out of a value
# that is a number wearing a label ("Item #20539670270", "20539670270.0").
_NUMERIC_ITEM_ID = re.compile(r"\A\D*(\d+)(?:\.0)?\D*\Z")
_BARE_ITEM_ID = re.compile(r"\A[A-Za-z0-9]{4,}\Z")

# Anchored at the end of the *path* so a slug segment cannot pose as an id:
# "/ip/Refurbished-Apple-iPhone" has no id, and the hyphens are what say so.
_ITEM_ID_IN_PATH = re.compile(r"/ip/(?:[^/]+/)?([A-Za-z0-9]{4,})/?\Z")

# The size Walmart's own "view larger" uses. odnBg matters: without it
# transparent PNGs composite onto black in some feed importers.
FULL_SIZE_PARAMS = "odnHeight=2000&odnWidth=2000&odnBg=FFFFFF"


def _get(record, field: str) -> str | None:
    """Read a field from a Product or a CSV dict, normalizing '' to None."""
    value = getattr(record, field, None) if not isinstance(record, dict) else record.get(field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def _item_id(value: str | None) -> str | None:
    """Normalize an item id without assuming it is a number."""
    if not value:
        return None
    text = value.strip()

    numeric = _NUMERIC_ITEM_ID.match(text)
    if numeric:
        return numeric.group(1)
    # A .ca token, kept verbatim — its letters carry meaning and its case is
    # the case the storefront uses.
    if _BARE_ITEM_ID.match(text):
        return text
    # Anything else is a mis-parse. A row with no id still gets its scraped
    # URL, which beats a confidently wrong link.
    return None


def to_gtin14(*candidates: str | None) -> str | None:
    """Normalize the first usable barcode to a 14-digit GTIN.

    UPC-A (12), EAN-13 and GTIN-14 are the same number space at different
    widths, so zero-padding is the whole conversion. Anything longer than 14
    digits is not a barcode — usually a Walmart item id that landed in the
    wrong column — and is dropped rather than truncated into a wrong one.
    """
    for candidate in candidates:
        digits = _digits(candidate)
        if digits and 8 <= len(digits) <= 14:
            return digits.zfill(14)
    return None


def resolve_condition(record) -> str | None:
    """Work out what condition the item is actually in.

    The graded form wins when there is one, so "Restored: Like New" reaches the
    sheet rather than the bucket it normalizes to.

    Both the bucket and the grade are re-derived from the record rather than
    trusted as stored: a row read back from the CSV may have been written
    before either rule existed. See reconcile_condition() for why a title
    outranks the schema.
    """
    # Local import: export.py is usable without the scraper's dependencies.
    from scraper import CONDITION_LABELS, detect_condition_detail, reconcile_condition

    title = _get(record, "title")
    condition = reconcile_condition(_get(record, "condition"), title)

    detail = detect_condition_detail(_get(record, "conditionDetail"), title,
                                     condition=condition)
    if detail:
        return detail

    if condition is None:
        return None
    return CONDITION_LABELS.get(condition, condition.replace("_", " ").title())


def resolve_item_id(record) -> str | None:
    """The Walmart item id, read off the URL when the field itself is empty.

    Grid tiles routinely parse without an explicit id while still linking to
    /ip/<id>, and a row with no id cannot be deduplicated or linked.
    """
    item_id = _item_id(_get(record, "itemId"))
    if item_id:
        return item_id

    url = _get(record, "url")
    if url:
        match = _ITEM_ID_IN_PATH.search(urlparse(url).path)
        if match:
            return match.group(1)
    return None


# domain -> (origin, default /ip prefix). walmart.ca serves every product page
# under a locale segment and canonicalizes to it ("/en/ip/<slug>/<id>"), so the
# bare "/ip/<id>" that works on .com is not the .ca address for the same item.
_WALMART_STOREFRONTS = {
    "walmart.ca": ("https://www.walmart.ca", "/en/ip"),
    "walmart.com": ("https://www.walmart.com", "/ip"),
    "walmart.com.mx": ("https://www.walmart.com.mx", "/ip"),
}

_LOCALE_IN_PATH = re.compile(r"\A/(en|fr)(?:/|\Z)")


def _listing_base(record) -> str:
    """Rebuild the canonical link against the storefront the product was
    actually scraped from — a .ca item must not get a walmart.com link."""
    parts = urlparse(_get(record, "url") or "")
    # Strip any userinfo and port so "www.walmart.ca:443" still matches.
    host = parts.netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]

    # No usable URL (grid tiles sometimes arrive without one) — .com is the
    # storefront the scraper sees most.
    origin, prefix = _WALMART_STOREFRONTS["walmart.com"]
    for domain, storefront in _WALMART_STOREFRONTS.items():
        if host == domain or host.endswith("." + domain):
            origin, prefix = storefront
            break

    # Keep the locale the shopper was actually on; the table's is a fallback.
    locale = _LOCALE_IN_PATH.match(parts.path)
    if locale:
        prefix = f"/{locale.group(1)}/ip"
    return origin + prefix


def canonical_listing_url(record) -> str | None:
    """The stable /ip/{itemId} address, falling back to the scraped URL."""
    url = _get(record, "url")
    item_id = resolve_item_id(record)

    if item_id:
        return f"{_listing_base(record)}/{item_id}"
    return url


def full_size_image(record) -> str | None:
    """Ask Walmart's image CDN for the original rather than a page thumbnail."""
    url = _get(record, "imageLink")
    if not url or "walmartimages.com" not in url:
        return url

    base = url.split("?", 1)[0]
    return f"{base}?{FULL_SIZE_PARAMS}"


def other_identifier(record, gtin: str | None) -> str | None:
    """A backup identifier, typed so the reader knows what they are holding.

    Two cases, both worth filling. If no GTIN could be built, this is the only
    identifier the row has. If one could, this still carries the manufacturer
    number, which is what matches a listing to a catalogue entry when the
    barcode does not.
    """
    upc = _digits(_get(record, "upc"))
    # Anything outside barcode width is a mis-parse (usually an item id that
    # landed in the upc field), and it was already rejected from the GTIN
    # column. Do not launder it into this one.
    if upc and not 8 <= len(upc) <= 14:
        upc = None
    # Skip the UPC when it is the very number already padded into the GTIN
    # column — repeating it teaches the reader nothing.
    if upc and (gtin is None or gtin.lstrip("0") != upc.lstrip("0")):
        return f"UPC:{upc}"

    for field, prefix in (("mpn", "MPN"), ("sku", "SKU")):
        value = _get(record, field)
        if value:
            return f"{prefix}:{value}"
    return None


def _sheet_item_id(item_id: str | None) -> int | str:
    if not item_id:
        return ""
    # Cast to a number only when there is no leading zero to lose — true of
    # every walmart.com id observed (e.g. "20539670270"). walmart.com.mx ids
    # can be 14-digit, zero-padded GTIN-style values ("00085369895438"); a
    # bare int() cast there silently drops the leading zeros.
    if item_id.isdigit() and not item_id.startswith("0"):
        return int(item_id)
    return item_id


def product_to_row(record) -> list:
    """One product -> one sheet row, in COLUMNS order."""
    gtin = to_gtin14(_get(record, "gtin"), _get(record, "upc"))
    item_id = resolve_item_id(record)

    return [
        _get(record, "title") or "",
        resolve_condition(record) or "",
        full_size_image(record) or "",
        # A numeric (.com) id is sent as an int so the sheet stores it as a
        # number, matching the example. A .ca id is alphanumeric and stays a
        # string. GTIN stays a string too: its leading zeros are significant.
        _sheet_item_id(item_id),
        gtin or "",
        other_identifier(record, gtin) or "",
        canonical_listing_url(record) or "",
        _get(record, "titleEn") or "",
    ]


def build_rows(records, *, header: bool = True, dedupe: bool = True) -> list[list]:
    """Products -> sheet rows, newest scrape order preserved.

    Deduplicates on item id because re-scraping a page you already saved is
    a common way to use the extension, and the CSV keeps every append.
    """
    rows: list[list] = [list(COLUMNS)] if header else []
    seen: set[str] = set()

    for record in records:
        row = product_to_row(record)
        if dedupe:
            # Fall back to the title for items with no id at all, so two
            # different unidentified products still get two rows.
            key = str(row[3]) or row[4] or row[0]
            if key and key in seen:
                continue
            seen.add(key)
        rows.append(row)
    return rows


def rows_as_dicts(records) -> list[dict]:
    """Same rows, keyed by column name — for previewing over the API."""
    return [dict(zip(COLUMNS, row)) for row in build_rows(records, header=False)]
