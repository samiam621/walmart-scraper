from pydantic import BaseModel, Field


class Product(BaseModel):
    """Normalized product record. Every field is optional because no single
    source (JSON-LD / OpenGraph / selectors) fills all of them."""

    title: str | None = None
    brand: str | None = None
    price: float | None = None
    currency: str | None = None
    condition: str | None = None  # new | refurbished | used | open_box
    # The graded form of the same thing, as a shopper sees it: "Restored: Like
    # New", "Pre-Owned: Good". Kept beside `condition` rather than folded into
    # it so that bucket stays a fixed vocabulary. None when the page grades
    # nothing, which is most of them.
    conditionDetail: str | None = None
    availability: str | None = None
    sku: str | None = None
    itemId: str | None = None
    # Barcodes are strings, not ints: they have leading zeros and overflow
    # readability as numbers. "012345678905" != 12345678905.
    gtin: str | None = None
    upc: str | None = None
    mpn: str | None = None
    imageLink: str | None = None
    url: str | None = None
    description: str | None = None
    rating: float | None = None
    reviewCount: int | None = None
    # English translation product title
    titleEn: str | None = None

    # Where each field came from, for debugging a bad scrape.
    sources: dict[str, str] = Field(default_factory=dict)


class ScrapeRequest(BaseModel):
    url: str | None = None
    html: str | None = None
