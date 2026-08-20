# Walmart Scraper

Scrapes any Walmart page — a product page or a whole search grid — from the tab
you are already looking at. It reads the structured data Walmart publishes
rather than CSS selectors, so a redesign degrades a scrape instead of ending it.

## Run

```bash
.venv/bin/python -m uvicorn app:app --reload --port 8000 --app-dir backend
```

Interactive docs: http://127.0.0.1:8000/docs

## How the parsing works

Walmart publishes the same product through several formats at once, and which
ones are populated varies by page and over time. `backend/scraper.py` reads all
of them, in priority order; each fills only the fields still empty, so a weak
layer never overwrites a strong one.

| Layer | Source | Carries |
|---|---|---|
| 1 | JSON-LD `schema.org/Product` | title, brand, gtin, rating |
| 2 | Embedded app state (`__NEXT_DATA__`) | **most of it** — price, UPC, item id |
| 3 | Microdata (`itemprop`) | scattered leftovers |
| 4 | OpenGraph `<meta>` | title, image — shallow but near-always present |
| 5 | URL shape (canonical link, `/ip/<slug>/<id>`) | the item id when markup omits it |
| 6 | CSS selectors | last resort, first thing a redesign breaks |

These are all Walmart sources. Layers 1, 3 and 4 are open standards Walmart
implements — they are not leftovers from supporting other retailers, and they
are what keeps a scrape partially working when layer 2 shifts.

Layer 2 carries the most: Walmart exposes only a shallow OpenGraph summary, and
price, UPC and item id live in the hydration blob. It is located by *shape* —
every nested object is scored on how product-like its keys are — rather than by
`props.pageProps.initialData.data.product`, which Walmart renames without
notice. A scraper pinned to that path fails silently, returning zero results
rather than an error.

Every parsed field records its origin in `product.sources`, so when a scrape
comes back wrong you can see which layer produced the bad value.

Condition (`new` / `refurbished` / `used` / `open_box`) comes from
`schema.org/itemCondition` when present, otherwise it is inferred from the
title and description — this is what catches Walmart's "Restored" listings,
which carry no condition field at all.

## Use the extension, not a URL

Walmart blocks server-side fetches. A request to a product URL redirects to
`/blocked` and returns a "Robot or human?" page with **HTTP 200** — so a status
check passes and you save the challenge page as a product. `scrape_url` detects
this by title and raises `BlockedError` instead.

Passing a `url` is still supported, because people try that first and a clear
error beats a missing endpoint. But it will not work against Walmart.

What works is the Chrome extension in `chromefrontend/`: it reads
`document.documentElement.outerHTML` from the tab you are already viewing — a
real browser, with your session — and POSTs it to `/api/scrape-page`.

Load it via `chrome://extensions` → Developer mode → Load unpacked →
select `chromefrontend/`.

## Any page, not just product pages

`/api/scrape-page` detects what kind of page it was given and routes itself, so
the extension works the same on a product page and on a search for
"refurbished iphone" — which captures all 40 results in one click.

```json
{"pageType": "listing", "count": 8, "products": [ ... ]}
{"pageType": "product", "count": 1, "products": [ ... ]}
```

Grids are found structurally, not at Walmart's current JSON path for them.
`listing.py` looks for **arrays whose elements are product-shaped and shaped
alike**, scoring elements with the same scorer the single-product path uses.
Walmart splits results across several `itemStacks`, so arrays sharing a key
signature are merged (Jaccard ≥ 0.7 — a plain overlap ratio scores a carousel's
subset of keys a perfect 1.0 and swallows the recommendations strip).

One thing scoring genuinely cannot do is tell a product page's "similar items"
carousel from a search grid — same keys, same size, same blob. The URL decides:
`/ip/` is a product page and its arrays are recommendations. Pass `force=True`
to `scrape_listing` to extract them anyway.

Add `?save=false` to preview without writing to the CSV.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + row count |
| POST | `/api/scrape-page` | **any page** — auto-detects product vs. grid (what the extension calls) |
| POST | `/api/scrape` | one product; 409s if handed a grid |
| POST | `/api/save-product` | append an already-parsed product |
| POST | `/api/scrape-and-save` | single product, parse + save |
| GET | `/api/products` | everything saved so far |

Rows land in `backend/data/products.csv`.

## When a field comes back empty

Check `sources` on the response first — it names the layer that produced every
value, so you can see which one stopped working rather than guessing.

If a field is missing everywhere, add a selector to `SELECTORS` in
`backend/scraper.py` as a stopgap:

```python
SELECTORS = {
    "price": ['[itemprop="price"]', '[data-seo-id="hero-price"]'],
}
```

Treat that as a warning, not a fix: a scrape that depends on layer 6 means a
structured source above it has changed shape, and the selector will break at
the next redesign. The durable fix is usually a new alias in `JSON_ALIASES`.

## Notes

- Walmart's Terms of Service restrict automated collection. Keep this to pages
  you visit yourself, at human rates.
- CORS is restricted to `chrome-extension://` origins. Set `EXTENSION_IDS` to
  pin it to your own extension before running this anywhere but localhost.
