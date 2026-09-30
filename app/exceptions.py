"""Domain errors that map onto HTTP responses.

Routes raise these instead of returning early, which keeps the request handlers
readable; :func:`app.create_app` registers a handler that renders an HTML error
page for browsers and a JSON body for the ``/api`` endpoints.
"""

from __future__ import annotations


class OcrAppError(Exception):
    """Base class for every error the application reports to the user."""

    #: HTTP status used when the error is not caught locally.
    status_code = 400
    #: Machine readable identifier, surfaced in JSON responses.
    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UploadValidationError(OcrAppError):
    """The submitted request/file cannot be accepted."""

    status_code = 400
    code = "invalid_upload"


class MissingFileError(UploadValidationError):
    status_code = 400
    code = "missing_file"


class UnsupportedFileTypeError(UploadValidationError):
    status_code = 400
    code = "unsupported_file_type"


class EmptyFileError(UploadValidationError):
    status_code = 400
    code = "empty_file"


class FileTooLargeError(OcrAppError):
    status_code = 413
    code = "file_too_large"


class CorruptFileError(UploadValidationError):
    """The bytes do not decode as a usable image/PDF."""

    status_code = 400
    code = "corrupt_file"


class EncryptedPdfError(UploadValidationError):
    status_code = 400
    code = "encrypted_pdf"


class PageLimitExceededError(UploadValidationError):
    status_code = 400
    code = "too_many_pages"


class BatchLimitExceededError(UploadValidationError):
    """More files were submitted than one batch may contain."""

    status_code = 400
    code = "too_many_files"



class OcrEngineUnavailableError(OcrAppError):
    """Tesseract is missing or could not be started."""

    status_code = 503
    code = "ocr_engine_unavailable"


class OcrProcessingError(OcrAppError):
    """Tesseract ran but failed / timed out on this document."""

    status_code = 422
    code = "ocr_failed"


class ResultExpiredError(OcrAppError):
    """The cached extraction result is unknown or has been evicted."""

    status_code = 404
    code = "result_not_found"


# ---------------------------------------------------------------------------
# MySQL persistence
# ---------------------------------------------------------------------------
class DatabaseError(OcrAppError):
    """Base class for every MySQL storage problem."""

    status_code = 503
    code = "database_error"


class InvalidDatabaseSettingsError(DatabaseError):
    """The submitted connection details are not usable."""

    status_code = 400
    code = "invalid_database_settings"


class DatabaseNotConfiguredError(DatabaseError):
    """Nothing is connected yet, so there is nowhere to write to."""

    status_code = 400
    code = "database_not_configured"


class DatabaseUnavailableError(DatabaseError):
    """The MySQL driver is missing or the server cannot be reached."""

    status_code = 503
    code = "database_unavailable"


class DatabaseWriteError(DatabaseError):
    """The connection works but the statement failed."""

    status_code = 500
    code = "database_write_failed"


class DatabaseRecordNotFoundError(DatabaseError):
    """The requested stored extraction does not exist any more."""

    status_code = 404
    code = "database_record_not_found"
