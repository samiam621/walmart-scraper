"""Pushes export rows into a Google Sheet.

Auth is a service account rather than OAuth: the backend runs headless next to
uvicorn, and an OAuth flow needs a human at a browser to refresh consent. The
cost is one manual step — the sheet has to be shared with the service
account's own email address, because a service account is a separate principal
that owns nothing by default. That is the step people miss, so it is what the
error messages here point at.

Configuration, all environment variables. Supply the key either way — the
inline form wins if both are set:

  GOOGLE_SERVICE_ACCOUNT_JSON   the key's JSON contents, or that JSON base64
                                encoded. For hosts like Render where there is
                                no filesystem to put a file on.
  GOOGLE_SERVICE_ACCOUNT_FILE   path to the key file. For local runs, and for
                                Render Secret Files (/etc/secrets/<name>).
  GOOGLE_SHEET_ID               the id from the sheet URL, /d/<this>/edit
  GOOGLE_SHEET_TAB              worksheet name, defaults to Sheet1
"""

import base64
import binascii
import json
import os
from pathlib import Path

import export

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_TAB = "Sheet1"


class SheetsError(RuntimeError):
    """Configuration or API failure, phrased for whoever has to fix it."""


def _load_key() -> dict:
    """Return the service account key as a dict, from whichever source is set.

    Raises SheetsError with the fix in the message, because every failure here
    is a configuration mistake someone has to go and correct.
    """
    inline = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if inline:
        return _parse_key(inline)

    key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not key_path:
        raise SheetsError(
            "No service account key: set GOOGLE_SERVICE_ACCOUNT_JSON (the key's "
            "contents) or GOOGLE_SERVICE_ACCOUNT_FILE (a path to it). See the "
            "Google Sheets section of the README."
        )

    path = Path(key_path).expanduser()
    if not path.is_file():
        raise SheetsError(
            f"Service account key not found at {path}. On Render, either add it "
            f"as a Secret File (then point this at /etc/secrets/<filename>) or "
            f"paste the JSON into GOOGLE_SERVICE_ACCOUNT_JSON instead — a path "
            f"from your laptop does not exist on the server."
        )

    try:
        return _parse_key(path.read_text())
    except OSError as exc:
        raise SheetsError(f"Could not read {path}: {exc}") from exc


def _parse_key(raw: str) -> dict:
    """Parse a key from raw JSON or from base64-encoded JSON.
    """
    text = raw.strip()

    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SheetsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor base64. Paste "
                "the whole key file, starting with '{'."
            ) from exc

    try:
        key = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SheetsError(f"Service account key is not valid JSON: {exc}") from exc

    missing = [f for f in ("client_email", "private_key", "token_uri") if not key.get(f)]
    if missing:
        raise SheetsError(
            f"Service account key is missing {', '.join(missing)}. That is not a "
            f"service account key — download a fresh one from the Keys tab."
        )

    # A key pasted through a form often arrives with its newlines escaped one
    # level too deep, which fails at signing time with an opaque padding error
    # rather than here. Undo it while we can still say what went wrong.
    if "\\n" in key["private_key"] and "\n" not in key["private_key"]:
        key["private_key"] = key["private_key"].replace("\\n", "\n")

    return key


def _config() -> tuple[dict, str, str]:
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise SheetsError(
            "Google Sheets export is not configured: set GOOGLE_SHEET_ID to the "
            "part of the sheet URL between /d/ and /edit."
        )

    key = _load_key()
    return key, sheet_id, os.getenv("GOOGLE_SHEET_TAB", "").strip() or DEFAULT_TAB


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
                f"Cannot open sheet {sheet_id}. Share it (Editor) with "
                f"{key.get('client_email', 'the service account')}, and check "
                f"the id is the part of the URL between /d/ and /edit."
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


def _merge_row(old: list, new: list) -> list | None:
    """Fill blanks in an existing row from a fresh scrape. None if unchanged.

    Only empty cells are written. A re-scrape is allowed to complete a row,
    never to overwrite it: the sheet is the durable record and may have been
    edited by hand, and a later scrape of the same item can legitimately carry
    *less* than the first (a grid tile after a product page).
    """
    merged = list(old) + [""] * (len(export.COLUMNS) - len(old))
    changed = False
    for index, value in enumerate(new):
        if str(value).strip() and not str(merged[index]).strip():
            merged[index] = value
            changed = True
    return merged if changed else None


def push(records, mode: str = "append") -> dict:
    """Write products to the configured sheet.

    append   adds rows for products the sheet does not have, and fills in
             blank cells on the rows it does (see _merge_row)
    replace  clears the tab and rewrites it from the given products

    Values go up with value_input_option="RAW" so Sheets stores them verbatim.
    The default, USER_ENTERED, parses every cell as if it were typed: it would
    strip the leading zeros off "00469139796107" and turn it into 469139796107.
    """
    if mode not in ("append", "replace"):
        raise SheetsError(f"Unknown mode {mode!r}; use 'append' or 'replace'.")

    key, sheet_id, tab = _config()
    worksheet = _open_worksheet(_client(key), sheet_id, tab, key)

    all_rows = export.build_rows(records, header=False)
    rows = all_rows

    if mode == "replace":
        worksheet.clear()
        worksheet.update(
            values=[list(export.COLUMNS)] + rows, range_name="A1", value_input_option="RAW"
        )
        written = len(rows)
        updated = 0
    else:
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
        written = len(new_rows)
        updated = len(updates)

    _format_header(worksheet)

    return {
        "status": "ok",
        "mode": mode,
        "rowsWritten": written,
        "rowsUpdated": updated,
        # Genuinely nothing to do: already present *and* nothing to fill in.
        "skipped": len(all_rows) - written - updated,
        "tab": tab,
        "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    }


def _format_header(worksheet) -> None:
    """Bold + frozen header. Cosmetic, so a failure here is not an error."""
    try:
        last_column = chr(ord("A") + len(export.COLUMNS) - 1)
        worksheet.freeze(rows=1)
        worksheet.format(f"A1:{last_column}1", {"textFormat": {"bold": True}})
    except Exception:  # noqa: BLE001
        pass
