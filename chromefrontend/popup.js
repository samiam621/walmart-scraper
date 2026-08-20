const BACKEND = 'http://127.0.0.1:8000';

// Fields worth surfacing above the raw JSON, in display order.
const SUMMARY_FIELDS = ['price', 'condition', 'brand', 'upc', 'itemId', 'availability'];

const status = document.getElementById('status');
const summary = document.getElementById('summary');
const output = document.getElementById('output');
const button = document.getElementById('scrape-btn');

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

async function post(payload) {
  let response;
  try {
    response = await fetch(`${BACKEND}/api/scrape-page`, {
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
  summary.replaceChildren();
  output.style.display = 'none';
  status.textContent = 'Reading page…';

  try {
    const payload = await activeTabHtml();
    status.textContent = `Parsing ${new URL(payload.url).hostname}…`;

    const { pageType, count, products } = await post(payload);

    if (pageType === 'listing') {
      const refurbished = products.filter((p) => p.condition === 'refurbished').length;
      status.textContent = `Saved ${count} products`
        + (refurbished ? ` (${refurbished} refurbished)` : '');
      summary.replaceChildren(...describeListing(products));
    } else {
      status.textContent = `Saved: ${products[0].title ?? 'untitled'}`;
      summary.replaceChildren(...describeProduct(products[0]));
    }

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
