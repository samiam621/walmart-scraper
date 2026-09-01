"""Translation of a Spanish scraped title to English.

Which storefront a product came from is read off the scraped URL's domain —
the one fact a caller cannot forget to set — not a flag threaded through
every call site.

The backend is the Cloud Translation API (v2), authenticated with the same
service account json as the Sheets 
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

from google.api_core import exceptions as google_exceptions
from google.cloud import translate_v2 as translate

import google_auth
from models import Product

# Only non-English storefronts need translation
NON_ENGLISH_STOREFRONTS: dict[str, str] = {
    "walmart.com.mx": "es",
}

# v2 Translation has no narrower scope of its own the way Sheets does.
TRANSLATE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Attempts per batch, and the waits between them. Short: a scrape is blocked
# on this, and the failure being retried is usually transient.
MAX_ATTEMPTS = 3
RETRY_WAITS = (0.3, 0.8)

# Wall clock on the whole phase, so a slow translator delays the save/Sheets push that follows it, not blocks it; unfit titles are reported skipped and fill in later from the product page.
#

TRANSLATION_BUDGET_SECONDS = 8.0

# Per-page cap on distinct titles translated, bounding what one large grid
# costs and how big a single request gets; the rest are reported skipped, not
# dropped.
MAX_TRANSLATIONS = 25


def _source_language(url: str | None) -> str | None:
    if not url:
        return None
    # Strip any userinfo and port, same normalization export.py's
    # _listing_base uses, so "www.walmart.com.mx:443" still matches.
    host = urlparse(url).netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]
    for domain, language in NON_ENGLISH_STOREFRONTS.items():
        if host == domain or host.endswith("." + domain):
            return language
    return None


def _client() -> translate.Client:
    """Built per annotate_all() call rather than cached, like sheets._client:
    the simplest thing that stays correct when several requests are in flight
    at once through asyncio.to_thread.
    """
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_info(
        google_auth.load_key(), scopes=TRANSLATE_SCOPES
    )
    return translate.Client(credentials=credentials)


def _translate_batch(client: translate.Client, texts: list[str],
                     source_language: str,
                     deadline: float) -> tuple[list[str] | None, str | None]:
    """(translations, error) for a whole list of titles in one call.

    All-or-nothing per call: a retry re-sends the entire list, so a failure
    that outlives MAX_ATTEMPTS fails every title in it together. See
    annotate_all for why that trade is worth making.

    `deadline` is a time.monotonic() value the retries stay inside — no new
    attempt is started past it, and a backoff never sleeps beyond it.
    """
    error = None
    for attempt in range(MAX_ATTEMPTS):
        if time.monotonic() >= deadline:
            return None, error or "translation budget exhausted"
        try:
            # A list argument always comes back a list, even at length one;
            # only a bare string gets a bare dict back.
            results = client.translate(
                texts, source_language=source_language, target_language="en"
            )
            return [result["translatedText"] for result in results], None
        except google_exceptions.GoogleAPICallError as exc:
            error = f"{type(exc).__name__}: {exc}".strip()
        except Exception as exc:  # noqa: BLE001 - reported, not raised; see annotate_all
            error = f"{type(exc).__name__}: {exc}".strip()
        if attempt < len(RETRY_WAITS):
            time.sleep(max(0.0, min(RETRY_WAITS[attempt], deadline - time.monotonic())))
    return None, error


def annotate_all(products: list[Product]) -> dict:
    """Fill titleEn in place for every product from a non-English storefront.

    Two passes: collect what needs translating, then send one request per
    language. Failure granularity is coarser than translating title by title
    — one dead batch marks every title in that language failed, where before
    a bad title left its siblings alone. Since a page is almost always a
    single storefront (only walmart.com.mx today), that usually reads as "the
    page's titles failed together". Accepted for cutting up to 25 sequential
    round trips down to about one.

    Never raises on a translation failure: that is not a scrape failure, and
    what it could not do comes back in the report instead, so a blank cell in
    the sheet can be told apart from a product that needed no translating. A
    missing or malformed key does propagate — app.py turns it into the same
    report shape.
    """
    # Pass 1: what to translate, deduped and capped, no network. Grids repeat
    # the same title across sponsored and organic slots; keyed by language
    # too, since the same words translate differently out of different ones.
    targets: dict[tuple[str, str], list[Product]] = {}
    order: dict[str, list[str]] = {}
    skipped = distinct = 0

    for product in products:
        if product.titleEn or not product.title:
            continue
        language = _source_language(product.url)
        if not language:
            continue

        key = (language, product.title)
        if key not in targets:
            # The cap counts distinct titles, not products: a page of
            # duplicates is not charged for translations it never makes.
            if distinct >= MAX_TRANSLATIONS:
                skipped += 1
                continue
            distinct += 1
            order.setdefault(language, []).append(product.title)
            targets[key] = []
        targets[key].append(product)

    # Pass 2: one batch per language.
    translated = failed = 0
    reason: str | None = None
    out_of_time = False
    deadline = time.monotonic() + TRANSLATION_BUDGET_SECONDS
    client = None

    for language, titles in order.items():
        if time.monotonic() >= deadline:
            out_of_time = True
            skipped += sum(len(targets[(language, title)]) for title in titles)
            continue

        # Built only when there is something to send, so a page with nothing
        # to translate never touches credentials.
        if client is None:
            client = _client()

        results, error = _translate_batch(client, titles, language, deadline)
        if results is not None:
            for title, text in zip(titles, results):
                for product in targets[(language, title)]:
                    product.titleEn = text
                    translated += 1
        else:
            failed += sum(len(targets[(language, title)]) for title in titles)
            reason = reason or error

    if skipped and reason is None:
        reason = (
            f"stopped after {TRANSLATION_BUDGET_SECONDS:g}s"
            if out_of_time
            else f"capped at {MAX_TRANSLATIONS} titles for one page"
        ) + "; scrape the product pages to fill in the rest"

    return {
        "ok": failed == 0,
        "translated": translated,
        "failed": failed,
        "skipped": skipped,
        "reason": reason,
    }
