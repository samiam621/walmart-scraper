// Not a constant any more: the same extension has to talk to a local dev
// server or a deployed one. Loaded from chrome.storage.sync at popup open,
// falling back to this when nothing is stored — the deployed backend, so a
// fresh install works without setup. For local work, put http://127.0.0.1:8000
// in the Backend URL field; that is stored and wins over this default.
const DEFAULT_BACKEND = 'https://walmart-scraper-mdp2.onrender.com';
let BACKEND = DEFAULT_BACKEND;

// Trailing slashes double up when concatenated with a path, producing a 404
// that looks like a missing endpoint rather than a typo.
function normalizeBackend(value) {
  const trimmed = (value ?? '').trim().replace(/\/+$/, '');
  if (!trimmed) return DEFAULT_BACKEND;
  if (!/^https?:\/\//.test(trimmed)) throw new Error('Backend URL must start with http:// or https://');
  return trimmed;
}

// Fields worth surfacing above the raw JSON, in display order.
const SUMMARY_FIELDS = ['price', 'condition', 'brand', 'upc', 'itemId', 'availability'];

const status = document.getElementById('status');
const summary = document.getElementById('summary');
const output = document.getElementById('output');
const button = document.getElementById('scrape-btn');
const sheetsButton = document.getElementById('sheets-btn');
const backendInput = document.getElementById('backend-url');

// Item ids from the most recent scrape. The export endpoint takes ids rather
// than "everything saved", so the button pushes the page you are looking at
// instead of re-uploading the whole CSV every time.
let lastScrapedIds = [];

// Runs inside the page, not the popup. Returns the rendered HTML so the
// backend does the parsing — one scraper, not one per site. Reading
// documentElement (rather than fetching the URL again) is the whole point:
// it is the DOM after hydration, with the user's own session, so it works on
// any product page in any tab without a per-site integration.
function grabPageHtml() {
  return {
    url: location.href,
    html: document.documentElement.outerHTML,
  };
}

// Never build HTML from scraped strings — a product title is attacker-
// controlled text and innerHTML would execute whatever is in it.
function row(label, value) {
  const line = document.createElement('div');
  const key = document.createElement('span');
  key.className = 'k';
  key.textContent = label;
  line.append(key, document.createTextNode(String(value)));
  return line;
}

function describeProduct(product) {
  const parts = [];
  for (const field of SUMMARY_FIELDS) {
    const value = product[field];
    if (value === null || value === undefined || value === '') continue;
    parts.push(row(field, field === 'price' ? `${product.currency ?? '$'}${value}` : value));
  }
  return parts;
}

// A grid gets a compact price + title list; showing every field for 40 items
// would bury the thing the user actually wants to see.
function describeListing(products) {
  return products.map((product) => {
    const price = product.price === null ? '—' : `$${product.price}`;
    const line = row(price, product.title ?? 'untitled');
    if (product.condition === 'refurbished') line.classList.add('refurb');
    return line;
  });
}

async function activeTabHtml() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error('No active tab.');

  // executeScript is rejected on chrome://, the Web Store, and PDF viewers.
  // Say so plainly instead of surfacing Chrome's opaque error.
  if (!/^https?:\/\//.test(tab.url ?? '')) {
    throw new Error('Open a product page in a normal http(s) tab first.');
  }

  const frames = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: grabPageHtml,
  });
  const result = frames?.[0]?.result;
  if (!result?.html) throw new Error('Could not read the page. Try reloading it.');
  return result;
}

async function post(payload, path = '/api/scrape-page') {
  let response;
  try {
    response = await fetch(`${BACKEND}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch {
    // fetch only rejects on a transport failure, which here means the server
    // is not running — a distinct problem from a page that failed to parse.
    throw new Error(`Backend not reachable at ${BACKEND}. Is uvicorn running?`);
  }

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(body?.detail ?? `Server returned ${response.status}`);
  }
  return body;
}

async function sendToSheets() {
  sheetsButton.disabled = true;
  status.classList.remove('error');
  status.textContent = 'Sending to Google Sheets…';

  try {
    const result = await post({ mode: 'append', itemIds: lastScrapedIds }, '/api/export/sheets');
    // "Updated" is its own count, not a kind of skip: it means a row that was
    // already there had blanks a fresh scrape could fill.
    const updated = result.rowsUpdated ? `, ${result.rowsUpdated} filled in` : '';
    const skipped = result.skipped ? `, ${result.skipped} already there` : '';
    status.textContent = `Added ${result.rowsWritten} row(s) to ${result.tab}${updated}${skipped}`;
  } catch (error) {
    console.error(error);
    status.textContent = error.message;
    status.classList.add('error');
  } finally {
    sheetsButton.disabled = lastScrapedIds.length === 0;
  }
}

async function scrapeActiveTab() {
  button.disabled = true;
  sheetsButton.disabled = true;
  lastScrapedIds = [];
  summary.replaceChildren();
  output.style.display = 'none';
  status.textContent = 'Reading page…';

  try {
    const payload = await activeTabHtml();
    status.textContent = `Parsing ${new URL(payload.url).hostname}…`;

    const { pageType, count, products, export: exported } = await post(payload);

    if (pageType === 'listing') {
      const refurbished = products.filter((p) => p.condition === 'refurbished').length;
      status.textContent = `Saved ${count} products`
        + (refurbished ? ` (${refurbished} refurbished)` : '');
      summary.replaceChildren(...describeListing(products));
    } else {
      status.textContent = `Saved: ${products[0].title ?? 'untitled'}`;
      summary.replaceChildren(...describeProduct(products[0]));
    }

    // The backend pushes to Sheets on the way through when AUTO_EXPORT_SHEETS
    // is on. Report it, and say so loudly when it failed: on a host with no
    // persistent disk the sheet is the only durable copy, so a silent failure
    // here is a scrape you are about to lose.
    if (exported) {
      status.append(document.createTextNode(
        exported.ok
          ? ` · ${exported.rowsWritten} to Sheets${exported.skipped ? `, ${exported.skipped} dup` : ''}`
          : ' · not sent to Sheets',
      ));
      if (!exported.ok) summary.append(row('Sheets', exported.reason));
    }

    // Rows without an id cannot be addressed by the export endpoint, so they
    // are dropped here rather than failing server-side.
    lastScrapedIds = products.map((p) => p.itemId).filter(Boolean).map(String);
    sheetsButton.disabled = lastScrapedIds.length === 0;

    output.style.display = 'block';
    output.textContent = JSON.stringify(products, null, 2);
  } catch (error) {
    console.error(error);
    status.textContent = error.message;
    status.classList.add('error');
  } finally {
    button.disabled = false;
  }
}

button.addEventListener('click', () => {
  status.classList.remove('error');
  scrapeActiveTab();
});

sheetsButton.addEventListener('click', sendToSheets);

// Persist on edit rather than behind a save button: one field, and a settings
// value that silently failed to save is worse than an extra write.
backendInput.addEventListener('change', async () => {
  try {
    BACKEND = normalizeBackend(backendInput.value);
    backendInput.value = BACKEND;
    await chrome.storage.sync.set({ backendUrl: BACKEND });
    status.classList.remove('error');
    status.textContent = `Backend set to ${BACKEND}`;
  } catch (error) {
    status.textContent = error.message;
    status.classList.add('error');
  }
});

// Load the stored backend before the user can click anything, so the first
// scrape after opening the popup cannot race the read and hit localhost.
(async () => {
  button.disabled = true;
  try {
    const { backendUrl } = await chrome.storage.sync.get('backendUrl');
    BACKEND = normalizeBackend(backendUrl);
  } catch {
    BACKEND = DEFAULT_BACKEND;
  }
  backendInput.value = BACKEND;
  button.disabled = false;
})();
