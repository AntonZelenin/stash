from enum import StrEnum


class ErrorCategory(StrEnum):
    """Why processing failed, for logs, traces and dead letters (never shown
    to users: a failed item is just `failed`). Only `TRANSIENT` is retried."""

    # Infrastructure (storage, database, OpenAI outage...): may succeed on a
    # later attempt.
    TRANSIENT = "TRANSIENT"
    # The file itself can't be processed: corrupt, truncated, encrypted,
    # unsupported, no content.
    MALFORMED_INPUT = "MALFORMED_INPUT"
    # The file went over a processing limit (see `ProcessingLimitExceeded`).
    PROCESSING_LIMIT_EXCEEDED = "PROCESSING_LIMIT_EXCEEDED"
    # Any other failure that can never succeed (missing object, content
    # replaced after validation, rejected by the model...).
    PERMANENT = "PERMANENT"


class PermanentProcessingError(Exception):
    """Processing can never succeed for this item no matter how often it's
    retried (corrupt/unsupported image, missing object, rejected by the
    model...). The worker fails the item and dead-letters the job right away
    instead of spending the remaining attempts.

    Anything else raised during processing is treated as transient and
    retried with backoff.
    """

    category = ErrorCategory.PERMANENT


class MalformedInputError(PermanentProcessingError):
    """The uploaded file can't be processed: corrupt, truncated, encrypted,
    unsupported or empty. Parsing is deterministic, so it's permanent."""

    category = ErrorCategory.MALFORMED_INPUT


class ProcessingLimitExceeded(PermanentProcessingError):
    """Processing went over one of its resource limits (bytes read from
    storage, bytes decompressed, pixels, pages, time...) — limits on how much
    work one file may cost, which are separate from, and much smaller than,
    how large a file may be uploaded. Where some of the file was processed
    before the limit, callers keep that part instead (see e.g. the document
    analyzer); raised all the way, it's permanent: the same file would hit
    the same limit again."""

    category = ErrorCategory.PROCESSING_LIMIT_EXCEEDED


class InputErrorWithheld(Exception):
    """Stands in for an exception a parser or decoder raised on an upload
    (see `content_safe_cause`)."""


def content_safe_cause(exc: BaseException) -> BaseException:
    """What to chain (`raise ... from content_safe_cause(exc)`) instead of
    an exception a parser or decoder raised on an upload: its message may
    quote the file (pypdf quotes the bytes it choked on, `int()` the text it
    couldn't read...), and a chained exception is logged and traced with
    its message. Keeps its type's name and its stack trace (where in the
    parser it failed), not its message nor what it was chained to."""
    stand_in = InputErrorWithheld(f"{type(exc).__module__}.{type(exc).__qualname__} (message withheld)")
    stand_in.__suppress_context__ = True
    return stand_in.with_traceback(exc.__traceback__)


def error_category(exc: BaseException) -> ErrorCategory:
    """The category of a processing failure: a `PermanentProcessingError`'s
    own, anything else is transient."""
    if isinstance(exc, PermanentProcessingError):
        return exc.category
    return ErrorCategory.TRANSIENT
