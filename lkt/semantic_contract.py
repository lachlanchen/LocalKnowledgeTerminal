"""Shared version identifiers for morphology meaning input and review."""

MORPHOLOGY_MEANING_PROMPT_POLICY = "source-scoped-morphology-meaning-v3"

SEMANTIC_JOB_PROMPT_MARKER = "semantic-enrichment:"


class SemanticContextUnavailable(ValueError):
    """The subject is no longer eligible for semantic enrichment."""

SEMANTIC_BOOK_RETRIEVAL_POLICY = "local-morphology-books-v1"
