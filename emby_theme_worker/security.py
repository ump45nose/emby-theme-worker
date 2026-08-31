from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit


SECRET_KEYS = re.compile(r"(?i)(token|api[_-]?key|authorization|cookie|password|secret)=([^&\s]+)")
URL_QUERY = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def redact(value: object) -> str:
    text = str(value)
    text = SECRET_KEYS.sub(r"\1=<redacted>", text)

    def _strip_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            return f"{parts.scheme}://{parts.netloc}/<redacted>"
        except ValueError:
            return "<redacted-url>"

    return URL_QUERY.sub(_strip_url, text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def read_secret(path: str | Path, *, required: bool = True) -> str | None:
    target = Path(path)
    if not target.exists():
        if required:
            raise FileNotFoundError(f"secret file missing: {target}")
        return None
    value = target.read_text(encoding="utf-8").strip()
    if required and not value:
        raise ValueError(f"secret file empty: {target}")
    return value or None


def safe_http_url_from_strm(content: str) -> str:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("STRM must contain exactly one non-empty URL")
    parts = urlsplit(lines[0])
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("STRM URL must be an absolute HTTP(S) URL")
    return lines[0]


def contained(path: str | Path, allowed_root: str | Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(allowed_root).resolve(strict=False))
        return True
    except ValueError:
        return False
