"""Locates and validates the Google service account key, for every consumer.

uses same service account json as sheets


"""

import base64
import binascii
import json
import os
from pathlib import Path


class CredentialsError(RuntimeError):
    """Configuration failure, phrased for whoever has to fix it."""


def load_key() -> dict:
    """Return the service account key as a dict, from whichever source is set.

    Raises CredentialsError with the fix in the message, because every failure
    here is a configuration mistake someone has to go and correct.
    """
    inline = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if inline:
        return _parse_key(inline)

    key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not key_path:
        raise CredentialsError(
            "No service account key: set GOOGLE_SERVICE_ACCOUNT_JSON (the key's "
            "contents) or GOOGLE_SERVICE_ACCOUNT_FILE (a path to it). See the "
            "Google credentials section of the README."
        )

    path = Path(key_path).expanduser()
    if not path.is_file():
        raise CredentialsError(
            f"Service account key not found at {path}. On Render, either add it "
            f"as a Secret File (then point this at /etc/secrets/<filename>) or "
            f"paste the JSON into GOOGLE_SERVICE_ACCOUNT_JSON instead — a path "
            f"from your laptop does not exist on the server."
        )

    try:
        return _parse_key(path.read_text())
    except OSError as exc:
        raise CredentialsError(f"Could not read {path}: {exc}") from exc


def _parse_key(raw: str) -> dict:
    """Parse a key from raw JSON or from base64-encoded JSON.
    """
    text = raw.strip()

    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise CredentialsError(
                "GOOGLE_SERVICE_ACCOUNT_JSON is neither JSON nor base64. Paste "
                "the whole key file, starting with '{'."
            ) from exc

    try:
        key = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CredentialsError(f"Service account key is not valid JSON: {exc}") from exc

    missing = [f for f in ("client_email", "private_key", "token_uri") if not key.get(f)]
    if missing:
        raise CredentialsError(
            f"Service account key is missing {', '.join(missing)}. That is not a "
            f"service account key — download a fresh one from the Keys tab."
        )

    # A key pasted through a form often arrives with its newlines escaped one
    # level too deep, which fails at signing time with an opaque padding error
    # rather than here. Undo it while we can still say what went wrong.
    if "\\n" in key["private_key"] and "\n" not in key["private_key"]:
        key["private_key"] = key["private_key"].replace("\\n", "\n")

    return key
