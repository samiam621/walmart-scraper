"""CSV persistence.

Uses the csv module rather than pandas-per-row: appending with
`DataFrame.to_csv(mode='a', header=False)` silently writes columns in
whatever order that one DataFrame happened to have, so a row written after
the model gains a field lands under the wrong headers.
"""

import csv
import os
from pathlib import Path

from models import Product

# Overridable because a hosted container's filesystem is wiped on every deploy
# and restart. Point DATA_DIR at a mounted persistent disk there, or the CSV —
# and so the whole export backlog — silently resets.
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data")).expanduser()
FILE_PATH = DATA_DIR / "products.csv"

# Fixed column order, taken from the model definition once at import time.
# `sources` is debugging metadata, not data — keep it out of the CSV.
FIELDNAMES = [name for name in Product.model_fields if name != "sources"]


def save_product(product: Product, path: Path = FILE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0

    row = product.model_dump(exclude={"sources"})
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def load_products(path: Path = FILE_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def count(path: Path = FILE_PATH) -> int:
    return len(load_products(path))
