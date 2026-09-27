class PermanentProcessingError(Exception):
    """Processing can never succeed for this item no matter how often it's
    retried (corrupt/unsupported image, missing object, rejected by the
    model...). The worker fails the item and dead-letters the job right away
    instead of spending the remaining attempts.

    Anything else raised during processing is treated as transient and
    retried with backoff.
    """


class ProcessingLimitExceeded(PermanentProcessingError):
    """Processing went over one of its resource limits (bytes read from
    storage, bytes decompressed, pages, time...) — limits on how much work
    one file may cost, which are separate from, and much smaller than, how
    large a file may be uploaded. Where some of the file was processed
    before the limit, callers keep that part instead (see e.g. the document
    analyzer); raised all the way, it's permanent: the same file would hit
    the same limit again."""
