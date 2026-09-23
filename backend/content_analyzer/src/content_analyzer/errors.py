class PermanentProcessingError(Exception):
    """Processing can never succeed for this item no matter how often it's
    retried (corrupt/unsupported image, missing object, rejected by the
    model...). The worker fails the item and dead-letters the job right away
    instead of spending the remaining attempts.

    Anything else raised during processing is treated as transient and
    retried with backoff.
    """
