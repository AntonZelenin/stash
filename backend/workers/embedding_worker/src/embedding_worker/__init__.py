"""Embedding worker: consumes `EMBEDDING_JOBS` (published by the API and the
analyzers when an item's searchable text changes) and stores each item's
embedding for semantic search."""
