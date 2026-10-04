"""pdf_redactor — redact personal data from documents.

Sensitive content is genuinely removed from the produced file (not covered up),
then replaced by a visible truncation pattern. The source file is never
modified and the copy carries a watermark.
"""

__version__ = "2.1.0"

from .backends import (  # noqa: F401
    DocumentError,
    FileReport,
    RedactionOptions,
    UnsupportedFormatError,
    get_backend,
    supported_extensions,
)
from .detectors import Candidate, detect_in_page, merge_candidates  # noqa: F401
from .policy import Policy, PolicyError  # noqa: F401

__all__ = [
    "__version__",
    "Candidate",
    "DocumentError",
    "FileReport",
    "Policy",
    "PolicyError",
    "RedactionOptions",
    "UnsupportedFormatError",
    "detect_in_page",
    "get_backend",
    "merge_candidates",
    "supported_extensions",
]
