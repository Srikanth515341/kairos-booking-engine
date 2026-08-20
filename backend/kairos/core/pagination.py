"""Cursor (keyset) pagination (Spec v1.0 §8) — opaque to clients, base64 of
`(sort_key, id)`. Never offset-based: an offset shifts under concurrent
inserts or deletes, silently skipping or duplicating rows on a list that's
concurrently written — which describes every list endpoint in this system.
A cursor anchored on a stable sort key plus an id tiebreak has no such
failure mode: the next page is always "rows after this exact key," not
"rows starting at this position."
"""

from __future__ import annotations

import base64
import json

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def encode_cursor(sort_key: str, row_id: str) -> str:
    payload = json.dumps([sort_key, row_id])
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Raises ValueError on any malformed cursor — callers should surface
    that as a 400, never silently ignore it and return an arbitrary page.
    """
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        decoded = json.loads(payload)
    except Exception as exc:
        raise ValueError("malformed cursor") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not isinstance(decoded[0], str)
        or not isinstance(decoded[1], str)
    ):
        raise ValueError("malformed cursor")
    sort_key, row_id = decoded
    return sort_key, row_id


def parse_limit(raw_limit: str | None) -> int:
    """Raises ValueError on an invalid limit — callers should surface that
    as a 400."""
    if raw_limit is None:
        return DEFAULT_PAGE_SIZE
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_SIZE)
