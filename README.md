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
| GET | `/api/export/preview` | the sheet rows, before pushing them anywhere |
| POST | `/api/export/sheets` | push rows into the Google Sheet |

Rows land in `backend/data/products.csv`.

## Google Sheets export

The CSV keeps every field the scraper found. The sheet keeps the seven a
listing tool actually needs, in `example.xlsx` order:

| Product Title | Item Condition | Image URL | Item ID | GTIN | Other Identifier | Listing URL |
|---|---|---|---|---|---|---|

Four of those are derived rather than copied — see the module docstring in
[`backend/export.py`](backend/export.py). The short version:

- **GTIN** is padded to 14 digits, because a UPC-A is a GTIN with the zeros
  left off: `469139796107` goes up as `00469139796107`.
- **Other Identifier** carries `UPC:`, `MPN:` or `SKU:` — a typed fallback for
  rows where no GTIN could be built, and the manufacturer number where one
  could.
- **Listing URL** is canonicalized to `/ip/{itemId}`; SEO slugs and tracking
  params change under you.
- **Image URL** is pinned to the 2000px original rather than a page thumbnail.

Preview the mapping any time, without touching Google:

```bash
curl -s localhost:8000/api/export/preview | python3 -m json.tool
```

### One-time setup

A service account, not OAuth — the backend runs headless and an OAuth refresh
needs a human at a browser.

1. In the [Google Cloud console](https://console.cloud.google.com/), create (or
   pick) a project and enable the **Google Sheets API**.
2. **IAM & Admin → Service Accounts → Create service account.** No roles are
   needed; project-level roles do not grant access to your files anyway.
3. On the account's **Keys** tab: **Add key → Create new key → JSON**. Save the
   download somewhere outside this repo.
4. Open the JSON and copy `client_email` — it looks like
   `something@project-id.iam.gserviceaccount.com`.
5. Create the spreadsheet and **Share it with that address as Editor.** This is
   the step everyone misses: a service account is its own principal and owns
   nothing until you share with it.
6. Copy the sheet id out of the URL — the part between `/d/` and `/edit`.

Then point the backend at both, in a `.env` at the repo root:

```
GOOGLE_SERVICE_ACCOUNT_FILE=/Users/you/.config/walmart-scraper/service-account.json
GOOGLE_SHEET_ID=1AbC...xyz
GOOGLE_SHEET_TAB=Sheet1
```

`app.py` loads that file at import, before anything reads `os.getenv`. Use an
**absolute path** for the key: uvicorn resolves relative paths against the
directory you launched it from, which is rarely the one you meant. `~` is
expanded, but `$VARS` are not. Real environment variables, if already set,
take precedence over the file.

Keep the key outside the repo and `chmod 600` it. It is a live credential to
every file you shared with that service account, and it is not password
protected — anyone who reads the file has that access.

### Pushing

Click **Send to Google Sheets** in the extension after a scrape, or:

```bash
curl -X POST localhost:8000/api/export/sheets -H 'Content-Type: application/json' -d '{"mode":"append"}'
```

With `AUTO_EXPORT_SHEETS` on — the default — you rarely need either: each
scrape pushes itself, and the popup reports `3 to Sheets` or `1 dup` next to
the save message.

`append` adds only products whose Item ID is not already in the sheet, so
re-running it is safe. `mode: "replace"` clears the tab and rewrites it.
`itemIds: [...]` limits the push to specific products — the extension sends the
ids it just scraped, so the button uploads that page rather than the backlog.

Values are written with `value_input_option="RAW"`. The default would parse
each cell as if typed, which strips the leading zeros off a GTIN.

## Deploying to Render

`render.yaml` is a blueprint — create the service with **New > Blueprint**
rather than assembling it by hand, so the disk and env vars come with it.

Three things differ from running locally, and all three are silent failures if
you skip them:

**The key cannot be a path from your laptop.** `/Users/you/...` does not exist
on the server. Either paste the key into `GOOGLE_SERVICE_ACCOUNT_JSON`, or add
it as a Render **Secret File** and set `GOOGLE_SERVICE_ACCOUNT_FILE` to
`/etc/secrets/<filename>`. For the env-var route, prefer base64 — dashboard
fields reformat the `\n` escapes in `private_key`, and a mangled key fails at
signing time with an opaque padding error rather than at startup:

```bash
base64 -i ~/.config/walmart-scraper/service-account.json | pbcopy
```

**Bind `0.0.0.0` on `$PORT`.** The start command does this. A loopback bind
passes locally and then fails every health check on Render, because the router
reaches the container from outside.

**The filesystem is wiped on every deploy** — and on every spin-down after
idle, which on a free instance happens constantly. `products.csv` does not
survive either, so the CSV cannot be the record of what you scraped.

`AUTO_EXPORT_SHEETS=true` (the default) is the answer: every scrape is pushed
to the sheet on the way through, so the sheet is the durable copy and the CSV
is just a local cache. The push is best-effort — if Google is unreachable the
scrape still succeeds and reports `export.ok: false`, and the rows stay in the
CSV for the next push to pick up. Set it to `false` to go back to pushing only
when you click the button.

On a paid plan you can have both: attach a disk and set `DATA_DIR` to its
mount path, and the CSV becomes durable too.

Set in the Render dashboard (the blueprint marks them `sync: false`, so they
are prompted for and never stored in git):

| Variable | Value |
|---|---|
| `GOOGLE_SHEET_ID` | the id from your sheet URL |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the key, base64 encoded |
| `GOOGLE_SHEET_TAB` | `Sheet1` |
| `EXTENSION_IDS` | your extension's id, comma separated |

Set `EXTENSION_IDS` once the backend is public. Left unset, CORS accepts any
unpacked extension — fine on localhost, too loose on the open internet.

Then point the extension at it: open the popup and put the Render URL in
**Backend URL**. It is stored in `chrome.storage.sync` and defaults to
`http://127.0.0.1:8000`, so local development is unaffected.

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
