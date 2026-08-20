import os

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import listing
import storage
from models import Product, ScrapeRequest
from scraper import BlockedError, scrape_html, scrape_url

app = FastAPI(title="Product Scraper")

# A bare extension id is not an origin and will never match the Origin header
# the browser sends, so it has to be prefixed with the scheme. Set
# EXTENSION_IDS to a comma-separated list to lock this down; unset, it accepts
# any unpacked extension, which is what you want while developing since the id
# changes every time you reload from a different directory.
EXTENSION_IDS = [i.strip() for i in os.getenv("EXTENSION_IDS", "").split(",") if i.strip()]
CORS_ORIGINS = (
    {"allow_origins": [f"chrome-extension://{i}" for i in EXTENSION_IDS]}
    if EXTENSION_IDS
    else {"allow_origin_regex": r"chrome-extension://[a-p]{32}"}
)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    **CORS_ORIGINS,
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "products": storage.count()}


@app.post("/api/scrape", response_model=Product)
async def scrape(request: ScrapeRequest):
    """Parse a product from a URL or from HTML.

    Prefer `html` (captured by the extension in a real browser) for sites with
    bot protection; `url` is the convenience path for everything else.
    """
    if not request.url and not request.html:
        raise HTTPException(status_code=400, detail="Provide either 'url' or 'html'.")

    if request.html:
        # Scoring alone cannot always tell a search grid from a product page:
        # one tile carrying a UPC outscores its neighbours and looks dominant.
        # If the page has a grid, this is the wrong endpoint for it.
        if listing.scrape_listing(request.html, request.url):
            raise HTTPException(
                status_code=409,
                detail="This page is a product grid, not one product. Use /api/scrape-page.",
            )
        return scrape_html(request.html, url=request.url)

    try:
        return await scrape_url(request.url)
    except BlockedError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{exc} Load the page in Chrome and use the extension instead.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Site returned {exc.response.status_code}. It likely blocks "
                f"server-side requests — send page HTML instead."
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc


@app.post("/api/save-product")
async def save_product(product: Product):
    """Save an already-parsed product. This is what the extension posts to."""
    try:
        storage.save_product(product)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write CSV: {exc}") from exc
    return {"status": "success", "saved": product.title}


@app.post("/api/scrape-and-save", response_model=Product)
async def scrape_and_save(request: ScrapeRequest):
    product = await scrape(request)

    # Now that the extension runs on any page, the button gets pressed on
    # search results and category pages too. A title alone is just the <title>
    # tag, which every page has — require something only a product page
    # carries, or the CSV fills up with junk rows.
    if product.price is None and product.itemId is None and product.upc is None \
            and product.sku is None and product.gtin is None:
        raise HTTPException(
            status_code=422,
            detail="No product found on this page — got no price and no identifier.",
        )

    await save_product(product)
    return product


@app.post("/api/scrape-page")
async def scrape_page(request: ScrapeRequest, save: bool = True):
    """Scrape whatever page you are on — product page or results grid.

    This is the endpoint the extension calls, because the user should not have
    to tell it which kind of page they are looking at. A search page yields
    every tile; a product page yields the one product, with the full field set
    that only a detail page carries (UPC, description, ratings).
    """
    if not request.url and not request.html:
        raise HTTPException(status_code=400, detail="Provide either 'url' or 'html'.")

    if request.html:
        html, url = request.html, request.url
    else:
        try:
            product = await scrape_url(request.url)
        except BlockedError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc
        return _respond([product], "product", save)

    soup = BeautifulSoup(html, "lxml")
    kind = listing.page_type(url=url, soup=soup)

    # Trust the structure over the URL: /ip/ pages sometimes render a grid
    # (multipack pickers), and search URLs vary per site. Try the grid whenever
    # the URL does not clearly say "product".
    products: list[Product] = []
    if kind != "product":
        products = listing.scrape_listing_soup(soup, url)

    if products:
        return _respond(products, "listing", save)

    single = scrape_html(html, url=url)
    if single.price is None and not (single.itemId or single.upc or single.sku or single.gtin):
        raise HTTPException(
            status_code=422,
            detail=(
                "No products found on this page — no grid, and no price or "
                "identifier for a single product."
            ),
        )
    return _respond([single], "product", save)


def _respond(products: list[Product], kind: str, save: bool) -> dict:
    if save:
        for product in products:
            storage.save_product(product)
    return {"pageType": kind, "count": len(products), "products": products}


@app.get("/api/products")
async def list_products():
    return {"count": storage.count(), "products": storage.load_products()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
