"""Shared runtime logging setup and redaction helpers."""

from __future__ import annotations

import logging
import re
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

_SECRET_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|token)=([^&\s'\"`]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(sk-[a-z0-9-]{6})[a-z0-9_-]+")
_CONFIGURED = False


def redact_sensitive_text(text: str) -> str:
    """Remove secrets from strings before they hit logs or notifications."""
    redacted = _SECRET_QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    return _BEARER_RE.sub(r"\1[redacted]", redacted)


def clean_log_value(value: object, *, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", redact_sensitive_text(str(value))).strip()
    if not text:
        return "unknown"
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def configure_logging(level_name: str | None = None) -> None:
    """Configure one clean logger for CLI and cron runs."""
    global _CONFIGURED
    if level_name is None:
        from entropyseo.config import get_settings

        level_name = get_settings().log_level
    root = logging.getLogger()
    if not _CONFIGURED:
        root.handlers.clear()
        handler = RichHandler(
            console=Console(stderr=True), show_time=False, show_path=False, markup=False, rich_tracebacks=False
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        _CONFIGURED = True
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "httpx2", "urllib3", "anthropic", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    suffix = " · ".join(f"{key}={clean_log_value(value)}" for key, value in fields.items() if value is not None)
    logger.log(level, "%s%s", event, f" · {suffix}" if suffix else "")
