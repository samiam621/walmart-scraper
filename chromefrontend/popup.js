
const USE_LOCAL = false;

const LOCAL_BACKEND = 'http://127.0.0.1:8000';
const DEFAULT_BACKEND = 'https://walmart-scraper-mdp2.onrender.com';
const BACKEND = USE_LOCAL ? LOCAL_BACKEND : DEFAULT_BACKEND;

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


// Item ids from the most recent scrape
let lastScrapedIds = [];

// Runs inside the page, not the popup. Returns the rendered HTML so the
// ReadingdocumentElement (rather than fetching the URL again) is the whole point:
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
    // show condition ex. "Restored: Like New"
    // in place of the bucket it normalizes to.
    const value = field === 'condition' ? (product.conditionDetail ?? product.condition)
      : product[field];
    if (value === null || value === undefined || value === '') continue;
    parts.push(row(field, field === 'price' ? `${product.currency ?? '$'}${value}` : value));
  }
  return parts;
}

//  gets a compact price + title list
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


async function scrapeActiveTab() {
  button.disabled = true;
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

