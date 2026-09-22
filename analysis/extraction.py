"""TXT / DOCX extraction and content normalisation.

The normalised text is what gets hashed for dedup and handed to the
analysers, so the normalisation rule is deliberately simple and stable.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata

TXT_MIME = "text/plain"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_EXTENSIONS = {".txt", ".docx"}

MAX_FILE_BYTES = 10 * 1024 * 1024


class UploadError(Exception):
    """Rejection at the upload boundary (never becomes a queued task)."""


def extract_text(filename: str, raw: bytes, content_type: str = "") -> str:
    """Extract plain text from an uploaded TXT or DOCX byte string.

    Raises UploadError for unsupported types, empty payloads or corrupt
    archives. DOCX parsing errors are surfaced verbatim enough to locate the
    file, but never leak raw bytes back to the API.
    """
    name = filename.lower()
    is_docx = name.endswith(".docx") or content_type == DOCX_MIME
    is_txt = name.endswith(".txt") or (content_type == TXT_MIME and not name.endswith(".docx"))

    if is_docx:
        return _extract_docx(raw, filename)
    if is_txt:
        return _extract_txt(raw, filename)
    raise UploadError(f"unsupported file type: {filename!r} (only .txt and .docx)")


def _extract_txt(raw: bytes, filename: str) -> str:
    # Decode tolerant UTF-8 with BOM support; fall back to latin-1 so odd
    # legacy exports still produce text rather than failing the batch.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    if not text.strip():
        raise UploadError(f"empty text file: {filename!r}")
    return text


def _extract_docx(raw: bytes, filename: str) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency always present
        raise UploadError("DOCX support unavailable (python-docx not installed)") from exc

    try:
        document = docx.Document(io.BytesIO(raw))
    except Exception as exc:
        raise UploadError(f"unreadable DOCX file {filename!r}: {type(exc).__name__}") from exc

    paragraphs = [p.text for p in document.paragraphs]
    # Tables are text too.
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.append(cell.text)
    text = "\n".join(paragraphs)
    if not text.strip():
        raise UploadError(f"DOCX file contains no text: {filename!r}")
    return text


_WS_RUN = re.compile(r"[ \t\f\v]+")


def normalise_text(text: str) -> str:
    """Stable normalisation used for hashing and analysis:

    NFKC unicode normalisation, CRLF -> LF, collapse runs of spaces/tabs,
    strip trailing spaces per line, drop runs of >2 blank lines, trim ends.
    """
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RUN.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank <= 2:
                out.append("")
    return "\n".join(out).strip()


def content_digest(text: str) -> str:
    """SHA-256 hex digest of the normalised text (stored with every result)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
