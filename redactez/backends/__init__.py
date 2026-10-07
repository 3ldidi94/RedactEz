"""Document backends. Importing this module registers the known formats."""

from .base import (  # noqa: F401
    Backend,
    CorruptedDocumentError,
    DocumentError,
    EncryptedDocumentError,
    FileReport,
    OutputError,
    ProgressCallback,
    RedactionOptions,
    TermReport,
    UnsupportedFormatError,
    get_backend,
    register,
    supported_extensions,
)

# A missing PyMuPDF must not break the whole package: the error is reported
# when a PDF is actually processed.
PDF_IMPORT_ERROR = None
try:
    from . import pdf  # noqa: F401
    from . import image  # noqa: F401  (built on the PDF backend)
except ImportError as exc:  # pragma: no cover
    PDF_IMPORT_ERROR = str(exc)
from . import docx  # noqa: F401,E402  (standard library only)

__all__ = [
    "Backend",
    "CorruptedDocumentError",
    "DocumentError",
    "EncryptedDocumentError",
    "FileReport",
    "OutputError",
    "PDF_IMPORT_ERROR",
    "ProgressCallback",
    "RedactionOptions",
    "TermReport",
    "UnsupportedFormatError",
    "get_backend",
    "register",
    "supported_extensions",
]
