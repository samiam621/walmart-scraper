"""translation of a spanish scraped title to english

Which storefront a product came from is read off the scraped URL's domain —
the one fact a caller cannot forget to set — not a flag threaded through
every call site.

The current service behind deep_translator is scraped rather than an API
"""
#if we want to switch to google translate (paid)
from google.cloud import translate
#option


from __future__ import annotations

import time
from urllib.parse import urlparse

import requests
from deep_translator import GoogleTranslator
from deep_translator import google as _deep_translator_google

from models import Product

# Only non-English storefronts need translation
NON_ENGLISH_STOREFRONTS: dict[str, str] = {
    "walmart.com.mx": "es",
}

REQUEST_TIMEOUT = 4.0


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
RETRY_WAITS = (0.3, 0.8)

# Wall clock on the whole phase, so a slow translator delays the save/Sheets push that follows it, not blocks it; unfit titles are reported skipped and fill in later from the product page.
TRANSLATION_BUDGET_SECONDS = 8.0

# Per-page cap on titles translated, so one large grid can't spend minutes and get the service throttled for the product pages that follow; the rest are reported skipped, not dropped.
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


def _translate_one(translator: GoogleTranslator, text: str,
                   deadline: float) -> tuple[str | None, str | None]:
    """(translation, error). Retries a failure; gives up quietly after that.

    Both failure modes are treated alike: deep_translator raises when it
    cannot find a translation in the response, but it also has a path that
    returns None outright, and a caller that only caught the exception would
    still write a blank.

    `deadline` is a time.monotonic() value the retries stay inside — no new
    attempt is started past it, and a backoff never sleeps beyond it, so the
    slowest possible title is one attempt rather than the full three.
    """
    error = None
    for attempt in range(MAX_ATTEMPTS):
        if time.monotonic() >= deadline:
            return None, error or "translation budget exhausted"
        try:
            result = translator.translate(text)
            if result:
                return result, None
            error = "translator returned no text"
        except Exception as exc:  # noqa: BLE001 - reported, not raised; see module docstring
            error = f"{type(exc).__name__}: {exc}".strip()
        if attempt < len(RETRY_WAITS):
            time.sleep(max(0.0, min(RETRY_WAITS[attempt], deadline - time.monotonic())))
    return None, error


def annotate_all(products: list[Product]) -> dict:
    """Fill titleEn in place for every product from a non-English storefront.

    Never raises: a translation failure is not a scrape failure. What it could
    not do comes back in the report instead, so a blank cell in the sheet can
    be told apart from a product that needed no translating.
    """
    deadline = time.monotonic() + TRANSLATION_BUDGET_SECONDS
    translators: dict[str, GoogleTranslator] = {}
    # Grids repeat the same title across sponsored and organic slots, and a
    # repeat costs a whole round trip. Keyed by language too, since the same
    # words translate differently out of different languages.
    done: dict[tuple[str, str], str] = {}
    translated = failed = skipped = calls = 0
    reason: str | None = None
    out_of_time = False

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

        # Both bounds checked before the call, never during: a title is
        # either attempted within the budget or left for another scrape.
        # Calls are counted rather than products, so a page of duplicates is
        # not charged for translations it never had to make.
        if calls >= MAX_TRANSLATIONS or time.monotonic() >= deadline:
            out_of_time = out_of_time or time.monotonic() >= deadline
            skipped += 1
            continue

        calls += 1
        translator = translators.setdefault(
            language, GoogleTranslator(source=language, target="en")
        )
        result, error = _translate_one(translator, product.title, deadline)
        if result:
            product.titleEn = result
            done[(language, product.title)] = result
            translated += 1
        else:
            failed += 1
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
