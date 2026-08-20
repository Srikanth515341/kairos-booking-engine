"""Structured JSON logging (Implementation Plan Phase 4 scope). Every log
record's standard fields plus whatever the caller passes via `extra=`
(request_id, user_id, resource_id, outcome, ...) — a flat JSON object per
line, so log aggregation doesn't need to parse free-text messages.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_RESERVED_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message"
}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
