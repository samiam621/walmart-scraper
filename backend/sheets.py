"""Pushes export rows into a Google Sheet, one spreadsheet per storefront.

Rows are routed by the storefront they were scraped from
"""

import os
from urllib.parse import urlparse

import export
import google_auth

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DEFAULT_TAB = "Sheet1"

# Storefront -> the country whose sheet it belongs in. Same set as
# export._WALMART_STOREFRONTS and scraper.WALMART_HOSTS.
_COUNTRY_BY_DOMAIN = {
    "walmart.com.mx": "MX",
    "walmart.ca": "CA",
    "walmart.com": "US",
}

# The order sheets are written in, so a mixed push reports predictably.
_COUNTRIES = ("US", "CA", "MX")


class SheetsError(RuntimeError):
    """Configuration or API failure, phrased for whoever has to fix it."""


def _load_key() -> dict:
    """The service account key, as a SheetsError when it cannot be found.

    Finding and validating the key is shared with the translation client, so
    it lives in google_auth. Re-raised as SheetsError here so every existing
    `except SheetsError` around the export keeps catching a bad key.
    """
    try:
        return google_auth.load_key()
    except google_auth.CredentialsError as exc:
        raise SheetsError(str(exc)) from exc


def _country(url: str | None) -> str | None:
    """Which country's sheet a URL belongs in, or None if it is not a
    storefront we know."""
    if not url:
        return None
    # Strip any userinfo and port so "www.walmart.ca:443" still matches.
    host = urlparse(url).netloc.lower().rsplit("@", 1)[-1].split(":", 1)[0]
    for domain, country in _COUNTRY_BY_DOMAIN.items():
        if host == domain or host.endswith("." + domain):
            return country
    return None


def _config() -> tuple[dict, dict[str, str], str]:
    """Key, the configured sheet ids by country, and the tab name.

    Countries with no id set are absent rather than blank; push() reports them
    per group so one unconfigured storefront cannot block the others.
    """
    sheet_ids = {
        country: os.getenv(f"GOOGLE_SHEET_ID_{country}", "").strip()
        for country in _COUNTRIES
    }
    sheet_ids = {country: i for country, i in sheet_ids.items() if i}

    if not sheet_ids:
        raise SheetsError(
            "Google Sheets export is not configured: set at least one of "
            "GOOGLE_SHEET_ID_US, GOOGLE_SHEET_ID_CA, GOOGLE_SHEET_ID_MX."
        )

    tab = os.getenv("GOOGLE_SHEET_TAB", "").strip() or DEFAULT_TAB
    return _load_key(), sheet_ids, tab


def _client(key: dict):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise SheetsError("Missing dependencies: pip install gspread google-auth") from exc

    try:
        credentials = Credentials.from_service_account_info(key, scopes=SCOPES)
    except (ValueError, KeyError) as exc:
        raise SheetsError(f"Service account key was rejected: {exc}") from exc

    return gspread.authorize(credentials)


def _open_worksheet(client, sheet_id: str, tab: str, key: dict):
    import gspread

    try:
        spreadsheet = client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (403, 404):
            raise SheetsError(
                "Cannot open sheet. Share it with gmail and make sure sheet id is correct"
            ) from exc
        raise SheetsError(f"Google Sheets API error: {exc}") from exc

    try:
        return spreadsheet.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        # Creating it beats failing: a fresh spreadsheet's only tab is often
        # named something else, and the export knows its own column count.
        return spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(export.COLUMNS))


def _existing_rows(worksheet) -> dict[str, int]:
    """Item id -> 1-based sheet row, so append can update in place.

    Matching on id alone is not enough to decide what to do with a repeat: a
    row written from a search-grid tile carries a title, an image and an id
    and nothing else, because a tile has no GTIN to give. Re-scraping that
    item's own product page is the normal way to fill those gaps, so the row
    number is kept, not just the id — see _merge_row.
    """
    item_id_column = export.COLUMNS.index("Item ID") + 1
    try:
        values = worksheet.col_values(item_id_column)
    except Exception:  # noqa: BLE001 - an unreadable sheet just means no dedupe
        return {}
    # enumerate from 2: row 1 is the header, and values[0] is that header.
    return {
        value.strip(): index
        for index, value in enumerate(values[1:], start=2)
        if value.strip()
    }


def _refines(new: str, old: str) -> bool:
    """True when `new` is `old` plus a grade: "Restored" -> "Restored: Like New".
    """
    return new.startswith(f"{old}: ")


# Columns a fresh scrape may *correct*, not merely fill. Every other
# column stays fill-only — re-scraping must not revert a hand-edited title.
CORRECTABLE_COLUMNS = ("GTIN", "Other Identifier")


def _merge_row(old: list, new: list) -> list | None:
    """Fill blanks in an existing row from a fresh scrape, and correct an
    identifier that disagrees with it. None if unchanged.
    """
    condition_column = export.COLUMNS.index("Item Condition")
    correctable = {export.COLUMNS.index(name) for name in CORRECTABLE_COLUMNS}
    merged = list(old) + [""] * (len(export.COLUMNS) - len(old))
    changed = False
    for index, value in enumerate(new):
        value, current = str(value).strip(), str(merged[index]).strip()
        if not value or value == current:
            continue
        if (not current
                or index in correctable
                or (index == condition_column and _refines(value, current))):
            merged[index] = value
            changed = True
    return merged if changed else None


def _push_group(worksheet, rows: list[list], mode: str) -> tuple[int, int]:
    """Write one country's rows to its worksheet. Returns (written, updated)."""
    if mode == "replace":
        worksheet.clear()
        worksheet.update(
            values=[list(export.COLUMNS)] + rows, range_name="A1", value_input_option="RAW"
        )
        return len(rows), 0

    existing = _existing_rows(worksheet)

    # Read the rows we might update in one call rather than one per row
    current = worksheet.get_all_values() if existing else []

    new_rows, updates = [], []
    for row in rows:
        line = existing.get(str(row[3]))
        if line is None:
            new_rows.append(row)
            continue
        old = current[line - 1] if line - 1 < len(current) else []
        merged = _merge_row(old, row)
        if merged:
            last_column = chr(ord("A") + len(export.COLUMNS) - 1)
            updates.append({"range": f"A{line}:{last_column}{line}", "values": [merged]})

    # An empty sheet still needs its header before the first append.
    if not worksheet.acell("A1").value:
        worksheet.update(
            values=[list(export.COLUMNS)], range_name="A1", value_input_option="RAW"
        )
    if updates:
        # One request for every repaired row, not one per row.
        worksheet.batch_update(updates, value_input_option="RAW")
    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="RAW")
    return len(new_rows), len(updates)


def _group_by_country(records, page_url: str | None) -> dict[str, list]:
    """Split records into the sheet each one belongs in.

    A record's own URL decides, so the mixed-storefront CSV re-export routes
    correctly. Grid tiles sometimes arrive without one, and those inherit the
    page they were scraped from; with neither, .com is the storefront the
    scraper sees most (same fallback as export._listing_base).
    """
    fallback = _country(page_url) or "US"
    groups: dict[str, list] = {}
    for record in records:
        country = _country(export._get(record, "url")) or fallback
        groups.setdefault(country, []).append(record)
    return groups


def push(records, mode: str = "append", page_url: str | None = None) -> dict:
    """Write products to the sheet for the storefront each came from.

    append   adds rows for products the sheet does not have, fills in blank
             cells on the rows it does, and corrects an identifier that
             disagrees with the fresh scrape (see _merge_row)
    replace  clears the tab and rewrites it from the given products. Only the
             sheets whose countries appear in `records` are touched — a US-only
             push must not blank the CA and MX sheets.

    page_url is the page the records were scraped from, used for tiles that
    carry no URL of their own. Leave it unset when the records come from the
    CSV, which mixes storefronts.

    Values go up with value_input_option="RAW" so Sheets stores them verbatim.
    The default, USER_ENTERED, parses every cell as if it were typed: it would
    strip the leading zeros off "00469139796107" and turn it into 469139796107.
    """
    if mode not in ("append", "replace"):
        raise SheetsError(f"Unknown mode {mode!r}; use 'append' or 'replace'.")

    key, sheet_ids, tab = _config()
    groups = _group_by_country(records, page_url)

    client = None
    written = updated = skipped = 0
    results, errors = [], []

    for country in _COUNTRIES:
        group = groups.get(country)
        if not group:
            continue

        if country not in sheet_ids:
            errors.append({
                "country": country,
                "count": len(group),
                "reason": f"GOOGLE_SHEET_ID_{country} is not set, so {len(group)} "
                          f"{country} row(s) were not exported.",
            })
            continue

        # One authorization covers every spreadsheet, so build it once and
        # only when there is something to write.
        if client is None:
            client = _client(key)

        worksheet = _open_worksheet(client, sheet_ids[country], tab, key)
        rows = export.build_rows(group, header=False)
        rows_written, rows_updated = _push_group(worksheet, rows, mode)
        _format_header(worksheet)

        # Per group, not once at the end: dedupe is per worksheet, so a row
        # already present in the CA sheet says nothing about the US one.
        rows_skipped = len(rows) - rows_written - rows_updated
        written += rows_written
        updated += rows_updated
        skipped += rows_skipped
        results.append({
            "country": country,
            "rowsWritten": rows_written,
            "rowsUpdated": rows_updated,
            "skipped": rows_skipped,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet_ids[country]}/edit",
        })

    if not results:
        raise SheetsError(
            "Nothing was exported: no sheet is configured for "
            + ", ".join(e["country"] for e in errors)
            + ". Set the matching GOOGLE_SHEET_ID_* variable."
        )

    return {
        "status": "partial" if errors else "ok",
        "mode": mode,
        "rowsWritten": written,
        "rowsUpdated": updated,
        #  nothing to do: already present *and* nothing to fill in.
        "skipped": skipped,
        "tab": tab,
        # Only meaningful when one sheet was written; a mixed push has several.
        "url": results[0]["url"] if len(results) == 1 else None,
        "sheets": results,
        "errors": errors,
    }


def _format_header(worksheet) -> None:
    """Bold + frozen header. Cosmetic, so a failure here is not an error."""
    try:
        last_column = chr(ord("A") + len(export.COLUMNS) - 1)
        worksheet.freeze(rows=1)
        worksheet.format(f"A1:{last_column}1", {"textFormat": {"bold": True}})
    except Exception:  # noqa: BLE001
        pass
