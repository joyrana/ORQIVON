"""Upload validation. Runs before anything is stored or parsed.

DOCX is a zip archive, so it is inspected as one: entry count, total uncompressed size, compression
ratio (zip bombs), entry names (traversal), required parts, and macro payloads are all checked
without extracting anything to disk. XML parsing itself (python-docx/lxml) runs with entity
resolution disabled and no network access.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any, Literal

from agentic_sdlc.core.errors import ValidationFailed
from agentic_sdlc.core.settings import get_settings

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MEDIA_TYPE = "text/plain"
PDF_MEDIA_TYPE = "application/pdf"
MAX_PDF_PAGES = 500
MAX_RATIO = 100  # uncompressed/compressed per entry


@dataclass
class ValidatedUpload:
    format: Literal["docx", "text", "pdf"]
    media_type: str
    warnings: list[str] = field(default_factory=list)


def _validate_docx(data: bytes) -> ValidatedUpload:
    s = get_settings()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("file is not a valid DOCX (corrupt archive)") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > s.max_docx_entries:
            raise ValidationFailed("DOCX contains too many parts", details={"limit": s.max_docx_entries})
        total = 0
        names = set()
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                raise ValidationFailed("DOCX contains an unsafe path", details={"entry": name[:100]})
            if info.flag_bits & 0x1:
                raise ValidationFailed("encrypted DOCX files are not supported")
            total += info.file_size
            if (
                info.compress_size
                and info.file_size / info.compress_size > MAX_RATIO
                and info.file_size > 1_000_000
            ):
                raise ValidationFailed(
                    "DOCX part has a suspicious compression ratio", details={"entry": name[:100]}
                )
            names.add(name)
        if total > s.max_docx_uncompressed_bytes:
            raise ValidationFailed(
                "DOCX expands beyond the size limit", details={"limit_bytes": s.max_docx_uncompressed_bytes}
            )
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise ValidationFailed("file is not a Word document (missing required parts)")
        warnings = []
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise ValidationFailed("macro-enabled documents are not accepted")
        if any(n.startswith("word/embeddings/") for n in names):
            warnings.append("Embedded objects were ignored.")
        if any(n.startswith("word/media/") for n in names):
            warnings.append("Images were ignored; only text, lists and tables are extracted.")
    return ValidatedUpload(format="docx", media_type=DOCX_MEDIA_TYPE, warnings=warnings)


def _validate_text(data: bytes) -> ValidatedUpload:
    if b"\x00" in data:
        raise ValidationFailed("text file contains binary data")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("text file must be UTF-8") from exc
    if not text.strip():
        raise ValidationFailed("file is empty")
    return ValidatedUpload(format="text", media_type=TEXT_MEDIA_TYPE)


def _validate_pdf(data: bytes) -> ValidatedUpload:
    """Structure, encryption and size checks. Active content is never executed (text is only read)."""
    import io

    import pypdf
    from pypdf.errors import PdfReadError

    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValidationFailed("encrypted PDFs are not supported; remove the password and upload again")
        pages = len(reader.pages)
        catalog: Any = reader.trailer["/Root"].get_object()
    except ValidationFailed:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ValidationFailed("file is not a valid PDF") from exc
    if pages == 0:
        raise ValidationFailed("the PDF has no pages")
    if pages > MAX_PDF_PAGES:
        raise ValidationFailed("the PDF has too many pages", details={"limit": MAX_PDF_PAGES})
    warnings = []
    names_ref: Any = catalog.get("/Names")
    names: Any = names_ref.get_object() if names_ref is not None else None
    if "/OpenAction" in catalog or "/AA" in catalog or (names and "/JavaScript" in names):
        warnings.append("The PDF contains scripts or actions; they were ignored (only text is read).")
    if names and "/EmbeddedFiles" in names:
        warnings.append("Attached files inside the PDF were ignored.")
    return ValidatedUpload(format="pdf", media_type=PDF_MEDIA_TYPE, warnings=warnings)


def validate_upload(filename: str, data: bytes) -> ValidatedUpload:
    s = get_settings()
    if not data:
        raise ValidationFailed("file is empty")
    if len(data) > s.max_upload_bytes:
        raise ValidationFailed("file is too large", details={"limit_bytes": s.max_upload_bytes})
    lower = filename.lower()
    if data[:4] == b"PK\x03\x04":
        if not lower.endswith(".docx"):
            raise ValidationFailed("only .docx archives are accepted")
        return _validate_docx(data)
    if data[:5] == b"%PDF-" or lower.endswith(".pdf"):
        if not (lower.endswith(".pdf") and data[:5] == b"%PDF-"):
            raise ValidationFailed("file is not a valid PDF")
        return _validate_pdf(data)
    if lower.endswith((".txt", ".md", ".vtt")):
        return _validate_text(data)
    raise ValidationFailed("unsupported file type; upload .docx, .pdf, .txt, .md or .vtt")
