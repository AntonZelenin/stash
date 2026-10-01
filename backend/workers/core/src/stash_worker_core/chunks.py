"""What the analyzers that describe media as search chunks (images, videos)
share: cleaning up the model's chunks before they're stored one per line
(`stash_shared.descriptions.from_chunks`)."""


def clean_chunks(values: list, seen: set[str]) -> list[str]:
    """`values`' strings with whitespace collapsed (so each fits on one
    line), leaving out empty ones and any already in `seen`
    (case-insensitively), which it adds to. Anything that isn't a string is
    skipped."""
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            cleaned.append(value)
    return cleaned
