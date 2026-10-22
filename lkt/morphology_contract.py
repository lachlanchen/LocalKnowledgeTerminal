"""Stable runtime contract for versioned morphology observations."""

from __future__ import annotations


CURRENT_MORPHOLOGY_PARSER_VERSION = "book-morphology-observation-v7"


def canonical_morpheme_identity(
    language: object, form: object, kind: object
) -> tuple[str, str, str]:
    """Return the role-aware base, display form, and stable morpheme key.

    Edge-hyphen notation is presentation metadata, not part of identity. The
    role remains in the key so supported cross-role homographs stay distinct
    while same-role aliases such as ``pre`` and ``pre-`` coalesce.
    """
    import re
    import unicodedata

    normalized_language = unicodedata.normalize("NFKC", str(language)).strip().casefold()
    normalized_kind = unicodedata.normalize("NFKC", str(kind)).strip().casefold()
    normalized_form = unicodedata.normalize("NFKC", str(form)).strip().casefold()
    normalized_form = re.sub(r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]", "-", normalized_form)
    base = normalized_form.strip("-").strip()
    if not normalized_language or not normalized_kind or not base:
        raise ValueError("language, kind, and morpheme base must be non-empty")
    if normalized_kind == "prefix":
        display = f"{base}-"
    elif normalized_kind == "suffix":
        display = f"-{base}"
    else:
        display = base
    return base, display, f"morpheme:{normalized_language}:{normalized_kind}:{base}"


def canonical_lexical_node_identity(
    subject_entity_id: object,
    entity_type: object,
    language: object,
    form: object,
    *,
    role: object,
    period_label: object = "",
    date_min: object = None,
    date_max: object = None,
) -> tuple[str, ...]:
    """Return a subject-scoped semantic graph identity.

    A lexical graph may contain legacy and current entity IDs for one claim.
    Entity IDs are not semantic identity. Language, role, and historical
    period/date qualifiers are identity, so cross-language or successive
    historical stages and cross-role morpheme homographs remain distinct.
    """
    import re
    import unicodedata

    subject = str(subject_entity_id).strip()
    normalized_type = unicodedata.normalize("NFKC", str(entity_type)).strip().casefold()
    normalized_language = unicodedata.normalize("NFKC", str(language)).strip().casefold()
    normalized_role = unicodedata.normalize("NFKC", str(role)).strip().casefold()
    if not subject or not normalized_type or not normalized_language or not normalized_role:
        raise ValueError("subject, entity type, language, and role must be non-empty")
    if normalized_type == "morpheme":
        normalized_form = canonical_morpheme_identity(
            normalized_language, form, normalized_role
        )[0]
    else:
        normalized_form = unicodedata.normalize("NFKC", str(form)).strip().casefold()
        normalized_form = re.sub(
            r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]", "-", normalized_form
        )
        normalized_form = re.sub(r"\s+", " ", normalized_form)
    if not normalized_form:
        raise ValueError("lexical node form must be non-empty")
    normalized_period = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(period_label)).strip().casefold(),
    )
    return (
        subject,
        normalized_type,
        normalized_language,
        normalized_role,
        normalized_form,
        normalized_period,
        "" if date_min is None else str(date_min).strip(),
        "" if date_max is None else str(date_max).strip(),
    )


def canonical_morpheme_occurrence_identity(
    term: object,
    language: object,
    form: object,
    kind: object,
    ordinal: object,
) -> tuple[str, ...]:
    """Return a stable visible span, falling back to a source occurrence slot."""
    import re
    import unicodedata

    base = canonical_morpheme_identity(language, form, kind)[0]
    normalized_term = unicodedata.normalize("NFKC", str(term)).strip().casefold()
    normalized_term = re.sub(
        r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]", "-", normalized_term
    )
    occurrences = [
        index
        for index in range(max(0, len(normalized_term) - len(base) + 1))
        if normalized_term.startswith(base, index)
    ]
    normalized_kind = str(kind).strip().casefold()
    if normalized_kind == "prefix":
        occurrences = [index for index in occurrences if index == 0]
    elif normalized_kind == "suffix":
        occurrences = [
            index for index in occurrences if index + len(base) == len(normalized_term)
        ]
    if len(occurrences) == 1:
        start = occurrences[0]
        return ("span", str(start), str(start + len(base)))
    try:
        source_ordinal = int(ordinal)
    except (TypeError, ValueError):
        source_ordinal = -1
    return ("ordinal", str(source_ordinal))
