"""translation of a spanish scraped title to english

Which storefront a product came from is read off the scraped URL's domain —
the one fact a caller cannot forget to set — not a flag threaded through
every call site.

The service behind deep_translator is scraped rather than an API: it answers
with an unparseable page often enough (measured at roughly 1 call in 12) that
a single attempt drops titles at random. Those misses are invisible in the
sheet, because a blank cell there looks exactly like a product that needed no
translating. So this module retries, and it reports what it could not do
instead of swallowing it — see annotate_all's return value, which app.py puts
in the scrape response the same way it reports a failed Sheets push.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import requests
from deep_translator import GoogleTranslator
from deep_translator import google as _deep_translator_google

from models import Product

# domain -> deep_translator source language. Only non-English storefronts need
# an entry; walmart.com and walmart.ca (English tab) are absent on purpose so
# their titles pass through untouched.
NON_ENGLISH_STOREFRONTS: dict[str, str] = {
    "walmart.com.mx": "es",
}

# deep_translator calls requests.get() with no timeout, so a connection that
# stalls would hang the scrape thread indefinitely — and this runs inside a
# request someone is waiting on. Its google module holds `requests` as a
# module-global and only ever calls .get() on it, so replacing that one name
# gives every call it makes a deadline without altering requests for anything
# else in the process.
REQUEST_TIMEOUT = 8.0


class _TimeoutRequests:
    """Stands in for the `requests` module inside deep_translator.google."""

    @staticmethod
    def get(*args, **kwargs):
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        return requests.get(*args, **kwargs)


_deep_translator_google.requests = _TimeoutRequests

# Attempts per title, and the waits between them. Short: a scrape is blocked
# on this, and the failure being retried is a bad response rather than a busy
# server, so it usually clears immediately.
MAX_ATTEMPTS = 3
RETRY_WAITS = (0.4, 1.2)

# A search page carries dozens of tiles, and every title is its own round
# trip. Uncapped, one grid scrape would spend minutes translating and is the
# surest way to get throttled — which would then cost the *product* pages
# their translations too. Past this the titles are left to be filled in when
# the product page itself is scraped, and the count is reported rather than
# quietly dropped.
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


def _translate_one(translator: GoogleTranslator, text: str) -> tuple[str | None, str | None]:
    """(translation, error). Retries a failure; gives up quietly after that.

    Both failure modes are treated alike: deep_translator raises when it
    cannot find a translation in the response, but it also has a path that
    returns None outright, and a caller that only caught the exception would
    still write a blank.
    """
    error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            result = translator.translate(text)
            if result:
                return result, None
            error = "translator returned no text"
        except Exception as exc:  # noqa: BLE001 - reported, not raised; see module docstring
            error = f"{type(exc).__name__}: {exc}".strip()
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])
    return None, error


def annotate_all(products: list[Product]) -> dict:
    """Fill titleEn in place for every product from a non-English storefront.

    Never raises: a translation failure is not a scrape failure. What it could
    not do comes back in the report instead, so a blank cell in the sheet can
    be told apart from a product that needed no translating.
    """
    translators: dict[str, GoogleTranslator] = {}
    # Grids repeat the same title across sponsored and organic slots, and a
    # repeat costs a whole round trip. Keyed by language too, since the same
    # words translate differently out of different languages.
    done: dict[tuple[str, str], str] = {}
    translated = failed = skipped = calls = 0
    reason: str | None = None

    for product in products:
        if product.titleEn or not product.title:
            continue
        language = _source_language(product.url)
        if not language:
            continue

        cached = done.get((language, product.title))
        if cached is not None:
            product.titleEn = cached
            translated += 1
            continue

        # Counted against the calls actually made, so a page of duplicates
        # is not charged for translations it never asked for.
        if calls >= MAX_TRANSLATIONS:
            skipped += 1
            continue

        calls += 1
        translator = translators.setdefault(
            language, GoogleTranslator(source=language, target="en")
        )
        result, error = _translate_one(translator, product.title)
        if result:
            product.titleEn = result
            done[(language, product.title)] = result
            translated += 1
        else:
            failed += 1
            reason = reason or error

    if skipped and reason is None:
        reason = (
            f"capped at {MAX_TRANSLATIONS} titles for one page; scrape the "
            f"product pages to fill in the rest"
        )

    return {
        "ok": failed == 0,
        "translated": translated,
        "failed": failed,
        "skipped": skipped,
        "reason": reason,
    }
