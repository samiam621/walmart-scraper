import asyncio
import os
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import export  # noqa: E402 - must follow load_dotenv
import listing  # noqa: E402
import sheets  # noqa: E402
import storage  # noqa: E402
import translate  # noqa: E402
from models import Product, ScrapeRequest
from pydantic import BaseModel

from scraper import BlockedError, scrape_html, scrape_url

app = FastAPI(title="Product Scraper")


EXTENSION_IDS = [i.strip() for i in os.getenv("EXTENSION_IDS", "").split(",") if i.strip()]
CORS_ORIGINS = (
    {"allow_origins": [f"chrome-extension://{i}" for i in EXTENSION_IDS]}
    if EXTENSION_IDS
    else {"allow_origin_regex": r"chrome-extension://[a-p]{32}"}
)


AUTO_EXPORT_SHEETS = os.getenv("AUTO_EXPORT_SHEETS", "true").strip().lower() not in (
    "0", "false", "no", "off", ""
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
        return await _respond([product], "product", save, request.url)

    soup = BeautifulSoup(html, "lxml")
    kind = listing.page_type(url=url, soup=soup)

    # Trust the structure over the URL: /ip/ pages sometimes render a grid
    # (multipack pickers), and search URLs vary per site. Try the grid whenever
    # the URL does not clearly say "product".
    products: list[Product] = []
    if kind != "product":
        products = listing.scrape_listing_soup(soup, url)

    if products:
        return await _respond(products, "listing", save, url)

    single = scrape_html(html, url=url)
    if single.price is None and not (single.itemId or single.upc or single.sku or single.gtin):
        raise HTTPException(
            status_code=422,
            detail=(
                "No products found on this page — no grid, and no price or "
                "identifier for a single product."
            ),
        )
    return await _respond([single], "product", save, url)


async def _respond(products: list[Product], kind: str, save: bool, page_url: str | None) -> dict:
    translation = await _translate_titles(products)

    if save:
        for product in products:
            storage.save_product(product)

    payload = {"pageType": kind, "count": len(products), "products": products}
    if translation:
        payload["translation"] = translation
    if save and AUTO_EXPORT_SHEETS:
        payload["export"] = await _push_to_sheets(products, page_url)
    return payload


async def _translate_titles(products: list[Product]) -> dict | None:
    """Best-effort title translation for non-English storefronts (e.g.
    walmart.com.mx). Mirrors _push_to_sheets: a translation failure must
    never fail the scrape, but it must not be silent either — an untranslated
    title reaches the sheet as a blank cell, which is indistinguishable from
    a product that never needed translating. None when this page had nothing
    to translate, so an ordinary walmart.com scrape says nothing about it.
    """
    try:
        # deep_translator is blocking, and this runs inside an async
        # endpoint; without the thread it would stall every other request.
        report = await asyncio.to_thread(translate.annotate_all, products)
    except Exception as exc:  # noqa: BLE001 - see docstring
        return {
            "ok": False,
            "translated": 0,
            "failed": 0,
            "skipped": 0,
            "reason": f"Translation failed: {exc}",
        }

    if not (report["translated"] or report["failed"] or report["skipped"]):
        return None
    return report


async def _push_to_sheets(products: list[Product], page_url: str | None) -> dict:
    """Best-effort export. Never raises — a scrape that parsed correctly is
    still a success even if Google is unreachable, and the rows stay in the
    CSV for the next push to pick up.

    page_url routes grid tiles that arrived without a URL of their own to the
    sheet for the storefront they were actually scraped from."""
    try:
        # gspread is blocking, and this runs inside an async endpoint; without
        # the thread it would stall every other request for the round trip.
        result = await asyncio.to_thread(sheets.push, products, "append", page_url)
    except sheets.SheetsError as exc:
        return {"ok": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - see docstring
        return {"ok": False, "reason": f"Sheets push failed: {exc}"}

    return {
        "ok": True,
        "rowsWritten": result["rowsWritten"],
        "rowsUpdated": result["rowsUpdated"],
        "skipped": result["skipped"],
        "url": result["url"],
        # Which sheets this page landed in, and any country whose sheet id is
        # unset — an unconfigured storefront must not fail silently.
        "sheets": result["sheets"],
        "errors": result["errors"],
    }


class SheetsExportRequest(BaseModel):
    # "append" adds what the sheet is missing; "replace" rewrites the tab.
    mode: str = "append"
    # Optional: export only these item ids. The extension sends the ids it
    # just scraped so one click pushes that page, not the whole backlog.
    itemIds: list[str] | None = None


@app.get("/api/export/preview")
async def export_preview(limit: int = 50):
    """The rows exactly as they would land in the sheet. Check the mapping
    here before pushing anything to Google."""
    rows = export.rows_as_dicts(storage.load_products())
    return {"columns": export.COLUMNS, "count": len(rows), "rows": rows[:limit]}


@app.post("/api/export/sheets")
async def export_to_sheets(request: SheetsExportRequest):
    """Push saved products into the configured Google Sheet."""
    if request.mode not in ("append", "replace"):
        raise HTTPException(
            status_code=400, detail=f"Unknown mode {request.mode!r}; use 'append' or 'replace'."
        )

    products = storage.load_products()

    if request.itemIds:
        wanted = {str(i).strip() for i in request.itemIds if str(i).strip()}
        products = [p for p in products if str(p.get("itemId", "")).strip() in wanted]
        if not products:
            raise HTTPException(
                status_code=404,
                detail="None of those item ids are saved. Scrape the page first.",
            )

    if not products:
        raise HTTPException(status_code=404, detail="Nothing saved yet — scrape a page first.")

    try:
        # No page_url: the CSV mixes storefronts, so each record routes on its
        # own URL rather than inheriting one page's.
        return sheets.push(products, mode=request.mode)
    except sheets.SheetsError as exc:
        # 503, not 500: the code is fine, the credentials or sharing are not.
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/products")
async def list_products():
    return {"count": storage.count(), "products": storage.load_products()}


if __name__ == "__main__":
    import uvicorn

    # 127.0.0.1 locally so a dev server is not exposed to the network, but a
    # PaaS routes to the container from outside and health-checks it, so a
    # loopback bind there just fails to answer. PORT is assigned per deploy,
    # never hardcoded.
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    uvicorn.run(app, host=host, port=port)
