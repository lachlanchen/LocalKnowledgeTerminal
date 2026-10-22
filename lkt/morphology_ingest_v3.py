from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .morphology_scaffold import (
    _LEXICAL_WORD,
    _identifier,
    _letters,
    _looks_index_only,
    _normalise,
)
from .morphology_contract import (
    CURRENT_MORPHOLOGY_PARSER_VERSION,
    canonical_morpheme_occurrence_identity,
    canonical_morpheme_identity,
)
from .relation_support import (
    RelationSupportSpec,
    ensure_relation_support_schema,
    morphology_source_owner_id,
    replace_relation_support_scope,
)


# Compatibility export for callers of the historically named module.  The
# active value lives in morphology_contract so ingestion, scheduling, and
# health reporting cannot drift independently.
PARSER_VERSION = CURRENT_MORPHOLOGY_PARSER_VERSION
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 100
MATERIALIZABLE_KINDS = frozenset(("root", "prefix", "suffix"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS morphology_ingest_runs (
    run_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    source_manifest_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running', 'complete', 'failed')),
    planned_records INTEGER NOT NULL,
    processed_records INTEGER NOT NULL DEFAULT 0,
    materialized_records INTEGER NOT NULL DEFAULT 0,
    ambiguous_records INTEGER NOT NULL DEFAULT 0,
    invariant_report_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS morphology_ingest_batches (
    run_id TEXT NOT NULL REFERENCES morphology_ingest_runs(run_id),
    batch_index INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('root', 'affix', 'mixed')),
    first_source_entry_id TEXT NOT NULL,
    last_source_entry_id TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK(record_count BETWEEN 1 AND 100),
    state TEXT NOT NULL CHECK(state IN ('running', 'complete', 'failed')),
    counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, batch_index)
);

CREATE TABLE IF NOT EXISTS morphology_source_observations_v3 (
    source_observation_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('root', 'affix')),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    headword TEXT NOT NULL,
    headword_normalized TEXT NOT NULL,
    source_page INTEGER NOT NULL DEFAULT 0,
    locator TEXT NOT NULL DEFAULT '',
    raw_excerpt TEXT NOT NULL,
    raw_fingerprint TEXT NOT NULL,
    parse_state TEXT NOT NULL,
    materialization_state TEXT NOT NULL CHECK(
        materialization_state IN (
            'observed', 'parsed', 'ambiguous', 'materialized', 'blocked'
        )
    ),
    eligibility_reason TEXT NOT NULL DEFAULT '',
    term_count INTEGER NOT NULL DEFAULT 0,
    component_count INTEGER NOT NULL DEFAULT 0,
    formula_link_count INTEGER NOT NULL DEFAULT 0,
    declaration_count INTEGER NOT NULL DEFAULT 0,
    qwen_ready_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(corpus_id, source_entry_id, source_hash, parser_version)
);
CREATE INDEX IF NOT EXISTS idx_morph_source_v3_progress
    ON morphology_source_observations_v3(
        parser_version, source_kind, materialization_state, source_entry_id
    );

CREATE TABLE IF NOT EXISTS morphology_term_observations_v3 (
    term_observation_id TEXT PRIMARY KEY,
    source_observation_id TEXT NOT NULL REFERENCES
        morphology_source_observations_v3(source_observation_id),
    span_fingerprint TEXT NOT NULL,
    clause_index INTEGER NOT NULL,
    clause_text TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    raw_term TEXT NOT NULL,
    normalized_term TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    confidence REAL NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('observed', 'materializable', 'materialized', 'blocked')
    ),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    materialized_entity_id TEXT REFERENCES entities(entity_id),
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_observation_id, span_fingerprint)
);

CREATE TABLE IF NOT EXISTS morphology_component_observations_v3 (
    component_observation_id TEXT PRIMARY KEY,
    source_observation_id TEXT NOT NULL REFERENCES
        morphology_source_observations_v3(source_observation_id),
    span_fingerprint TEXT NOT NULL,
    clause_index INTEGER NOT NULL,
    clause_text TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    raw_form TEXT NOT NULL,
    normalized_form TEXT NOT NULL,
    gloss TEXT NOT NULL DEFAULT '',
    proposed_kind TEXT NOT NULL CHECK(
        proposed_kind IN ('root', 'prefix', 'suffix', 'ambiguous')
    ),
    kind_confidence REAL NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN (
            'observed', 'ambiguous', 'materializable', 'materialized', 'blocked'
        )
    ),
    qwen_ready INTEGER NOT NULL CHECK(qwen_ready IN (0, 1)),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    materialized_entity_id TEXT REFERENCES entities(entity_id),
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_observation_id, span_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_morph_component_v3_work
    ON morphology_component_observations_v3(
        proposed_kind, state, qwen_ready, source_observation_id
    );

CREATE TABLE IF NOT EXISTS morphology_formula_links_v3 (
    formula_link_id TEXT PRIMARY KEY,
    source_observation_id TEXT NOT NULL REFERENCES
        morphology_source_observations_v3(source_observation_id),
    span_fingerprint TEXT NOT NULL,
    clause_index INTEGER NOT NULL,
    term_observation_id TEXT NOT NULL REFERENCES
        morphology_term_observations_v3(term_observation_id),
    component_observation_id TEXT NOT NULL REFERENCES
        morphology_component_observations_v3(component_observation_id),
    surface_relation TEXT NOT NULL CHECK(
        surface_relation IN ('exact', 'explicit-allomorph', 'ambiguous')
    ),
    confidence REAL NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('observed', 'ambiguous', 'materializable', 'materialized', 'blocked')
    ),
    qwen_ready INTEGER NOT NULL CHECK(qwen_ready IN (0, 1)),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_observation_id, span_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_morph_formula_v3_work
    ON morphology_formula_links_v3(state, qwen_ready, source_observation_id);

CREATE TABLE IF NOT EXISTS morphology_term_occurrence_bindings_v3 (
    source_occurrence_id TEXT PRIMARY KEY,
    term_id TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL,
    projection_ordinal INTEGER NOT NULL,
    morpheme_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_morph_occurrence_binding_projection_v3
    ON morphology_term_occurrence_bindings_v3(term_id, projection_ordinal);

CREATE TABLE IF NOT EXISTS morphology_declarations_v3 (
    declaration_id TEXT PRIMARY KEY,
    source_observation_id TEXT NOT NULL REFERENCES
        morphology_source_observations_v3(source_observation_id),
    span_fingerprint TEXT NOT NULL,
    declaration_type TEXT NOT NULL CHECK(
        declaration_type IN (
            'definition', 'source', 'history', 'allomorph', 'opposition'
        )
    ),
    subject_component_observation_id TEXT REFERENCES
        morphology_component_observations_v3(component_observation_id),
    object_value TEXT NOT NULL DEFAULT '',
    object_normalized_form TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    period_label TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL,
    state TEXT NOT NULL CHECK(
        state IN ('observed', 'qwen-ready', 'materializable', 'materialized')
    ),
    qwen_ready INTEGER NOT NULL CHECK(qwen_ready IN (0, 1)),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_observation_id, span_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_morph_declaration_v3_work
    ON morphology_declarations_v3(declaration_type, state, qwen_ready);

CREATE TABLE IF NOT EXISTS morphology_pilot_repairs_v3 (
    repair_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    legacy_edge_id TEXT NOT NULL REFERENCES entity_edges(edge_id),
    legacy_assertion_id TEXT REFERENCES relation_assertions(assertion_id),
    legacy_source_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    legacy_target_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    source_observation_id TEXT REFERENCES
        morphology_source_observations_v3(source_observation_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    action TEXT NOT NULL CHECK(
        action IN (
            'retain', 'repoint', 'archive', 'already-nonaccepted',
            'preserve-v3'
        )
    ),
    reason TEXT NOT NULL,
    replacement_edge_id TEXT REFERENCES entity_edges(edge_id),
    replacement_morpheme_id TEXT REFERENCES entities(entity_id),
    legacy_unknown INTEGER NOT NULL CHECK(legacy_unknown IN (0, 1)),
    forced_nonliteral INTEGER NOT NULL CHECK(forced_nonliteral IN (0, 1)),
    before_status TEXT NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(parser_version, legacy_edge_id)
);
CREATE INDEX IF NOT EXISTS idx_morph_pilot_repair_v3_action
    ON morphology_pilot_repairs_v3(parser_version, action, legacy_edge_id);

CREATE TABLE IF NOT EXISTS morphology_enrichment_clusters_v3 (
    cluster_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    cluster_key TEXT NOT NULL UNIQUE,
    cluster_kind TEXT NOT NULL CHECK(
        cluster_kind IN ('declaration', 'component', 'link')
    ),
    priority_class INTEGER NOT NULL,
    canonical_subject_key TEXT NOT NULL,
    subject_entity_id TEXT REFERENCES entities(entity_id),
    evidence_fingerprint TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(
        status IN ('pending', 'queued', 'running', 'complete', 'cooldown')
    ),
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_job_id TEXT REFERENCES preparation_jobs(job_id),
    last_completed_fingerprint TEXT NOT NULL DEFAULT '',
    last_output_fingerprint TEXT NOT NULL DEFAULT '',
    next_eligible_at TEXT NOT NULL DEFAULT '',
    last_outcome_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_morph_enrichment_v3_next
    ON morphology_enrichment_clusters_v3(
        status, priority_class, next_eligible_at, updated_at
    );
CREATE INDEX IF NOT EXISTS idx_morph_enrichment_v3_last_job
    ON morphology_enrichment_clusters_v3(last_job_id, parser_version);

CREATE TABLE IF NOT EXISTS morphology_enrichment_epochs_v3 (
    epoch_id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL REFERENCES
        morphology_enrichment_clusters_v3(cluster_id),
    epoch_number INTEGER NOT NULL,
    input_fingerprint TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES preparation_jobs(job_id),
    status TEXT NOT NULL CHECK(
        status IN ('queued', 'running', 'complete', 'failed')
    ),
    output_fingerprint TEXT NOT NULL DEFAULT '',
    outcome_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT '',
    UNIQUE(cluster_id, epoch_number),
    UNIQUE(job_id)
);
CREATE INDEX IF NOT EXISTS idx_morph_enrichment_epoch_v3_status
    ON morphology_enrichment_epochs_v3(status, updated_at);

CREATE TABLE IF NOT EXISTS morphology_enrichment_scheduler_v3 (
    scheduler_key TEXT PRIMARY KEY,
    cursor_offset INTEGER NOT NULL DEFAULT 0,
    cycle_number INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CorpusSpec:
    source_kind: str
    path: Path
    corpus_id: str
    source_hash: str
    source_title: str


@dataclass(frozen=True)
class SourceRow:
    source_kind: str
    corpus_id: str
    source_hash: str
    source_title: str
    record_id: str
    headword: str
    source_page: int
    content: str


@dataclass(frozen=True)
class DeclarationSeed:
    raw_form: str
    normalized_form: str
    kind: str
    gloss: str
    confidence: float
    basis: str


@dataclass(frozen=True)
class TermObservation:
    observation_id: str
    span_fingerprint: str
    clause_index: int
    clause_text: str
    span_start: int
    span_end: int
    raw_term: str
    normalized_term: str
    confidence: float
    state: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComponentObservation:
    observation_id: str
    span_fingerprint: str
    clause_index: int
    clause_text: str
    span_start: int
    span_end: int
    raw_form: str
    normalized_form: str
    gloss: str
    proposed_kind: str
    kind_confidence: float
    state: str
    qwen_ready: bool
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FormulaLinkObservation:
    observation_id: str
    span_fingerprint: str
    clause_index: int
    term_observation_id: str
    component_observation_id: str
    surface_relation: str
    confidence: float
    state: str
    qwen_ready: bool
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeclarationObservation:
    observation_id: str
    span_fingerprint: str
    declaration_type: str
    subject_component_observation_id: str
    object_value: str
    object_normalized_form: str
    language: str
    period_label: str
    confidence: float
    state: str
    qwen_ready: bool
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedRecordV3:
    source: SourceRow
    source_observation_id: str
    raw_fingerprint: str
    parse_state: str
    eligibility_reason: str
    terms: tuple[TermObservation, ...]
    components: tuple[ComponentObservation, ...]
    links: tuple[FormulaLinkObservation, ...]
    declarations: tuple[DeclarationObservation, ...]

    @property
    def qwen_ready_count(self) -> int:
        return sum(item.qwen_ready for item in self.components) + sum(
            item.qwen_ready for item in self.links
        ) + sum(item.qwen_ready for item in self.declarations)


def _normalized_component_glosses(value: str) -> tuple[str, ...]:
    """Return stable clause-local sense atoms without inventing equivalence."""

    normalized: list[str] = []
    for part in re.split(r"[,;/，；／]+", _clean(value)):
        gloss = _normalise(part.strip(" .:'\"’“”"))
        if gloss and gloss not in normalized:
            normalized.append(gloss)
    return tuple(normalized)


class DeclarationLexicon:
    """Book-owned kind evidence used without converting ambiguity to certainty."""

    def __init__(self, seeds: Iterable[DeclarationSeed]):
        self._by_form: dict[str, list[DeclarationSeed]] = collections.defaultdict(list)
        for seed in seeds:
            if seed.kind not in MATERIALIZABLE_KINDS:
                continue
            keys = {_letters(seed.normalized_form)}
            # ``ten<tain`` explicitly declares a surface allomorph and its
            # canonical form.  Both spellings carry the printed role, while
            # unrelated forms merely listed under the same heading do not.
            if "<" in seed.raw_form or "〈" in seed.raw_form:
                keys.update(_letters(part) for part in _form_parts(seed.raw_form))
            for key in sorted(keys - {""}):
                self._by_form[key].append(seed)

    def resolve(self, raw_form: str, gloss: str) -> tuple[str, float, str]:
        parts = _form_parts(raw_form)
        # In printed allomorph notation (for example, ``al<ad``), the final
        # form is the canonical morpheme and the earlier form is its surface
        # realization.  Mixing both forms' declarations can assign the
        # surface form's unrelated role to the canonical component.
        lookup_parts = parts[-1:] if "<" in raw_form or "〈" in raw_form else parts
        candidates: list[DeclarationSeed] = []
        for part in lookup_parts:
            candidates.extend(self._by_form.get(_letters(part), ()))
        if not candidates:
            return "ambiguous", 0.0, "no-declaration-match"
        normalized_glosses = frozenset(_normalized_component_glosses(gloss))
        exact = [
            item
            for item in candidates
            if normalized_glosses
            and (candidate_glosses := frozenset(
                _normalized_component_glosses(item.gloss)
            ))
            and (
                normalized_glosses <= candidate_glosses
                or candidate_glosses <= normalized_glosses
            )
        ]
        if exact:
            exact_kinds = {item.kind for item in exact}
            if len(exact_kinds) == 1:
                return next(iter(exact_kinds)), 0.99, "declaration-form-and-gloss"
            return "ambiguous", 0.0, "conflicting-declaration-kinds"
        kinds = {item.kind for item in candidates}
        if len(kinds) != 1:
            return "ambiguous", 0.0, "conflicting-declaration-kinds"
        declared_senses = {
            frozenset(senses)
            for item in candidates
            if (senses := _normalized_component_glosses(item.gloss))
        }
        if normalized_glosses and declared_senses:
            return (
                "ambiguous",
                0.0,
                (
                    "conflicting-declaration-senses"
                    if len(declared_senses) > 1
                    else "declaration-gloss-mismatch"
                ),
            )
        # An exact canonical-form match to an explicitly typed book
        # declaration with no printed sense to contradict the clause is role
        # evidence, not a model guess.  A printed but different sense stays
        # ambiguous for local RAG + Qwen classification above.
        return next(iter(kinds)), 0.95, "declaration-canonical-form"


_TERM_HEAD = re.compile(
    r"(?<![A-Za-z])(?P<term>[A-Za-z][A-Za-z'’-]{1,48})"
    r"(?:[*＊•·]{0,5})\s*(?=[\[［])"
)
_COMPONENT = re.compile(
    r"(?<![A-Za-z])"
    r"(?P<form>[－-]?[A-Za-z][A-Za-z/／〈<>-]{0,34}[－-]?)\s*"
    r"[（(]\s*[=＝]\s*"
    r"(?P<gloss>[A-Za-z][A-Za-z0-9 ,;/.'’\"-]{0,100})\s*[）)]"
)
_FORM_TOKEN = re.compile(r"[－-]?[A-Za-z][A-Za-z-]{1,30}[－-]?")
_DECLARATION_MARKER = re.compile(
    r"(?P<root>词\s*根|字\s*根|\b(?:word\s+)?root\b)"
    r"|(?P<prefix>前\s*缀|\bprefix\b)"
    r"|(?P<suffix>后\s*缀|\bsuffix\b)",
    re.IGNORECASE,
)
_COMPOSITE_ROLE_HEADING = re.compile(
    r"(?:词\s*根\s*(?:(?:和|与|及|、|[/／&])\s*)?词\s*缀)"
    r"|(?:词\s*缀\s*(?:(?:和|与|及|、|[/／&])\s*)?词\s*根)"
    r"|(?:\b(?:word\s+)?roots?\s*(?:and|or|[/&])\s*"
    r"(?:affixes?|prefixes?|suffixes?)\b)"
    r"|(?:\b(?:affixes?|prefixes?|suffixes?)\s*(?:and|or|[/&])\s*"
    r"(?:word\s+)?roots?\b)"
    r"|(?:\bprefixes?\s*(?:and|or|[/&])\s*suffixes?\b)",
    re.IGNORECASE,
)
_SOURCE = re.compile(r"(?:来源于|来自)\s*([^，。；;]{1,100})")
_MEANING = re.compile(r"意为\s*[＂\"“]?([^。；;＂\"”]{1,180})")
_ALLOMORPH_ARROW = re.compile(
    r"(?<![A-Za-z])([A-Za-z][A-Za-z-]{0,24})\s*"
    r"(?:→|变成|变为)\s*([A-Za-z][A-Za-z-]{0,24})(?![A-Za-z])"
)
_OPPOSITION = re.compile(
    r"(?:反义(?:前缀|后缀|词根)?(?:是|为)\s*"
    r"|(?:与|和)\s*)([A-Za-z][A-Za-z-]{1,24})"
    r"(?:\s*意思相反)?",
    re.IGNORECASE,
)
_HISTORY_MARKERS = (
    "来源于",
    "来自",
    "脱落",
    "变成",
    "变为",
    "派生",
    "演变",
    "变体",
    "缩写",
    "之前",
)
_LANGUAGE_LABELS = (
    ("盎格鲁-撒克逊", "ang"),
    ("盎格鲁－撒克逊", "ang"),
    ("古法语", "fro"),
    ("拉丁语", "la"),
    ("希腊语", "grc"),
    ("法语", "fr"),
    ("英语", "en"),
)
_DECLARATION_STOPWORDS = frozenset(
    (
        "affix",
        "away",
        "from",
        "greek",
        "intensive",
        "latin",
        "lecture",
        "meaning",
        "prefix",
        "root",
        "source",
        "suffix",
        "word",
    )
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def _clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _declaration_markers(text: str) -> tuple[re.Match[str], ...]:
    """Return role declarations, excluding generic morphology headings."""

    composite_spans = tuple(
        (match.start(), match.end()) for match in _COMPOSITE_ROLE_HEADING.finditer(text)
    )
    return tuple(
        marker
        for marker in _DECLARATION_MARKER.finditer(text)
        if not any(
            start <= marker.start() and marker.end() <= end
            for start, end in composite_spans
        )
    )


def _de_tex(value: str) -> str:
    text = _clean(value)
    wrapped = re.compile(
        r"\\(?:operatorname|mathrm|mathbf|boldsymbol|text)\s*"
        r"\{\s*([^{}]{1,80})\s*\}"
    )
    for _ in range(4):
        replaced = wrapped.sub(r"\1", text)
        if replaced == text:
            break
        text = replaced
    text = re.sub(r"\\(?:left|right|overline|quad|cdot|textcircled)\b", " ", text)
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = text.replace("\\(", " ").replace("\\)", " ")
    return " ".join(text.split())


def _form_parts(raw_form: str) -> tuple[str, ...]:
    parts = []
    for value in re.split(r"[/／〈<>]", raw_form):
        letters = "".join(re.findall(r"[A-Za-z]+", value)).casefold()
        if letters and letters not in parts:
            parts.append(letters)
    return tuple(parts)


def _canonical_form(raw_form: str, kind: str) -> str:
    parts = _form_parts(raw_form)
    base = parts[-1] if "〈" in raw_form or "<" in raw_form else (
        parts[0] if parts else ""
    )
    if not base:
        return ""
    if kind == "prefix":
        return f"{base}-"
    if kind == "suffix":
        return f"-{base}"
    return base


def _declared_header_form(
    source: SourceRow,
    raw_form: str,
    declared_parts: Iterable[str] = (),
) -> bool:
    """Limit a role heading to its headword, allomorph, or marked list item."""

    headword_parts = {
        value
        for value in (
            *(_form_parts(source.headword)),
            *(_letters(token) for token in _FORM_TOKEN.findall(source.headword)),
        )
        if value
    }
    headword_parts.update(str(value) for value in declared_parts if str(value))
    raw_parts = {value for value in _form_parts(raw_form) if value}
    stripped = raw_form.strip()
    explicitly_marked = stripped.startswith(("-", "－")) or stripped.endswith(
        ("-", "－")
    )
    return bool(headword_parts & raw_parts) or explicitly_marked


def _role_matches_term(kind: str, raw_form: str, term: str) -> bool:
    """Require an affix role to agree with its observed word position."""

    word = _letters(term)
    parts = tuple(part for part in _form_parts(raw_form) if part)
    if not word or not parts:
        return False
    if kind == "prefix":
        return any(word.startswith(part) for part in parts)
    if kind == "suffix":
        return any(word.endswith(part) for part in parts)
    return True


def _explicit_kind(raw_form: str, context: str) -> tuple[str, float, str]:
    stripped = raw_form.strip()
    if stripped.startswith(("-", "－")) and not stripped.endswith(("-", "－")):
        return "suffix", 0.99, "printed-leading-hyphen"
    if stripped.endswith(("-", "－")) and not stripped.startswith(("-", "－")):
        return "prefix", 0.99, "printed-trailing-hyphen"
    markers = _declaration_markers(context)
    marker = markers[-1] if markers else None
    # A role marker governs a formula only when it is immediately adjacent.
    # Previously any marker in a 140-character window, including the generic
    # heading ``词根词缀``, stamped every component as a root.
    if marker and not re.search(r"[A-Za-z0-9]", context[marker.end() :]):
        kind = "root" if marker.group("root") else (
            "prefix" if marker.group("prefix") else "suffix"
        )
        return kind, 0.99, "printed-declaration"
    return "ambiguous", 0.0, "no-explicit-kind"


def _language_from_text(value: str) -> tuple[str, str]:
    for label, code in _LANGUAGE_LABELS:
        if label in value:
            return code, label
    return "", value.strip()


def _source_observation_id(source: SourceRow) -> str:
    return _identifier(
        "morph-source-observation",
        (
            f"{source.corpus_id}\0{source.record_id}\0{source.source_hash}\0"
            f"{PARSER_VERSION}"
        ),
    )


def _span_id(prefix: str, source_observation_id: str, fingerprint: str) -> str:
    return _identifier(prefix, f"{source_observation_id}\0{fingerprint}")


def _extract_gloss(text: str) -> str:
    match = _MEANING.search(text)
    if match:
        return " ".join(match.group(1).split()).strip(" ,.;，")
    return ""


def extract_declaration_seeds(source: SourceRow) -> tuple[DeclarationSeed, ...]:
    """Read explicit role declarations without treating index rows as claims."""

    text = _de_tex(source.content)
    markers = list(_declaration_markers(text))
    if _looks_index_only(text) and not markers:
        return ()
    seeds: list[DeclarationSeed] = []
    default_gloss = _extract_gloss(text)
    for index, marker in enumerate(markers):
        kind = "root" if marker.group("root") else (
            "prefix" if marker.group("prefix") else "suffix"
        )
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = text[marker.end() : end]
        # Dictionary rows commonly place a compact declaration list directly
        # before several bracketed example entries, without punctuation.  A
        # role heading governs only that list; it must never leak into an
        # example such as ``assiduous[...] as<ad(=to,near)``.
        example = _TERM_HEAD.search(segment)
        if example:
            segment = segment[: example.start()]
        header = re.split(
            r"来源于|来自|意为|(?:例词|例句) *[:：]?|\bexamples?\b|[。；;]",
            segment[:260],
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        primary_declaration = _FORM_TOKEN.search(header)
        declared_parts = (
            _form_parts(primary_declaration.group(0))
            if primary_declaration is not None
            else ()
        )
        # Only formulae in the declaration header inherit its role.  Example
        # decompositions later in the entry have their own component roles.
        all_formula_matches = list(_COMPONENT.finditer(header))
        formula_matches = [
            formula
            for formula in all_formula_matches
            if _declared_header_form(
                source, formula.group("form"), declared_parts
            )
        ]
        for formula in formula_matches:
            raw = formula.group("form")
            canonical = _canonical_form(raw, kind)
            if canonical:
                seeds.append(
                    DeclarationSeed(
                        raw, canonical, kind,
                        " ".join(formula.group("gloss").split()).strip(" ,.;"),
                        1.0, "explicit-declaration-formula",
                    )
                )
        # Plain-form declarations are useful (``词根 LIG``), but scanning
        # inside parsed formula spans would promote their gloss words and the
        # surface half of ``ten<tain`` as independent declarations.
        formula_declared_parts = {
            part
            for formula in formula_matches
            for part in _form_parts(formula.group("form"))
        }
        plain_header = list(header)
        for formula in all_formula_matches:
            plain_header[formula.start() : formula.end()] = [
                " "
            ] * (formula.end() - formula.start())
        for token in _FORM_TOKEN.findall("".join(plain_header)):
            word = _letters(token)
            if (
                not word
                or word in _DECLARATION_STOPWORDS
                or word in formula_declared_parts
                or not _declared_header_form(source, token, declared_parts)
            ):
                continue
            canonical = _canonical_form(token, kind)
            if canonical:
                seeds.append(
                    DeclarationSeed(
                        token, canonical, kind, default_gloss, 1.0,
                        "explicit-declaration-header",
                    )
                )

    deduped: dict[tuple[str, str, str], DeclarationSeed] = {}
    for seed in seeds:
        key = (seed.kind, _normalise(seed.normalized_form), _normalise(seed.gloss))
        deduped.setdefault(key, seed)
    return tuple(deduped.values())[:32]


def build_declaration_lexicon(rows: Iterable[SourceRow]) -> DeclarationLexicon:
    return DeclarationLexicon(
        seed for row in rows for seed in extract_declaration_seeds(row)
    )


def _term_clauses(source: SourceRow, text: str) -> list[tuple[int, str, int, int, str]]:
    anchors: list[tuple[int, str]] = []
    if (
        _LEXICAL_WORD.fullmatch(source.headword.strip())
        and not source.headword.strip().isupper()
        and _normalise(text).startswith(_normalise(source.headword))
    ):
        anchors.append((0, source.headword.strip()))
    for match in _TERM_HEAD.finditer(text):
        term = match.group("term").strip()
        if _LEXICAL_WORD.fullmatch(term):
            anchors.append((match.start("term"), term))
    deduped: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for anchor in sorted(anchors, key=lambda item: (item[0], _normalise(item[1]))):
        key = (anchor[0], _normalise(anchor[1]))
        if key not in seen:
            seen.add(key)
            deduped.append(anchor)
    if not deduped and _LEXICAL_WORD.fullmatch(source.headword.strip()):
        if _COMPONENT.search(_de_tex(text)):
            deduped.append((0, source.headword.strip()))
    clauses = []
    for index, (start, term) in enumerate(deduped):
        end = deduped[index + 1][0] if index + 1 < len(deduped) else len(text)
        clauses.append((index, term, start, end, text[start:end].strip()))
    return clauses


def _declaration_observations(
    source: SourceRow,
    source_observation_id: str,
    text: str,
    declared_components: Sequence[ComponentObservation],
    seeds: Sequence[DeclarationSeed],
) -> list[DeclarationObservation]:
    observations: list[DeclarationObservation] = []
    by_letters = {
        _letters(item.normalized_form): item.observation_id
        for item in declared_components
    }
    subject_id = declared_components[0].observation_id if declared_components else ""

    def add(
        declaration_type: str,
        object_value: str,
        *,
        object_form: str = "",
        language: str = "",
        confidence: float = 0.95,
        qwen_ready: bool = False,
        subject: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        resolved_subject = subject_id if subject is None else subject
        fingerprint = _fingerprint(
            source.record_id,
            declaration_type,
            resolved_subject,
            object_value,
            object_form,
            language,
        )
        observations.append(
            DeclarationObservation(
                _span_id("morph-declaration", source_observation_id, fingerprint),
                fingerprint,
                declaration_type,
                resolved_subject,
                object_value,
                object_form,
                language,
                "",
                confidence,
                "qwen-ready" if qwen_ready else "materializable",
                qwen_ready,
                properties or {},
            )
        )

    gloss = _extract_gloss(text)
    if gloss:
        for component in declared_components:
            add(
                "definition", gloss, confidence=1.0,
                subject=component.observation_id,
                properties={"basis": "printed-meaning"},
            )
    source_match = _SOURCE.search(text)
    if source_match:
        raw_source = " ".join(source_match.group(1).split()).strip(" ,.;，")
        language, label = _language_from_text(raw_source)
        add(
            "source", raw_source, language=language, confidence=0.96,
            qwen_ready=not bool(language),
            properties={"source_language_label": label},
        )
    if any(marker in text for marker in _HISTORY_MARKERS):
        add(
            "history", text[:700], confidence=0.9, qwen_ready=True,
            properties={"task": "parse-history-sequence"},
        )
    for match in _ALLOMORPH_ARROW.finditer(text):
        left, right = match.group(1), match.group(2)
        left_subject = by_letters.get(_letters(left), "")
        add(
            "allomorph", right, object_form=_letters(right),
            confidence=0.99 if left_subject else 0.8,
            qwen_ready=not bool(left_subject),
            subject=left_subject,
            properties={
                "raw_left": left,
                "raw_right": right,
                "basis": "printed-arrow",
                "task": "resolve-allomorph-subject" if not left_subject else "",
            },
        )
    for seed in seeds:
        if "<" not in seed.raw_form and "〈" not in seed.raw_form:
            continue
        parts = _form_parts(seed.raw_form)
        if len(parts) < 2:
            continue
        canonical = parts[-1]
        canonical_subject = by_letters.get(_letters(seed.normalized_form), "")
        for variant in parts[:-1]:
            add(
                "allomorph", variant,
                object_form=_letters(variant),
                confidence=0.99 if canonical_subject else 0.8,
                qwen_ready=not bool(canonical_subject),
                subject=canonical_subject,
                properties={
                    "basis": "inline-explicit-allomorph",
                    "raw_form": seed.raw_form,
                    "canonical_form": canonical,
                    "surface_form": variant,
                    "task": (
                        "resolve-allomorph-subject" if not canonical_subject else ""
                    ),
                },
            )
    for match in _OPPOSITION.finditer(text):
        target = match.group(1)
        if _letters(target):
            add(
                "opposition", target, object_form=_letters(target),
                confidence=0.9, qwen_ready=True,
                properties={"task": "resolve-opposed-morpheme"},
            )
    deduped: dict[str, DeclarationObservation] = {}
    for item in observations:
        deduped.setdefault(item.span_fingerprint, item)
    return list(deduped.values())


def parse_source_record(
    source: SourceRow, lexicon: DeclarationLexicon
) -> ParsedRecordV3:
    text = _clean(source.content)
    formula_text = _de_tex(text)
    source_observation_id = _source_observation_id(source)
    seeds = extract_declaration_seeds(source)
    terms: list[TermObservation] = []
    components: list[ComponentObservation] = []
    links: list[FormulaLinkObservation] = []

    for ordinal, seed in enumerate(seeds):
        fingerprint = _fingerprint(
            source.record_id, "declaration-component", ordinal, seed.raw_form,
            seed.normalized_form, seed.kind, seed.gloss,
        )
        components.append(
            ComponentObservation(
                _span_id("morph-component-observation", source_observation_id, fingerprint),
                fingerprint,
                -1,
                text[:700],
                0,
                min(len(text), 700),
                seed.raw_form,
                seed.normalized_form,
                seed.gloss,
                seed.kind,
                seed.confidence,
                "materializable",
                False,
                {"basis": seed.basis, "declared": True, "candidate_kinds": [seed.kind]},
            )
        )

    declared_fingerprints = {
        (_letters(item.raw_form), item.proposed_kind, _normalise(item.gloss))
        for item in components
    }
    clauses = _term_clauses(source, formula_text)
    for clause_index, term, start, end, clause in clauses:
        term_fingerprint = _fingerprint(
            source.record_id, "term", clause_index, _normalise(term), start, end
        )
        term_observation = TermObservation(
            _span_id("morph-term-observation", source_observation_id, term_fingerprint),
            term_fingerprint,
            clause_index,
            clause[:900],
            start,
            end,
            term,
            _normalise(term),
            1.0,
            "materializable",
            {"basis": "printed-entry-head", "same_clause_only": True},
        )
        terms.append(term_observation)
        local_formula = _de_tex(clause)
        for formula_ordinal, match in enumerate(_COMPONENT.finditer(local_formula)):
            raw_form = match.group("form").strip()
            gloss = " ".join(match.group("gloss").split()).strip(" ,.;")
            context = local_formula[max(0, match.start() - 140) : match.start()]
            explicit_kind, explicit_confidence, explicit_basis = _explicit_kind(
                raw_form, context
            )
            declared_kind, declared_confidence, declared_basis = lexicon.resolve(
                raw_form, gloss
            )
            declared_role_conflict = (
                declared_kind == "ambiguous"
                and declared_basis == "conflicting-declaration-kinds"
            )
            if declared_role_conflict:
                kind, confidence, basis = (
                    "ambiguous",
                    0.0,
                    declared_basis,
                )
            elif explicit_kind == "ambiguous":
                kind, confidence, basis = (
                    declared_kind,
                    declared_confidence,
                    declared_basis,
                )
            elif declared_kind == "ambiguous" or declared_kind == explicit_kind:
                kind, confidence, basis = (
                    explicit_kind,
                    max(explicit_confidence, declared_confidence),
                    explicit_basis,
                )
            else:
                # Two book-derived role claims disagree.  Retain the span for
                # local-model review instead of materializing either role.
                kind, confidence, basis = (
                    "ambiguous",
                    0.0,
                    "conflicting-book-kind-evidence",
                )
            role_candidates = {
                value
                for value in (explicit_kind, declared_kind, kind)
                if value in MATERIALIZABLE_KINDS
            }
            if (
                explicit_kind in {"prefix", "suffix"}
                and explicit_basis
                in {"printed-leading-hyphen", "printed-trailing-hyphen"}
                and not _role_matches_term(explicit_kind, raw_form, term)
            ):
                role_candidates.add(explicit_kind)
                kind, confidence, basis = (
                    "ambiguous",
                    0.0,
                    "affix-position-conflict",
                )
            materializable_kind = kind in MATERIALIZABLE_KINDS and confidence >= 0.9
            normalized_form = (
                _canonical_form(raw_form, kind)
                if materializable_kind
                else (_form_parts(raw_form)[-1] if _form_parts(raw_form) else "")
            )
            approx_start = start + max(0, clause.find(match.group("form")))
            approx_end = approx_start + len(match.group(0))
            component_fingerprint = _fingerprint(
                source.record_id,
                "formula-component",
                clause_index,
                formula_ordinal,
                raw_form,
                gloss,
                approx_start,
                approx_end,
            )
            component = ComponentObservation(
                _span_id(
                    "morph-component-observation",
                    source_observation_id,
                    component_fingerprint,
                ),
                component_fingerprint,
                clause_index,
                clause[:900],
                approx_start,
                approx_end,
                raw_form,
                normalized_form,
                gloss,
                kind if materializable_kind else "ambiguous",
                confidence,
                "materializable" if materializable_kind else "ambiguous",
                not materializable_kind,
                {
                    "basis": basis,
                    "candidate_kind": (
                        next(iter(role_candidates))
                        if len(role_candidates) == 1
                        else ""
                    ),
                    "candidate_kinds": (
                        sorted(role_candidates)
                        if role_candidates
                        else ["prefix", "root", "suffix"]
                    ),
                    "component_glosses": list(
                        _normalized_component_glosses(gloss)
                    ),
                    "form_parts": list(_form_parts(raw_form)),
                    "same_clause_only": True,
                    "task": "classify-morpheme-kind" if not materializable_kind else "",
                },
            )
            # A declaration and formula may describe the same claim, but they
            # remain separate span observations and share a canonical entity.
            components.append(component)

            word = _letters(term)
            parts = _form_parts(raw_form)
            matching_parts = [part for part in parts if part and part in word]
            canonical_letters = _letters(normalized_form)
            if canonical_letters and canonical_letters in word:
                relation = "exact"
                link_confidence = min(1.0, confidence or 0.7)
            elif matching_parts and len(parts) > 1:
                relation = "explicit-allomorph"
                link_confidence = min(0.98, confidence or 0.7)
            else:
                relation = "ambiguous"
                link_confidence = min(0.55, confidence or 0.55)
            link_materializable = materializable_kind and relation != "ambiguous"
            link_fingerprint = _fingerprint(
                source.record_id,
                "formula-link",
                clause_index,
                term_observation.observation_id,
                component.observation_id,
                relation,
            )
            links.append(
                FormulaLinkObservation(
                    _span_id("morph-formula-link", source_observation_id, link_fingerprint),
                    link_fingerprint,
                    clause_index,
                    term_observation.observation_id,
                    component.observation_id,
                    relation,
                    link_confidence,
                    "materializable" if link_materializable else "ambiguous",
                    not link_materializable,
                    {
                        "term": term,
                        "component_parts": list(parts),
                        "matching_parts": matching_parts,
                        "same_clause": True,
                        "task": "resolve-formula-link" if not link_materializable else "",
                    },
                )
            )

    declarations = _declaration_observations(
        source, source_observation_id, formula_text,
        [item for item in components if item.clause_index == -1], seeds,
    )
    dedup_terms = {item.span_fingerprint: item for item in terms}
    dedup_components = {item.span_fingerprint: item for item in components}
    dedup_links = {item.span_fingerprint: item for item in links}
    dedup_declarations = {item.span_fingerprint: item for item in declarations}
    terms = list(dedup_terms.values())
    components = list(dedup_components.values())
    links = list(dedup_links.values())
    declarations = list(dedup_declarations.values())
    if links:
        parse_state = "formula-observed"
    elif components:
        parse_state = "declaration-observed"
    elif terms:
        parse_state = "entry-observed"
    else:
        parse_state = "evidence-only"
    if any(item.state == "materializable" for item in components + links):
        eligibility_reason = "typed-book-claim"
    elif any(item.qwen_ready for item in components + links + declarations):
        eligibility_reason = "retained-for-local-qwen"
    else:
        eligibility_reason = "provenance-only"
    return ParsedRecordV3(
        source,
        source_observation_id,
        _fingerprint(source.content),
        parse_state,
        eligibility_reason,
        tuple(terms),
        tuple(components),
        tuple(links),
        tuple(declarations),
    )


def _read_corpus(source_kind: str, path: Path) -> tuple[CorpusSpec, list[SourceRow]]:
    if source_kind not in {"root", "affix"}:
        raise ValueError(f"invalid morphology source kind: {source_kind!r}")
    uri = f"file:{path.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        metadata = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM metadata")
        }
        rows = [
            SourceRow(
                source_kind,
                metadata["corpus_id"],
                metadata["source_sha256"],
                metadata.get("source_title", ""),
                str(row["record_id"]),
                str(row["headword"]),
                int(row["source_page"] or 0),
                str(row["content"]),
            )
            for row in connection.execute(
                """SELECT record_id, headword, source_page, content
                   FROM records ORDER BY row_id"""
            )
        ]
    return (
        CorpusSpec(
            source_kind,
            path.resolve(),
            metadata["corpus_id"],
            metadata["source_sha256"],
            metadata.get("source_title", ""),
        ),
        rows,
    )


def load_and_parse(
    sources: Sequence[tuple[str, Path]],
) -> tuple[list[CorpusSpec], list[ParsedRecordV3]]:
    specs: list[CorpusSpec] = []
    rows: list[SourceRow] = []
    for source_kind, path in sources:
        spec, corpus_rows = _read_corpus(source_kind, path)
        specs.append(spec)
        rows.extend(corpus_rows)
    lexicon = build_declaration_lexicon(rows)
    return specs, [parse_source_record(row, lexicon) for row in rows]


def _entity_keys(parsed: Iterable[ParsedRecordV3]) -> dict[str, set[Any]]:
    keys: dict[str, set[Any]] = {
        "terms": set(),
        "morphemes": set(),
        "meanings": set(),
        "component_links": set(),
    }
    for record in parsed:
        term_by_observation = {
            item.observation_id: _identifier(
                "term", f"term:en:word:{item.normalized_term}"
            )
            for item in record.terms
            if item.state == "materializable"
        }
        component_by_observation: dict[str, str] = {}
        for item in record.components:
            if item.state != "materializable" or item.proposed_kind not in MATERIALIZABLE_KINDS:
                continue
            try:
                _, _, canonical_key = canonical_morpheme_identity(
                    "en", item.normalized_form, item.proposed_kind
                )
            except ValueError:
                continue
            morpheme_id = _identifier(
                "morpheme",
                canonical_key,
            )
            component_by_observation[item.observation_id] = morpheme_id
            keys["morphemes"].add(morpheme_id)
            if item.gloss.strip():
                keys["meanings"].add((morpheme_id, _normalise(item.gloss.strip(" ,.;"))))
        keys["terms"].update(term_by_observation.values())
        for link in record.links:
            if link.state != "materializable":
                continue
            term_id = term_by_observation.get(link.term_observation_id)
            morpheme_id = component_by_observation.get(link.component_observation_id)
            if term_id and morpheme_id:
                keys["component_links"].add((term_id, morpheme_id))
    return keys


def _simulation_summary(
    specs: Sequence[CorpusSpec], parsed: Sequence[ParsedRecordV3]
) -> dict[str, Any]:
    by_kind: dict[str, list[ParsedRecordV3]] = collections.defaultdict(list)
    for record in parsed:
        by_kind[record.source.source_kind].append(record)

    def summarize(records: Sequence[ParsedRecordV3]) -> dict[str, Any]:
        components = [item for record in records for item in record.components]
        links = [item for record in records for item in record.links]
        declarations = [item for record in records for item in record.declarations]
        terms = [item for record in records for item in record.terms]
        entity_keys = _entity_keys(records)
        return {
            "source_observations": len(records),
            "parse_states": dict(collections.Counter(item.parse_state for item in records)),
            "eligibility": dict(
                collections.Counter(item.eligibility_reason for item in records)
            ),
            "term_observations": len(terms),
            "component_observations": len(components),
            "component_states": dict(collections.Counter(item.state for item in components)),
            "proposed_kinds": dict(
                collections.Counter(item.proposed_kind for item in components)
            ),
            "formula_links": len(links),
            "formula_link_states": dict(collections.Counter(item.state for item in links)),
            "surface_relations": dict(
                collections.Counter(item.surface_relation for item in links)
            ),
            "declarations": len(declarations),
            "declaration_types": dict(
                collections.Counter(item.declaration_type for item in declarations)
            ),
            "qwen_ready_observations": sum(item.qwen_ready_count for item in records),
            "rejected_source_rows": 0,
            "materialization_projection": {
                "terms": len(entity_keys["terms"]),
                "morphemes": len(entity_keys["morphemes"]),
                "meanings": len(entity_keys["meanings"]),
                "component_links": len(entity_keys["component_links"]),
            },
        }

    return {
        "dry_run": True,
        "parser_version": PARSER_VERSION,
        "sources": [
            {
                "source_kind": item.source_kind,
                "path": str(item.path),
                "corpus_id": item.corpus_id,
                "source_hash": item.source_hash,
                "source_title": item.source_title,
            }
            for item in specs
        ],
        "combined": summarize(parsed),
        "modes": {key: summarize(value) for key, value in sorted(by_kind.items())},
    }


def _open_knowledge(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _append_values(properties: dict[str, Any], key: str, values: Iterable[str]) -> None:
    existing = properties.get(key, [])
    if not isinstance(existing, list):
        existing = [existing]
    properties[key] = sorted(
        dict.fromkeys(
            str(value).strip()
            for value in (*existing, *values)
            if str(value).strip()
        )
    )


def _ensure_evidence(
    connection: sqlite3.Connection, record: ParsedRecordV3
) -> tuple[str, bool, bool]:
    source = record.source
    locator = f"source page {source.source_page}" if source.source_page else ""
    proposed_id = _identifier(
        "evidence",
        (
            f"morphology-v3:{source.corpus_id}\0{source.record_id}\0"
            f"{source.source_hash}\0{locator}"
        ),
    )
    existing = connection.execute(
        """SELECT evidence_id, excerpt, payload FROM evidence_records
           WHERE corpus_id = ? AND source_entry_id = ?
             AND source_hash = ? AND locator = ?""",
        (source.corpus_id, source.record_id, source.source_hash, locator),
    ).fetchone()
    payload = _json_object(str(existing["payload"])) if existing else {}
    payload.update(
        {
            "entry_id": source.record_id,
            "headword": source.headword,
            "section": f"{source.source_kind.title()} dictionary",
            "pages": [source.source_page] if source.source_page else [],
            "excerpt": source.content,
            "raw_content": source.content,
            "corpus_id": source.corpus_id,
            "source_title": source.source_title,
            "kind": f"morphology-{source.source_kind}",
            "locator": locator,
            "source_hash": source.source_hash,
            "parse_state_v3": record.parse_state,
            "morphology_source_observation_id": record.source_observation_id,
        }
    )
    _append_values(payload, "parser_versions", [PARSER_VERSION])
    evidence_id = str(existing["evidence_id"]) if existing else proposed_id
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    created = existing is None
    changed = bool(
        created
        or str(existing["excerpt"]) != source.content
        or str(existing["payload"]) != payload_json
    )
    if created:
        connection.execute(
            """INSERT INTO evidence_records(
                   evidence_id, corpus_id, source_entry_id, source_hash,
                   locator, excerpt, payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence_id,
                source.corpus_id,
                source.record_id,
                source.source_hash,
                locator,
                source.content,
                payload_json,
            ),
        )
    elif changed:
        connection.execute(
            """UPDATE evidence_records SET excerpt = ?, payload = ?
               WHERE evidence_id = ?""",
            (source.content, payload_json, evidence_id),
        )
    row = connection.execute(
        """SELECT evidence_id FROM evidence_records
           WHERE corpus_id = ? AND source_entry_id = ?
             AND source_hash = ? AND locator = ?""",
        (source.corpus_id, source.record_id, source.source_hash, locator),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"failed to checkpoint evidence for {source.record_id}")
    return str(row[0]), changed, created


def _link_entity_evidence(
    connection: sqlite3.Connection,
    entity_id: str,
    evidence_id: str,
    claim: str,
    confidence: float,
) -> bool:
    cursor = connection.execute(
        """INSERT OR IGNORE INTO entity_evidence(
               entity_id, evidence_id, claim, confidence
           ) VALUES (?, ?, ?, ?)""",
        (entity_id, evidence_id, claim, float(confidence)),
    )
    return bool(cursor.rowcount)


def _calibrated_parser_quality(confidence: float) -> float:
    return round(min(0.99, max(0.0, float(confidence))), 6)


def _is_explicit_typed_declaration(
    item: ComponentObservation,
    declarations: Sequence[DeclarationObservation],
) -> bool:
    if item.proposed_kind not in MATERIALIZABLE_KINDS:
        return False
    basis = str(item.properties.get("basis") or "")
    explicit_provenance = (
        bool(item.properties.get("declared"))
        or basis in {"declaration-form-and-gloss", "printed-declaration"}
    )
    if not explicit_provenance:
        return False
    return any(
        declaration.subject_component_observation_id == item.observation_id
        and declaration.declaration_type in {"definition", "history", "source"}
        and declaration.state in {"materializable", "materialized"}
        and not declaration.qwen_ready
        and bool(declaration.object_value.strip())
        for declaration in declarations
    )


def _retain_or_promote_book_entity(
    connection: sqlite3.Connection,
    entity_id: str,
    timestamp: str,
    *,
    accept: bool,
    confidence: float,
) -> tuple[bool, bool]:
    row = connection.execute(
        "SELECT status, quality_score FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None:
        return False, False
    status = str(row[0])
    if status in {"archived", "rejected", "superseded"}:
        return False, False
    if status == "accepted" or not accept:
        return True, False
    calibrated = _calibrated_parser_quality(confidence)
    cursor = connection.execute(
        """UPDATE entities
           SET status = 'accepted',
               quality_score = MIN(
                   0.99, MAX(COALESCE(quality_score, 0.0), ?)
               ),
               updated_at = ?
           WHERE entity_id = ? AND status = 'draft'""",
        (calibrated, timestamp, entity_id),
    )
    return bool(cursor.rowcount), bool(cursor.rowcount)


def _ensure_term_entity(
    connection: sqlite3.Connection,
    item: TermObservation,
    evidence_id: str,
    timestamp: str,
) -> tuple[str | None, dict[str, int]]:
    counts = collections.Counter()
    entity_id = _identifier("term", f"term:en:word:{item.normalized_term}")
    canonical_key = f"term:en:word:{item.normalized_term}"
    payload = json.dumps(
        {
            "language": "en",
            "kind": "word",
            "acquisition": "book-morphology-v3",
            "parser_version": PARSER_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    entity = connection.execute(
        """INSERT OR IGNORE INTO entities(
               entity_id, entity_type, canonical_key, label, status,
               quality_score, payload, created_at, updated_at
           ) VALUES (?, 'term', ?, ?, 'draft', ?, ?, ?, ?)""",
        (
            entity_id,
            canonical_key,
            item.raw_term,
            _calibrated_parser_quality(item.confidence),
            payload,
            timestamp,
            timestamp,
        ),
    )
    counts["created_entities"] += int(bool(entity.rowcount))
    usable, promoted = _retain_or_promote_book_entity(
        connection,
        entity_id,
        timestamp,
        accept=False,
        confidence=item.confidence,
    )
    counts["promoted_entities"] += int(promoted)
    if not usable:
        return None, dict(counts)
    term = connection.execute(
        """INSERT OR IGNORE INTO terms(
               entity_id, language, text, normalized, kind
           ) VALUES (?, 'en', ?, ?, 'word')""",
        (entity_id, item.raw_term, item.normalized_term),
    )
    counts["created_terms"] += int(bool(term.rowcount))
    evidence_added = _link_entity_evidence(
        connection,
        entity_id,
        evidence_id,
        f"printed morphology entry {item.raw_term!r}",
        item.confidence,
    )
    counts["entity_evidence_added"] += int(evidence_added)
    counts["canonical_changes"] += sum(counts.values())
    return entity_id, dict(counts)


def _ensure_morpheme_entity(
    connection: sqlite3.Connection,
    item: ComponentObservation,
    evidence_id: str,
    timestamp: str,
    *,
    initially_accepted: bool,
) -> tuple[str | None, dict[str, int]]:
    counts = collections.Counter()
    if item.proposed_kind not in MATERIALIZABLE_KINDS:
        raise RuntimeError("v3 refused to materialize an ambiguous morpheme")
    try:
        normalized, form, canonical_key = canonical_morpheme_identity(
            "en", item.normalized_form or item.raw_form, item.proposed_kind
        )
    except ValueError:
        return None, {}
    entity_id = _identifier("morpheme", canonical_key)
    payload = json.dumps(
        {
            "language": "en",
            "kind": item.proposed_kind,
            "meaning": item.gloss,
            "acquisition": "book-morphology-v3",
            "parser_version": PARSER_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    initial_status = "accepted" if initially_accepted else "draft"
    calibrated_quality = _calibrated_parser_quality(item.kind_confidence)
    entity = connection.execute(
        """INSERT OR IGNORE INTO entities(
               entity_id, entity_type, canonical_key, label, status,
               quality_score, payload, created_at, updated_at
           ) VALUES (?, 'morpheme', ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_id,
            canonical_key,
            form,
            initial_status,
            calibrated_quality,
            payload,
            timestamp,
            timestamp,
        ),
    )
    counts["created_entities"] += int(bool(entity.rowcount))
    usable, promoted = _retain_or_promote_book_entity(
        connection,
        entity_id,
        timestamp,
        accept=initially_accepted,
        confidence=item.kind_confidence,
    )
    counts["promoted_entities"] += int(promoted)
    if not usable:
        return None, dict(counts)
    morpheme = connection.execute(
        """INSERT OR IGNORE INTO morphemes(
               entity_id, language, form, normalized, kind, meaning
           ) VALUES (?, 'en', ?, ?, ?, ?)""",
        (entity_id, form, normalized, item.proposed_kind, item.gloss),
    )
    counts["created_morphemes"] += int(bool(morpheme.rowcount))
    evidence_added = _link_entity_evidence(
        connection,
        entity_id,
        evidence_id,
        (
            f"printed {item.proposed_kind} {form!r}"
            + (f" defined as {item.gloss!r}" if item.gloss else "")
        ),
        item.kind_confidence,
    )
    counts["entity_evidence_added"] += int(evidence_added)
    counts["canonical_changes"] += sum(counts.values())
    return entity_id, dict(counts)


def _edge_properties(
    connection: sqlite3.Connection,
    source_entity_id: str,
    target_entity_id: str,
    relation: str,
    additions: dict[str, Any],
) -> tuple[dict[str, Any], sqlite3.Row | None]:
    row = connection.execute(
        """SELECT edge_id, basis, confidence, status, properties
           FROM entity_edges
           WHERE source_entity_id = ? AND target_entity_id = ? AND relation = ?""",
        (source_entity_id, target_entity_id, relation),
    ).fetchone()
    properties = _json_object(str(row["properties"])) if row else {}
    for key, value in additions.items():
        if key.endswith("_ids") or key in {
            "parser_versions", "source_entry_ids", "source_hashes",
            "span_fingerprints", "corpus_ids", "source_kinds",
            "component_glosses",
        }:
            values = value if isinstance(value, list) else [value]
            _append_values(properties, key, values)
        elif key == "clauses":
            values = value if isinstance(value, list) else [value]
            existing = properties.get(key, [])
            if not isinstance(existing, list):
                existing = [existing]
            properties[key] = list(dict.fromkeys((*existing, *values)))[:12]
        else:
            properties[key] = value
    return properties, row


def _upsert_edge_with_evidence(
    connection: sqlite3.Connection,
    *,
    subject_entity_id: str,
    source_entity_id: str,
    target_entity_id: str,
    relation: str,
    evidence_id: str,
    confidence: float,
    properties: dict[str, Any],
    timestamp: str,
) -> tuple[str, dict[str, int]]:
    counts = collections.Counter()
    endpoints = connection.execute(
        """SELECT COUNT(*) FROM entities
           WHERE entity_id IN (?, ?) AND status = 'accepted'""",
        (source_entity_id, target_entity_id),
    ).fetchone()[0]
    if int(endpoints) != 2:
        raise RuntimeError(
            f"v3 refused accepted edge with nonaccepted endpoint: {relation}"
        )
    merged, existing = _edge_properties(
        connection, source_entity_id, target_entity_id, relation, properties
    )
    _append_values(merged, "evidence_ids", [evidence_id])
    merged_json = json.dumps(
        merged,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    edge_id = (
        str(existing["edge_id"])
        if existing
        else _identifier(
            "edge", f"{source_entity_id}\0{target_entity_id}\0{relation}"
        )
    )
    desired_confidence = max(
        float(confidence), float(existing["confidence"]) if existing else 0.0
    )
    edge_changed = bool(
        existing is None
        or str(existing["basis"]) != "book"
        or float(existing["confidence"]) != desired_confidence
        or str(existing["status"]) != "accepted"
        or str(existing["properties"]) != merged_json
    )
    if existing is None:
        connection.execute(
            """INSERT INTO entity_edges(
                   edge_id, source_entity_id, target_entity_id, relation,
                   basis, confidence, status, properties, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'book', ?, 'accepted', ?, ?, ?)""",
            (
                edge_id, source_entity_id, target_entity_id, relation,
                desired_confidence, merged_json, timestamp, timestamp,
            ),
        )
        counts["created_edges"] += 1
    elif edge_changed:
        connection.execute(
            """UPDATE entity_edges
               SET basis = 'book', confidence = ?, status = 'accepted',
                   properties = ?, updated_at = ? WHERE edge_id = ?""",
            (desired_confidence, merged_json, timestamp, edge_id),
        )
        counts["updated_edges"] += 1

    authoritative = connection.execute(
        """SELECT assertion_id, basis, confidence, status, properties_json
           FROM relation_assertions
           WHERE subject_entity_id = ? AND source_entity_id = ?
             AND target_entity_id = ? AND relation = ?""",
        (subject_entity_id, source_entity_id, target_entity_id, relation),
    ).fetchone()
    assertion_id = (
        str(authoritative["assertion_id"])
        if authoritative
        else _identifier(
            "assertion",
            f"{subject_entity_id}\0{source_entity_id}\0{target_entity_id}\0{relation}",
        )
    )
    assertion_confidence = max(
        float(confidence),
        float(authoritative["confidence"]) if authoritative else 0.0,
    )
    assertion_changed = bool(
        authoritative is None
        or str(authoritative["basis"]) != "book"
        or float(authoritative["confidence"]) != assertion_confidence
        or str(authoritative["status"]) != "accepted"
        or str(authoritative["properties_json"]) != merged_json
    )
    if authoritative is None:
        connection.execute(
            """INSERT INTO relation_assertions(
                   assertion_id, subject_entity_id, source_entity_id,
                   target_entity_id, relation, basis, confidence, status,
                   properties_json, revision, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'book', ?, 'accepted', ?, 1, ?, ?)""",
            (
                assertion_id, subject_entity_id, source_entity_id,
                target_entity_id, relation, assertion_confidence, merged_json,
                timestamp, timestamp,
            ),
        )
        counts["created_assertions"] += 1
    elif assertion_changed:
        connection.execute(
            """UPDATE relation_assertions
               SET basis = 'book', confidence = ?, status = 'accepted',
                   properties_json = ?, revision = revision + 1, updated_at = ?
               WHERE assertion_id = ?""",
            (
                assertion_confidence, merged_json, timestamp,
                str(authoritative["assertion_id"]),
            ),
        )
        counts["updated_assertions"] += 1
    evidence = connection.execute(
        """INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id)
           VALUES (?, ?)""",
        (assertion_id, evidence_id),
    )
    counts["assertion_evidence_added"] += int(bool(evidence.rowcount))
    counts["canonical_changes"] += sum(counts.values())
    return assertion_id, dict(counts)


def _archive_superseded_component_roles(
    connection: sqlite3.Connection,
    *,
    source: SourceRow,
    term_id: str,
    desired_morpheme_ids: set[str],
    evidence_id: str,
    timestamp: str,
) -> dict[str, int]:
    """Archive a stale same-record component claim superseded by this parse.

    The correction is deliberately narrow: the current record must no longer
    request the old target, and the old assertion must have no evidence outside
    this record.  A differently typed same-form component records a role
    correction; a form that is now ambiguous or absent records a retraction.
    Independent book support is therefore preserved.
    """

    desired_rows = []
    if desired_morpheme_ids:
        placeholders = ", ".join("?" for _ in desired_morpheme_ids)
        desired_rows = connection.execute(
            f"""SELECT entity_id, form, kind FROM morphemes
                 WHERE entity_id IN ({placeholders})""",
            tuple(sorted(desired_morpheme_ids)),
        ).fetchall()
    desired_by_letters: dict[str, set[tuple[str, str]]] = collections.defaultdict(set)
    for row in desired_rows:
        desired_by_letters[_letters(str(row["form"]))].add(
            (str(row["entity_id"]), str(row["kind"]))
        )

    candidates = connection.execute(
        """SELECT edge.edge_id, edge.target_entity_id, edge.properties,
                  morpheme.form, morpheme.kind,
                  assertion.assertion_id, assertion.properties_json
             FROM entity_edges AS edge
             JOIN morphemes AS morpheme
               ON morpheme.entity_id = edge.target_entity_id
             LEFT JOIN relation_assertions AS assertion
               ON assertion.subject_entity_id = edge.source_entity_id
              AND assertion.source_entity_id = edge.source_entity_id
              AND assertion.target_entity_id = edge.target_entity_id
              AND assertion.relation = edge.relation
              AND assertion.status = 'accepted'
            WHERE edge.source_entity_id = ?
              AND edge.relation = 'has-component'
              AND edge.status = 'accepted'
            ORDER BY edge.edge_id""",
        (term_id,),
    ).fetchall()
    counts = collections.Counter()
    for row in candidates:
        stale_id = str(row["target_entity_id"])
        if stale_id in desired_morpheme_ids:
            continue
        same_form_desired = desired_by_letters.get(_letters(str(row["form"])), set())
        if same_form_desired and str(row["kind"]) in {
            kind for _, kind in same_form_desired
        }:
            continue
        properties = _json_object(str(row["properties"]))

        def values(key: str, scalar_key: str = "") -> set[str]:
            raw = properties.get(key, [])
            raw_values = raw if isinstance(raw, list) else [raw]
            if scalar_key and properties.get(scalar_key):
                raw_values = [*raw_values, properties[scalar_key]]
            return {str(value) for value in raw_values if str(value)}

        parser_versions = values("parser_versions", "parser_version")
        if not any(
            version.startswith("book-morphology-observation-v")
            for version in parser_versions
        ):
            continue
        if source.record_id not in values("source_entry_ids", "source_entry_id"):
            continue
        if source.source_hash not in values("source_hashes", "source_hash"):
            continue
        if values("evidence_ids") != {evidence_id}:
            continue
        assertion_id = str(row["assertion_id"] or "")
        if assertion_id:
            assertion_evidence = {
                str(item[0])
                for item in connection.execute(
                    """SELECT evidence_id FROM assertion_evidence
                       WHERE assertion_id = ?""",
                    (assertion_id,),
                ).fetchall()
            }
            if assertion_evidence - {evidence_id}:
                continue

        replacement_ids = sorted(entity_id for entity_id, _ in same_form_desired)
        correction = {
            "parser_version": PARSER_VERSION,
            "reason": (
                "same-record canonical form has corrected component role"
                if same_form_desired
                else "same-record component is no longer safely materializable"
            ),
            "replacement_morpheme_ids": replacement_ids,
            "source_entry_id": source.record_id,
        }
        properties["role_correction"] = correction
        archived_edge = connection.execute(
            """UPDATE entity_edges
               SET status = 'archived', properties = ?, updated_at = ?
               WHERE edge_id = ? AND status = 'accepted'""",
            (
                json.dumps(properties, ensure_ascii=False, sort_keys=True),
                timestamp,
                str(row["edge_id"]),
            ),
        )
        if not archived_edge.rowcount:
            continue
        counts["superseded_role_edges_archived"] += 1
        if assertion_id:
            assertion_properties = _json_object(str(row["properties_json"] or "{}"))
            assertion_properties["role_correction"] = correction
            archived_assertion = connection.execute(
                """UPDATE relation_assertions
                   SET status = 'archived', properties_json = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE assertion_id = ? AND status = 'accepted'""",
                (
                    json.dumps(
                        assertion_properties, ensure_ascii=False, sort_keys=True
                    ),
                    timestamp,
                    assertion_id,
                ),
            )
            counts["superseded_role_assertions_archived"] += int(
                bool(archived_assertion.rowcount)
            )
        accepted_link = connection.execute(
            """SELECT 1 FROM entity_edges
               WHERE source_entity_id = ? AND target_entity_id = ?
                 AND relation = 'has-component' AND status = 'accepted'
               LIMIT 1""",
            (term_id, stale_id),
        ).fetchone()
        if accepted_link is None:
            removed = connection.execute(
                """DELETE FROM term_morphemes
                   WHERE term_id = ? AND morpheme_id = ?""",
                (term_id, stale_id),
            )
            counts["superseded_normalized_links_removed"] += int(
                bool(removed.rowcount)
            )
    counts["canonical_changes"] = sum(counts.values())
    return dict(counts)


def _ensure_meaning(
    connection: sqlite3.Connection,
    morpheme_id: str,
    item: ComponentObservation,
    evidence_id: str,
    provenance: dict[str, Any],
    timestamp: str,
) -> tuple[str | None, dict[str, int]]:
    counts = collections.Counter()
    definition = " ".join(item.gloss.split()).strip(" ,.;")
    if not definition:
        return None, {}
    normalized = _normalise(definition)
    rows = connection.execute(
        """SELECT m.entity_id, m.definition, m.sense_order, e.status
           FROM meanings m JOIN entities e ON e.entity_id = m.entity_id
           WHERE m.subject_entity_id = ? AND m.language = 'en'
           ORDER BY m.sense_order, m.entity_id""",
        (morpheme_id,),
    ).fetchall()
    match = next((row for row in rows if _normalise(str(row[1])) == normalized), None)
    if match:
        meaning_id = str(match[0])
        if str(match[3]) in {"archived", "superseded"}:
            return None, {}
    else:
        sense_order = max((int(row[2]) for row in rows), default=-1) + 1
        canonical_key = f"meaning:{morpheme_id}:en:{sense_order}:{normalized}"
        meaning_id = _identifier("meaning", canonical_key)
        payload = json.dumps(
            {
                "subject_entity_id": morpheme_id,
                "term_id": None,
                "language": "en",
                "definition": definition,
                "part_of_speech": "",
                "register_label": "",
                "domain_label": "morphology",
                "sense_order": sense_order,
                "provenance": provenance,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        entity = connection.execute(
            """INSERT OR IGNORE INTO entities(
                   entity_id, entity_type, canonical_key, label, status,
                   quality_score, payload, created_at, updated_at
               ) VALUES (?, 'meaning', ?, ?, 'accepted', 1.0, ?, ?, ?)""",
            (meaning_id, canonical_key, definition, payload, timestamp, timestamp),
        )
        counts["created_entities"] += int(bool(entity.rowcount))
        meaning = connection.execute(
            """INSERT OR IGNORE INTO meanings(
                   entity_id, subject_entity_id, term_id, language, definition,
                   part_of_speech, register_label, domain_label, sense_order
               ) VALUES (?, ?, NULL, 'en', ?, '', '', 'morphology', ?)""",
            (meaning_id, morpheme_id, definition, sense_order),
        )
        counts["created_meanings"] += int(bool(meaning.rowcount))
    usable, promoted = _retain_or_promote_book_entity(
        connection,
        meaning_id,
        timestamp,
        accept=True,
        confidence=item.kind_confidence,
    )
    counts["promoted_entities"] += int(promoted)
    if not usable:
        return None, dict(counts)
    evidence_added = _link_entity_evidence(
        connection,
        meaning_id,
        evidence_id,
        f"printed {item.proposed_kind} gloss {definition!r}",
        item.kind_confidence,
    )
    counts["entity_evidence_added"] += int(evidence_added)
    _, edge_counts = _upsert_edge_with_evidence(
        connection,
        subject_entity_id=morpheme_id,
        source_entity_id=morpheme_id,
        target_entity_id=meaning_id,
        relation="has-meaning",
        evidence_id=evidence_id,
        confidence=item.kind_confidence,
        properties={**provenance, "source_observation": True},
        timestamp=timestamp,
    )
    counts.update(edge_counts)
    own_changes = (
        counts["created_entities"]
        + counts["created_meanings"]
        + counts["promoted_entities"]
        + counts["entity_evidence_added"]
    )
    counts["canonical_changes"] += own_changes
    return meaning_id, dict(counts)


def _ensure_term_morpheme(
    connection: sqlite3.Connection,
    term_id: str,
    morpheme_id: str,
    surface: str,
    evidence_id: str,
    source_ordinal: int,
    source_occurrence_id: str = "",
) -> tuple[bool, int]:
    desired = connection.execute(
        """SELECT language,form,normalized,kind FROM morphemes
           WHERE entity_id=?""",
        (morpheme_id,),
    ).fetchone()
    if desired is None:
        raise ValueError(f"missing morpheme projection target: {morpheme_id}")
    desired_identity = canonical_morpheme_identity(
        desired[0], desired[1] or desired[2], desired[3]
    )[:1]
    term = connection.execute(
        "SELECT text,normalized FROM terms WHERE entity_id=?", (term_id,)
    ).fetchone()
    term_form = str(term[0] or term[1]) if term is not None else ""
    desired_occurrence = canonical_morpheme_occurrence_identity(
        term_form, desired[0], desired[1] or desired[2], desired[3], source_ordinal
    )

    def same_semantic_identity(row: sqlite3.Row | tuple[Any, ...]) -> bool:
        try:
            return (
                canonical_morpheme_identity(
                    row[3], row[4] or row[5], row[6]
                )[:1]
                == desired_identity
                and str(row[3]).casefold() == str(desired[0]).casefold()
                and str(row[6]).casefold() == str(desired[3]).casefold()
            )
        except ValueError:
            return False

    def shares_evidence(entity_id: str) -> bool:
        return bool(
            evidence_id
            and connection.execute(
                """SELECT 1 FROM entity_evidence
                   WHERE entity_id=? AND evidence_id=? LIMIT 1""",
                (entity_id, evidence_id),
            ).fetchone()
        )

    def persist_binding(ordinal: int) -> None:
        if not source_occurrence_id:
            return
        connection.execute(
            """INSERT INTO morphology_term_occurrence_bindings_v3(
                   source_occurrence_id,term_id,source_ordinal,
                   projection_ordinal,morpheme_id,evidence_id)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source_occurrence_id) DO UPDATE SET
                   term_id=excluded.term_id,
                   source_ordinal=excluded.source_ordinal,
                   projection_ordinal=excluded.projection_ordinal,
                   morpheme_id=excluded.morpheme_id,
                   evidence_id=excluded.evidence_id""",
            (
                source_occurrence_id,
                term_id,
                source_ordinal,
                ordinal,
                morpheme_id,
                evidence_id,
            ),
        )

    if source_occurrence_id:
        binding = connection.execute(
            """SELECT projection_ordinal
               FROM morphology_term_occurrence_bindings_v3
               WHERE source_occurrence_id=? AND term_id=? AND source_ordinal=?""",
            (source_occurrence_id, term_id, source_ordinal),
        ).fetchone()
        if binding is not None:
            bound = connection.execute(
                """SELECT link.morpheme_id,link.ordinal,link.surface,
                          morpheme.language,morpheme.form,morpheme.normalized,
                          morpheme.kind
                   FROM term_morphemes AS link
                   JOIN morphemes AS morpheme
                     ON morpheme.entity_id=link.morpheme_id
                   WHERE link.term_id=? AND link.ordinal=?""",
                (term_id, int(binding[0])),
            ).fetchone()
            if bound is not None and same_semantic_identity(bound) and (
                str(bound[0]) == morpheme_id or shares_evidence(str(bound[0]))
            ):
                prior_id = str(bound[0])
                ordinal = int(bound[1])
                prior_surface = str(bound[2])
                changed = prior_id != morpheme_id or prior_surface != surface
                if changed:
                    connection.execute(
                        """UPDATE term_morphemes SET morpheme_id=?,surface=?,
                                  confidence=1.0,basis='book'
                           WHERE term_id=? AND ordinal=? AND morpheme_id=?""",
                        (morpheme_id, surface, term_id, ordinal, prior_id),
                    )
                persist_binding(ordinal)
                return changed, ordinal
            connection.execute(
                """DELETE FROM morphology_term_occurrence_bindings_v3
                   WHERE source_occurrence_id=?""",
                (source_occurrence_id,),
            )

    semantic_matches: list[tuple[str, int, str]] = []
    for row in connection.execute(
        """SELECT link.morpheme_id,link.ordinal,link.surface,
                  morpheme.language,morpheme.form,morpheme.normalized,morpheme.kind
           FROM term_morphemes AS link
           JOIN morphemes AS morpheme ON morpheme.entity_id=link.morpheme_id
           WHERE link.term_id=? ORDER BY link.ordinal,link.morpheme_id""",
        (term_id,),
    ):
        if not same_semantic_identity(row):
            continue
        exact = str(row[0]) == morpheme_id
        existing_occurrence = canonical_morpheme_occurrence_identity(
            term_form, row[3], row[4] or row[5], row[6], row[1]
        )
        same_evidence = shares_evidence(str(row[0]))
        if existing_occurrence == desired_occurrence and (exact or same_evidence):
            semantic_matches.append((str(row[0]), int(row[1]), str(row[2])))
    if len(semantic_matches) == 1:
        prior_id, ordinal, prior_surface = semantic_matches[0]
        changed = prior_id != morpheme_id or prior_surface != surface
        if changed:
            connection.execute(
                """UPDATE term_morphemes SET morpheme_id=?,surface=?,
                          confidence=1.0,basis='book'
                   WHERE term_id=? AND ordinal=? AND morpheme_id=?""",
                (morpheme_id, surface, term_id, ordinal, prior_id),
            )
        persist_binding(ordinal)
        return changed, ordinal
    occupied = connection.execute(
        "SELECT 1 FROM term_morphemes WHERE term_id=? AND ordinal=?",
        (term_id, source_ordinal),
    ).fetchone()
    ordinal = source_ordinal if source_ordinal >= 0 and occupied is None else int(
        connection.execute(
            """SELECT COALESCE(MAX(ordinal), -1) + 1
               FROM term_morphemes WHERE term_id = ?""",
            (term_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """INSERT INTO term_morphemes(
               term_id, morpheme_id, ordinal, surface, confidence, basis
           ) VALUES (?, ?, ?, ?, 1.0, 'book')""",
        (term_id, morpheme_id, ordinal, surface),
    )
    persist_binding(ordinal)
    return True, ordinal


def _insert_observation_rows(
    connection: sqlite3.Connection,
    record: ParsedRecordV3,
    evidence_id: str,
    timestamp: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    source = record.source
    locator = f"source page {source.source_page}" if source.source_page else ""
    counts = collections.Counter()
    connection.execute(
        """INSERT INTO morphology_source_observations_v3(
               source_observation_id, corpus_id, source_entry_id, source_hash,
               parser_version, source_kind, evidence_id, headword,
               headword_normalized, source_page, locator, raw_excerpt,
               raw_fingerprint, parse_state, materialization_state,
               eligibility_reason, term_count, component_count,
               formula_link_count, declaration_count, qwen_ready_count,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed',
                     ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(corpus_id, source_entry_id, source_hash, parser_version)
           DO UPDATE SET evidence_id = excluded.evidence_id,
                         headword = excluded.headword,
                         headword_normalized = excluded.headword_normalized,
                         source_page = excluded.source_page,
                         locator = excluded.locator,
                         raw_excerpt = excluded.raw_excerpt,
                         raw_fingerprint = excluded.raw_fingerprint,
                         parse_state = excluded.parse_state,
                         eligibility_reason = excluded.eligibility_reason,
                         term_count = excluded.term_count,
                         component_count = excluded.component_count,
                         formula_link_count = excluded.formula_link_count,
                         declaration_count = excluded.declaration_count,
                         qwen_ready_count = excluded.qwen_ready_count,
                         updated_at = excluded.updated_at""",
        (
            record.source_observation_id,
            source.corpus_id,
            source.record_id,
            source.source_hash,
            PARSER_VERSION,
            source.source_kind,
            evidence_id,
            source.headword,
            _normalise(source.headword),
            source.source_page,
            locator,
            source.content,
            record.raw_fingerprint,
            record.parse_state,
            record.eligibility_reason,
            len(record.terms),
            len(record.components),
            len(record.links),
            len(record.declarations),
            record.qwen_ready_count,
            timestamp,
            timestamp,
        ),
    )
    term_entities: dict[str, str] = {}
    component_entities: dict[str, str] = {}
    for item in record.terms:
        connection.execute(
            """INSERT INTO morphology_term_observations_v3(
                   term_observation_id, source_observation_id, span_fingerprint,
                   clause_index, clause_text, span_start, span_end, raw_term,
                   normalized_term, language, confidence, state, evidence_id,
                   materialized_entity_id, properties_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?, NULL, ?, ?, ?)
               ON CONFLICT(source_observation_id, span_fingerprint)
               DO UPDATE SET clause_text = excluded.clause_text,
                             raw_term = excluded.raw_term,
                             normalized_term = excluded.normalized_term,
                             confidence = excluded.confidence,
                             evidence_id = excluded.evidence_id,
                             properties_json = excluded.properties_json,
                             updated_at = excluded.updated_at""",
            (
                item.observation_id,
                record.source_observation_id,
                item.span_fingerprint,
                item.clause_index,
                item.clause_text,
                item.span_start,
                item.span_end,
                item.raw_term,
                item.normalized_term,
                item.confidence,
                item.state,
                evidence_id,
                json.dumps(item.properties, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        if item.state == "materializable":
            entity_id, growth = _ensure_term_entity(
                connection, item, evidence_id, timestamp
            )
            counts.update(growth)
            if entity_id:
                term_entities[item.observation_id] = entity_id
                connection.execute(
                    """UPDATE morphology_term_observations_v3
                       SET state = 'materialized', materialized_entity_id = ?,
                           updated_at = ? WHERE term_observation_id = ?""",
                    (entity_id, timestamp, item.observation_id),
                )
                counts["materialized_terms"] += 1
            else:
                connection.execute(
                    """UPDATE morphology_term_observations_v3
                       SET state = 'blocked', updated_at = ?
                       WHERE term_observation_id = ?""",
                    (timestamp, item.observation_id),
                )
                counts["blocked_terms"] += 1

    for item in record.components:
        connection.execute(
            """INSERT INTO morphology_component_observations_v3(
                   component_observation_id, source_observation_id,
                   span_fingerprint, clause_index, clause_text, span_start,
                   span_end, raw_form, normalized_form, gloss, proposed_kind,
                   kind_confidence, state, qwen_ready, evidence_id,
                   materialized_entity_id, properties_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
               ON CONFLICT(source_observation_id, span_fingerprint)
               DO UPDATE SET clause_text = excluded.clause_text,
                             raw_form = excluded.raw_form,
                             normalized_form = excluded.normalized_form,
                             gloss = excluded.gloss,
                             proposed_kind = excluded.proposed_kind,
                             kind_confidence = excluded.kind_confidence,
                             qwen_ready = excluded.qwen_ready,
                             evidence_id = excluded.evidence_id,
                             properties_json = excluded.properties_json,
                             updated_at = excluded.updated_at""",
            (
                item.observation_id,
                record.source_observation_id,
                item.span_fingerprint,
                item.clause_index,
                item.clause_text,
                item.span_start,
                item.span_end,
                item.raw_form,
                item.normalized_form,
                item.gloss,
                item.proposed_kind,
                item.kind_confidence,
                item.state,
                int(item.qwen_ready),
                evidence_id,
                json.dumps(item.properties, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        if item.state != "materializable":
            counts["ambiguous_components"] += 1
            continue
        standalone_accepted = _is_explicit_typed_declaration(
            item, record.declarations
        )
        entity_id, growth = _ensure_morpheme_entity(
            connection,
            item,
            evidence_id,
            timestamp,
            initially_accepted=standalone_accepted,
        )
        counts.update(growth)
        if not entity_id:
            connection.execute(
                """UPDATE morphology_component_observations_v3
                   SET state = 'blocked', updated_at = ?
                   WHERE component_observation_id = ?""",
                (timestamp, item.observation_id),
            )
            counts["blocked_components"] += 1
            continue
        component_entities[item.observation_id] = entity_id
        connection.execute(
            """UPDATE morphology_component_observations_v3
               SET state = 'materialized', materialized_entity_id = ?,
                   updated_at = ? WHERE component_observation_id = ?""",
            (entity_id, timestamp, item.observation_id),
        )
        counts["materialized_components"] += 1
        provenance = {
            "acquisition": "book-morphology-v3",
            "parser_versions": [PARSER_VERSION],
            "source_kinds": [source.source_kind],
            "corpus_ids": [source.corpus_id],
            "source_entry_ids": [source.record_id],
            "source_hashes": [source.source_hash],
            "span_fingerprints": [item.span_fingerprint],
            "evidence_ids": [evidence_id],
            "source_observation": True,
        }
        if standalone_accepted:
            meaning_id, growth = _ensure_meaning(
                connection, entity_id, item, evidence_id, provenance, timestamp
            )
            counts.update(growth)
            counts["materialized_meanings"] += int(bool(meaning_id))

    term_observations = {item.observation_id: item for item in record.terms}
    component_observations = {
        item.observation_id: item for item in record.components
    }
    record_supports: list[RelationSupportSpec] = []
    for item in record.links:
        connection.execute(
            """INSERT INTO morphology_formula_links_v3(
                   formula_link_id, source_observation_id, span_fingerprint,
                   clause_index, term_observation_id, component_observation_id,
                   surface_relation, confidence, state, qwen_ready, evidence_id,
                   properties_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_observation_id, span_fingerprint)
               DO UPDATE SET surface_relation = excluded.surface_relation,
                             confidence = excluded.confidence,
                             qwen_ready = excluded.qwen_ready,
                             evidence_id = excluded.evidence_id,
                             properties_json = excluded.properties_json,
                             updated_at = excluded.updated_at""",
            (
                item.observation_id,
                record.source_observation_id,
                item.span_fingerprint,
                item.clause_index,
                item.term_observation_id,
                item.component_observation_id,
                item.surface_relation,
                item.confidence,
                item.state,
                int(item.qwen_ready),
                evidence_id,
                json.dumps(item.properties, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        if item.state != "materializable":
            counts["ambiguous_links"] += 1
            continue
        term_observation = term_observations.get(item.term_observation_id)
        component_observation = component_observations.get(
            item.component_observation_id
        )
        if (
            term_observation is None
            or component_observation is None
            or item.clause_index != term_observation.clause_index
            or item.clause_index != component_observation.clause_index
        ):
            connection.execute(
                """UPDATE morphology_formula_links_v3
                   SET state = 'blocked', updated_at = ? WHERE formula_link_id = ?""",
                (timestamp, item.observation_id),
            )
            counts["blocked_links"] += 1
            counts["non_clause_local_links_blocked"] += 1
            continue
        term_id = term_entities.get(item.term_observation_id)
        morpheme_id = component_entities.get(item.component_observation_id)
        if not term_id or not morpheme_id:
            connection.execute(
                """UPDATE morphology_formula_links_v3
                   SET state = 'blocked', updated_at = ? WHERE formula_link_id = ?""",
                (timestamp, item.observation_id),
            )
            counts["blocked_links"] += 1
            continue
        term_usable, term_promoted = _retain_or_promote_book_entity(
            connection,
            term_id,
            timestamp,
            accept=True,
            confidence=max(item.confidence, term_observation.confidence),
        )
        morpheme_usable, morpheme_promoted = _retain_or_promote_book_entity(
            connection,
            morpheme_id,
            timestamp,
            accept=True,
            confidence=max(item.confidence, component_observation.kind_confidence),
        )
        counts["promoted_entities"] += int(term_promoted) + int(morpheme_promoted)
        if not term_usable or not morpheme_usable:
            connection.execute(
                """UPDATE morphology_formula_links_v3
                   SET state = 'blocked', updated_at = ? WHERE formula_link_id = ?""",
                (timestamp, item.observation_id),
            )
            counts["blocked_links"] += 1
            continue
        surface = next(
            (
                component.raw_form
                for component in record.components
                if component.observation_id == item.component_observation_id
            ),
            "",
        )
        normalized_surface = _letters(surface)
        ordered_links = sorted(
            (
                link
                for link in record.links
                if link.state == "materializable"
                and link.term_observation_id == item.term_observation_id
                and link.component_observation_id in component_observations
            ),
            key=lambda link: (
                component_observations[link.component_observation_id].span_start,
                component_observations[link.component_observation_id].span_end,
                link.observation_id,
            ),
        )
        source_ordinal = next(
            index
            for index, link in enumerate(ordered_links)
            if link.observation_id == item.observation_id
        )
        normalized_link_changed, component_ordinal = _ensure_term_morpheme(
            connection,
            term_id,
            morpheme_id,
            normalized_surface,
            evidence_id,
            source_ordinal,
            item.observation_id,
        )
        counts["created_term_morphemes"] += int(normalized_link_changed)
        counts["canonical_changes"] += int(normalized_link_changed)
        properties = {
            "acquisition": "book-morphology-v3",
            "source_observation": True,
            "source_kinds": [source.source_kind],
            "corpus_ids": [source.corpus_id],
            "source_entry_ids": [source.record_id],
            "source_hashes": [source.source_hash],
            "parser_versions": [PARSER_VERSION],
            "span_fingerprints": [item.span_fingerprint],
            "evidence_ids": [evidence_id],
            "component_glosses": list(
                _normalized_component_glosses(component_observation.gloss)
            ),
            "surface_relation": item.surface_relation,
            "same_clause": True,
            "ordinal": component_ordinal,
            "surface": normalized_surface,
            "clauses": [
                next(
                    (
                        term.clause_text
                        for term in record.terms
                        if term.observation_id == item.term_observation_id
                    ),
                    "",
                )[:500]
            ],
            "modes": ["knowledge", "word", "root", "affix"],
        }
        assertion_id, growth = _upsert_edge_with_evidence(
            connection,
            subject_entity_id=term_id,
            source_entity_id=term_id,
            target_entity_id=morpheme_id,
            relation="has-component",
            evidence_id=evidence_id,
            confidence=item.confidence,
            properties=properties,
            timestamp=timestamp,
        )
        counts.update(growth)
        record_supports.append(
            RelationSupportSpec(
                assertion_id=assertion_id,
                basis="book",
                confidence=item.confidence,
                properties=properties,
                evidence_ids=(evidence_id,),
            )
        )
        connection.execute(
            """UPDATE morphology_formula_links_v3
               SET state = 'materialized', updated_at = ? WHERE formula_link_id = ?""",
            (timestamp, item.observation_id),
        )
        counts["materialized_links"] += 1

    # One stable logical source record owns all of its current links.  Replacing
    # this scope once, including with an empty set, retracts only this parser's
    # support and preserves independent parser/model/review owners.
    support_result = replace_relation_support_scope(
        connection,
        owner_type="morphology-parser",
        owner_id=morphology_source_owner_id(
            f"{source.corpus_id}:{source.record_id}"
        ),
        owner_version=PARSER_VERSION,
        supports=tuple(record_supports),
        timestamp=timestamp,
    )
    counts.update(support_result)
    counts["superseded_role_assertions_archived"] += int(
        support_result.get("assertions_archived", 0)
    )
    counts["superseded_role_edges_archived"] += int(
        support_result.get("edges_archived", 0)
    )
    counts["superseded_normalized_links_removed"] += int(
        support_result.get("term_morphemes_removed", 0)
    )
    counts["canonical_changes"] += sum(
        int(support_result.get(key, 0))
        for key in (
            "assertions_updated",
            "assertions_archived",
            "edges_changed",
            "edges_archived",
            "term_morphemes_removed",
        )
    )

    for item in record.declarations:
        connection.execute(
            """INSERT INTO morphology_declarations_v3(
                   declaration_id, source_observation_id, span_fingerprint,
                   declaration_type, subject_component_observation_id,
                   object_value, object_normalized_form, language, period_label,
                   confidence, state, qwen_ready, evidence_id, properties_json,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_observation_id, span_fingerprint)
               DO UPDATE SET object_value = excluded.object_value,
                             object_normalized_form = excluded.object_normalized_form,
                             language = excluded.language,
                             period_label = excluded.period_label,
                             confidence = excluded.confidence,
                             state = excluded.state,
                             qwen_ready = excluded.qwen_ready,
                             evidence_id = excluded.evidence_id,
                             properties_json = excluded.properties_json,
                             updated_at = excluded.updated_at""",
            (
                item.observation_id,
                record.source_observation_id,
                item.span_fingerprint,
                item.declaration_type,
                item.subject_component_observation_id,
                item.object_value,
                item.object_normalized_form,
                item.language,
                item.period_label,
                item.confidence,
                item.state,
                int(item.qwen_ready),
                evidence_id,
                json.dumps(item.properties, ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        counts[f"declaration_{item.declaration_type}"] += 1

    final_state = "ambiguous" if record.qwen_ready_count else (
        "materialized"
        if counts["materialized_terms"] or counts["materialized_components"]
        else "observed"
    )
    connection.execute(
        """UPDATE morphology_source_observations_v3
           SET materialization_state = ?, updated_at = ?
           WHERE source_observation_id = ?""",
        (final_state, timestamp, record.source_observation_id),
    )
    counts["records"] += 1
    counts[f"records_{final_state}"] += 1
    return term_entities, component_entities, dict(counts)


def _increment_graph_revision(connection: sqlite3.Connection) -> int:
    connection.execute(
        """INSERT INTO schema_meta(key, value)
           VALUES ('relation_graph_revision', '1')
           ON CONFLICT(key) DO UPDATE
           SET value = CAST(value AS INTEGER) + 1"""
    )
    return _current_graph_revision(connection)


def _current_graph_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """SELECT value FROM schema_meta
           WHERE key = 'relation_graph_revision'"""
    ).fetchone()
    return int(row[0]) if row else 0


def _accepted_component_roles(
    connection: sqlite3.Connection, term_entity_ids: Iterable[str]
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Return the accepted morphology topology for a bounded set of terms."""

    identifiers = tuple(
        sorted(
            {
                str(item).strip()
                for item in term_entity_ids
                if str(item).strip()
            }
        )
    )
    roles: dict[str, set[tuple[str, str]]] = {
        entity_id: set() for entity_id in identifiers
    }
    for offset in range(0, len(identifiers), 400):
        chunk = identifiers[offset : offset + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""SELECT assertion.subject_entity_id,
                       assertion.target_entity_id, morpheme.kind
                FROM relation_assertions AS assertion
                JOIN morphemes AS morpheme
                  ON morpheme.entity_id = assertion.target_entity_id
                JOIN entities AS entity
                  ON entity.entity_id = morpheme.entity_id
                 AND entity.status = 'accepted'
                WHERE assertion.status = 'accepted'
                  AND assertion.relation = 'has-component'
                  AND assertion.subject_entity_id = assertion.source_entity_id
                  AND assertion.subject_entity_id IN ({placeholders})
                  AND morpheme.kind IN ('root', 'prefix', 'suffix')
                ORDER BY assertion.subject_entity_id,
                         assertion.target_entity_id, morpheme.kind""",
            chunk,
        ).fetchall()
        for row in rows:
            roles[str(row["subject_entity_id"])].add(
                (str(row["target_entity_id"]), str(row["kind"]))
            )
    return {
        entity_id: tuple(sorted(values))
        for entity_id, values in roles.items()
    }


def _invalidate_lexical_projection_repairs(
    connection: sqlite3.Connection,
    term_entity_ids: Iterable[str],
    timestamp: str,
) -> int:
    """Atomically checkpoint terms whose accepted morphology roles changed.

    A blank desired fingerprint is a durable invalidation sentinel.  The
    knowledge scheduler replaces it with a full current projection fingerprint
    before a compose job can be claimed.  The previously applied fingerprint
    remains intact, so a crash or restart cannot mistake stale output for the
    newly desired graph.
    """

    identifiers = tuple(
        sorted(
            {
                str(item).strip()
                for item in term_entity_ids
                if str(item).strip()
            }
        )
    )
    for term_entity_id in identifiers:
        connection.execute(
            """INSERT INTO lexical_projection_repairs(
                   term_entity_id, desired_fingerprint, desired_revision,
                   desired_modes_json, status, created_at, updated_at
               ) VALUES (?, '', 0, '[]', 'pending', ?, ?)
               ON CONFLICT(term_entity_id) DO UPDATE SET
                   desired_fingerprint = '', desired_revision = 0,
                   status = 'pending', lease_token = '',
                   lease_expires_at = '', compose_job_id = NULL,
                   bound_fingerprint = '', failure_count = 0,
                   next_eligible_at = '', last_error = '',
                   updated_at = excluded.updated_at""",
            (term_entity_id, timestamp, timestamp),
        )
    return len(identifiers)


def ensure_schema(knowledge_db: Path) -> None:
    with closing(_open_knowledge(knowledge_db)) as connection:
        connection.executescript(SCHEMA)
        ensure_relation_support_schema(connection)
        connection.commit()


def _iter_batches(
    records: Sequence[ParsedRecordV3], batch_size: int
) -> Iterator[Sequence[ParsedRecordV3]]:
    for offset in range(0, len(records), batch_size):
        yield records[offset : offset + batch_size]


def run(
    knowledge_db: Path,
    sources: Sequence[tuple[str, Path]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Persist current observations and safe graph claims in bounded transactions."""

    batch_size = max(1, min(int(batch_size), MAX_BATCH_SIZE))
    specs, parsed = load_and_parse(sources)
    ensure_schema(knowledge_db)
    with closing(_open_knowledge(knowledge_db)) as connection:
        initial_graph_revision = _current_graph_revision(connection)
    final_graph_revision = initial_graph_revision
    timestamp = _now()
    run_id = str(uuid.uuid4())
    manifest = [
        {
            "source_kind": item.source_kind,
            "path": str(item.path),
            "corpus_id": item.corpus_id,
            "source_hash": item.source_hash,
        }
        for item in specs
    ]
    with closing(_open_knowledge(knowledge_db)) as connection:
        connection.execute(
            """INSERT INTO morphology_ingest_runs(
                   run_id, parser_version, source_manifest_json, state,
                   planned_records, created_at, updated_at
               ) VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (
                run_id,
                PARSER_VERSION,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                len(parsed),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()

    totals = collections.Counter()
    try:
        for batch_index, batch in enumerate(_iter_batches(parsed, batch_size)):
            batch_timestamp = _now()
            kinds = {item.source.source_kind for item in batch}
            source_kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
            with closing(_open_knowledge(knowledge_db)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                batch_term_ids = {
                    _identifier(
                        "term", f"term:en:word:{item.normalized_term}"
                    )
                    for record in batch
                    for item in record.terms
                    if item.state == "materializable" and item.normalized_term
                }
                roles_before = _accepted_component_roles(
                    connection, batch_term_ids
                )
                connection.execute(
                    """INSERT INTO morphology_ingest_batches(
                           run_id, batch_index, source_kind,
                           first_source_entry_id, last_source_entry_id,
                           record_count, state, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                    (
                        run_id,
                        batch_index,
                        source_kind,
                        batch[0].source.record_id,
                        batch[-1].source.record_id,
                        len(batch),
                        batch_timestamp,
                        batch_timestamp,
                    ),
                )
                batch_counts = collections.Counter()
                for record in batch:
                    evidence_id, evidence_changed, evidence_created = _ensure_evidence(
                        connection, record
                    )
                    batch_counts["evidence_records_created"] += int(evidence_created)
                    batch_counts["evidence_records_updated"] += int(
                        evidence_changed and not evidence_created
                    )
                    batch_counts["canonical_changes"] += int(evidence_changed)
                    _, _, counts = _insert_observation_rows(
                        connection, record, evidence_id, batch_timestamp
                    )
                    batch_counts.update(counts)
                roles_after = _accepted_component_roles(
                    connection, batch_term_ids
                )
                changed_role_terms = {
                    term_entity_id
                    for term_entity_id in batch_term_ids
                    if roles_before.get(term_entity_id, ())
                    != roles_after.get(term_entity_id, ())
                }
                batch_counts["projection_repairs_invalidated"] += (
                    _invalidate_lexical_projection_repairs(
                        connection, changed_role_terms, batch_timestamp
                    )
                )
                if batch_counts["canonical_changes"]:
                    final_graph_revision = _increment_graph_revision(connection)
                    batch_counts["revision_bumps"] = 1
                batch_report = dict(batch_counts)
                batch_report["graph_revision_after"] = final_graph_revision
                connection.execute(
                    """UPDATE morphology_ingest_batches
                       SET state = 'complete', counts_json = ?, updated_at = ?
                       WHERE run_id = ? AND batch_index = ?""",
                    (
                        json.dumps(batch_report, sort_keys=True),
                        _now(),
                        run_id,
                        batch_index,
                    ),
                )
                connection.execute(
                    """UPDATE morphology_ingest_runs
                       SET processed_records = processed_records + ?,
                           materialized_records = materialized_records + ?,
                           ambiguous_records = ambiguous_records + ?,
                           updated_at = ? WHERE run_id = ?""",
                    (
                        len(batch),
                        batch_counts["records_materialized"],
                        batch_counts["records_ambiguous"],
                        _now(),
                        run_id,
                    ),
                )
                connection.commit()
                totals.update(batch_counts)
        repair = repair_legacy_pilot(knowledge_db, parsed)
        final_graph_revision = int(
            repair.get("graph_revision_after", final_graph_revision)
        )
        report = structural_invariants(knowledge_db, specs)
        with closing(_open_knowledge(knowledge_db)) as connection:
            connection.execute(
                """UPDATE morphology_ingest_runs
                   SET state = 'complete', invariant_report_json = ?,
                       updated_at = ?, finished_at = ? WHERE run_id = ?""",
                (
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                    _now(),
                    _now(),
                    run_id,
                ),
            )
            connection.commit()
    except Exception as error:
        with closing(_open_knowledge(knowledge_db)) as connection:
            connection.execute(
                """UPDATE morphology_ingest_runs
                   SET state = 'failed', error = ?, updated_at = ?, finished_at = ?
                   WHERE run_id = ?""",
                (str(error), _now(), _now(), run_id),
            )
            connection.commit()
        raise
    return {
        "dry_run": False,
        "run_id": run_id,
        "parser_version": PARSER_VERSION,
        "planned_records": len(parsed),
        "batch_size": batch_size,
        "batch_count": (len(parsed) + batch_size - 1) // batch_size,
        "counts": dict(totals),
        "graph_revision_before": initial_graph_revision,
        "graph_revision_after": final_graph_revision,
        "pilot_repair": repair,
        "invariants": report,
    }


def structural_invariants(
    knowledge_db: Path, specs: Sequence[CorpusSpec] = ()
) -> dict[str, Any]:
    def source_record_count(path: Path) -> int:
        source_uri = f"file:{path.resolve()}?mode=ro"
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=30)
        ) as source_connection:
            row = source_connection.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    uri = f"file:{knowledge_db.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        expected = {
            item.corpus_id: source_record_count(item.path)
            for item in specs
        }
        observed = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """SELECT corpus_id, COUNT(*)
                   FROM morphology_source_observations_v3
                   WHERE parser_version = ? GROUP BY corpus_id""",
                (PARSER_VERSION,),
            )
        }
        scaffold_table = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'morphology_source_scaffolds'"""
        ).fetchone()
        pilot_records_missing = (
            connection.execute(
                """SELECT COUNT(*) FROM morphology_source_scaffolds scaffold
                   LEFT JOIN morphology_source_observations_v3 observation
                     ON observation.corpus_id = scaffold.corpus_id
                    AND observation.source_entry_id = scaffold.source_entry_id
                    AND observation.source_hash = scaffold.source_hash
                    AND observation.parser_version = ?
                   WHERE observation.source_observation_id IS NULL""",
                (PARSER_VERSION,),
            ).fetchone()[0]
            if scaffold_table is not None
            else 0
        )
        checks = {
            "source_coverage": {
                corpus_id: {
                    "expected": total,
                    "observed": observed.get(corpus_id, 0),
                    "missing": max(0, total - observed.get(corpus_id, 0)),
                }
                for corpus_id, total in expected.items()
            },
            "missing_source_evidence": connection.execute(
                """SELECT COUNT(*) FROM morphology_source_observations_v3 s
                   LEFT JOIN evidence_records e ON e.evidence_id = s.evidence_id
                   WHERE s.parser_version = ? AND e.evidence_id IS NULL""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "duplicate_source_keys": connection.execute(
                """SELECT COUNT(*) FROM (
                       SELECT corpus_id, source_entry_id, source_hash,
                              parser_version, COUNT(*) n
                       FROM morphology_source_observations_v3
                       WHERE parser_version = ?
                       GROUP BY corpus_id, source_entry_id, source_hash,
                                parser_version HAVING n > 1
                   )""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "duplicate_span_keys": connection.execute(
                """SELECT SUM(n) FROM (
                       SELECT COUNT(*) - 1 n
                       FROM morphology_component_observations_v3
                       GROUP BY source_observation_id, span_fingerprint
                       HAVING COUNT(*) > 1
                   )"""
            ).fetchone()[0] or 0,
            "materialized_ambiguous_components": connection.execute(
                """SELECT COUNT(*) FROM morphology_component_observations_v3
                   WHERE parser_version IS NULL"""
            ).fetchone()[0] if False else connection.execute(
                """SELECT COUNT(*) FROM morphology_component_observations_v3 c
                   JOIN morphology_source_observations_v3 s
                     ON s.source_observation_id = c.source_observation_id
                   WHERE s.parser_version = ? AND c.proposed_kind = 'ambiguous'
                     AND c.materialized_entity_id IS NOT NULL""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "v3_unknown_morphemes": connection.execute(
                """SELECT COUNT(*) FROM morphemes m JOIN entities e
                     ON e.entity_id = m.entity_id
                   WHERE m.kind = 'unknown'
                     AND json_extract(e.payload, '$.acquisition') = 'book-morphology-v3'"""
            ).fetchone()[0],
            "cross_clause_formula_links": connection.execute(
                """SELECT COUNT(*) FROM morphology_formula_links_v3 l
                   JOIN morphology_term_observations_v3 t
                     ON t.term_observation_id = l.term_observation_id
                   JOIN morphology_component_observations_v3 c
                     ON c.component_observation_id = l.component_observation_id
                   WHERE l.source_observation_id <> t.source_observation_id
                      OR l.source_observation_id <> c.source_observation_id
                      OR l.clause_index <> t.clause_index
                      OR l.clause_index <> c.clause_index"""
            ).fetchone()[0],
            "materialized_ambiguous_links": connection.execute(
                """SELECT COUNT(*) FROM morphology_formula_links_v3
                   WHERE state = 'materialized' AND surface_relation = 'ambiguous'"""
            ).fetchone()[0],
            "materialized_links_missing_graph_edge": connection.execute(
                """SELECT COUNT(*) FROM morphology_formula_links_v3 l
                   JOIN morphology_term_observations_v3 t
                     ON t.term_observation_id = l.term_observation_id
                   JOIN morphology_component_observations_v3 c
                     ON c.component_observation_id = l.component_observation_id
                   LEFT JOIN entity_edges e
                     ON e.source_entity_id = t.materialized_entity_id
                    AND e.target_entity_id = c.materialized_entity_id
                    AND e.relation = 'has-component'
                   WHERE l.state = 'materialized' AND e.edge_id IS NULL"""
            ).fetchone()[0],
            "v3_edges_missing_evidence_ids": connection.execute(
                """SELECT COUNT(*) FROM entity_edges
                   WHERE json_extract(properties, '$.acquisition') = 'book-morphology-v3'
                     AND COALESCE(json_array_length(
                           json_extract(properties, '$.evidence_ids')
                         ), 0) = 0"""
            ).fetchone()[0],
            "v3_assertions_missing_evidence": connection.execute(
                """SELECT COUNT(*) FROM relation_assertions a
                   WHERE json_extract(a.properties_json, '$.acquisition') =
                         'book-morphology-v3'
                     AND NOT EXISTS (
                         SELECT 1 FROM assertion_evidence x
                         WHERE x.assertion_id = a.assertion_id
                     )"""
            ).fetchone()[0],
            "accepted_v3_edges_with_nonaccepted_endpoint": connection.execute(
                """SELECT COUNT(*) FROM entity_edges edge
                   JOIN entities source ON source.entity_id = edge.source_entity_id
                   JOIN entities target ON target.entity_id = edge.target_entity_id
                   WHERE json_extract(edge.properties, '$.acquisition') =
                         'book-morphology-v3'
                     AND edge.status = 'accepted'
                     AND (source.status <> 'accepted' OR target.status <> 'accepted')"""
            ).fetchone()[0],
            "pilot_records_missing_v3_reparse": pilot_records_missing,
            "active_repaired_legacy_edges": connection.execute(
                """SELECT COUNT(*) FROM morphology_pilot_repairs_v3 repair
                   JOIN entity_edges edge ON edge.edge_id = repair.legacy_edge_id
                   WHERE repair.parser_version = ?
                     AND repair.action IN (
                         'repoint', 'archive', 'already-nonaccepted'
                     ) AND edge.status = 'accepted'""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "active_repaired_legacy_assertions": connection.execute(
                """SELECT COUNT(*) FROM morphology_pilot_repairs_v3 repair
                   JOIN relation_assertions assertion
                     ON assertion.assertion_id = repair.legacy_assertion_id
                   WHERE repair.parser_version = ?
                     AND repair.action IN (
                         'repoint', 'archive', 'already-nonaccepted'
                     ) AND assertion.status = 'accepted'""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "repaired_normalized_links_remaining": connection.execute(
                """SELECT COUNT(*) FROM morphology_pilot_repairs_v3 repair
                   JOIN term_morphemes link
                     ON link.term_id = repair.legacy_source_entity_id
                    AND link.morpheme_id = repair.legacy_target_entity_id
                   WHERE repair.parser_version = ?
                     AND repair.action IN (
                         'repoint', 'archive', 'already-nonaccepted'
                     )""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "repoint_targets_not_accepted": connection.execute(
                """SELECT COUNT(*) FROM morphology_pilot_repairs_v3 repair
                   LEFT JOIN entity_edges edge
                     ON edge.edge_id = repair.replacement_edge_id
                    AND edge.status = 'accepted'
                   WHERE repair.parser_version = ? AND repair.action = 'repoint'
                     AND edge.edge_id IS NULL""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "pilot_repairs_missing_evidence": connection.execute(
                """SELECT COUNT(*) FROM morphology_pilot_repairs_v3 repair
                   LEFT JOIN evidence_records evidence
                     ON evidence.evidence_id = repair.evidence_id
                   WHERE repair.parser_version = ? AND evidence.evidence_id IS NULL""",
                (PARSER_VERSION,),
            ).fetchone()[0],
            "accepted_legacy_unknown_morphemes": connection.execute(
                """SELECT COUNT(*) FROM morphemes morpheme
                   JOIN entities entity ON entity.entity_id = morpheme.entity_id
                   WHERE morpheme.kind = 'unknown' AND entity.status = 'accepted'
                     AND json_extract(entity.payload, '$.acquisition') =
                         'book-morphology-scaffold'"""
            ).fetchone()[0],
            "oversized_batches": connection.execute(
                """SELECT COUNT(*) FROM morphology_ingest_batches
                   WHERE record_count > 100"""
            ).fetchone()[0],
            "foreign_key_errors": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "integrity_check": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
        }
    numeric_failures = [
        key
        for key, value in checks.items()
        if key not in {"source_coverage", "integrity_check"}
        and isinstance(value, int)
        and value != 0
    ]
    coverage_failures = [
        corpus_id
        for corpus_id, value in checks["source_coverage"].items()
        if value["missing"] != 0
    ]
    checks["passed"] = (
        not numeric_failures
        and not coverage_failures
        and checks["integrity_check"] == "ok"
    )
    checks["failures"] = numeric_failures + [
        f"source_coverage:{item}" for item in coverage_failures
    ]
    return checks


def _pilot_repair_projection(
    parsed: Sequence[ParsedRecordV3],
) -> tuple[
    dict[tuple[str, str], set[tuple[str, str]]],
    dict[tuple[str, str, str], set[str]],
    set[tuple[str, str]],
]:
    desired_by_entry: dict[
        tuple[str, str], set[tuple[str, str]]
    ] = collections.defaultdict(set)
    typed_by_entry_form: dict[
        tuple[str, str, str], set[str]
    ] = collections.defaultdict(set)
    covered_entries: set[tuple[str, str]] = set()
    for record in parsed:
        entry_key = (record.source.source_kind, record.source.record_id)
        covered_entries.add(entry_key)
        terms = {
            item.observation_id: _identifier(
                "term", f"term:en:word:{item.normalized_term}"
            )
            for item in record.terms
        }
        components: dict[str, tuple[str, str]] = {}
        for item in record.components:
            if item.state != "materializable":
                continue
            try:
                base, _, canonical_key = canonical_morpheme_identity(
                    "en", item.normalized_form, item.proposed_kind
                )
            except ValueError:
                continue
            entity_id = _identifier(
                "morpheme",
                canonical_key,
            )
            components[item.observation_id] = (entity_id, _letters(base))
            typed_by_entry_form[(*entry_key, _letters(base))].add(entity_id)
        for link in record.links:
            if link.state != "materializable":
                continue
            term_id = terms.get(link.term_observation_id)
            component = components.get(link.component_observation_id)
            if term_id and component:
                desired_by_entry[entry_key].add((term_id, component[0]))
    return desired_by_entry, typed_by_entry_form, covered_entries


def _pilot_repair_rows(
    knowledge_db: Path, covered_entries: set[tuple[str, str]]
) -> list[sqlite3.Row]:
    uri = f"file:{knowledge_db.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        scaffold_table = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'morphology_source_scaffolds'"""
        ).fetchone()
        if scaffold_table is None:
            return []
        repair_table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'morphology_pilot_repairs_v3'"""
        ).fetchone()
        observation_table = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'morphology_source_observations_v3'"""
        ).fetchone()
        observation_column = (
            "observation.source_observation_id"
            if observation_table
            else "NULL AS source_observation_id"
        )
        observation_join = (
            "LEFT JOIN morphology_source_observations_v3 observation "
            "ON observation.corpus_id = scaffold.corpus_id "
            "AND observation.source_entry_id = scaffold.source_entry_id "
            "AND observation.source_hash = scaffold.source_hash "
            "AND observation.parser_version = ?"
            if observation_table
            else ""
        )
        repair_columns = (
            "repair.action AS recorded_action, "
            "repair.before_status AS recorded_before_status"
            if repair_table
            else "NULL AS recorded_action, NULL AS recorded_before_status"
        )
        repair_join = (
            "LEFT JOIN morphology_pilot_repairs_v3 repair "
            "ON repair.parser_version = ? "
            "AND repair.legacy_edge_id = edge.edge_id"
            if repair_table
            else ""
        )
        parameters = tuple(
            [PARSER_VERSION] * (int(bool(observation_table)) + int(bool(repair_table)))
        )
        rows = connection.execute(
            f"""SELECT edge.edge_id, edge.source_entity_id,
                      edge.target_entity_id, edge.status,
                      morpheme.form, morpheme.kind, term.text AS term_text,
                      COALESCE(json_extract(edge.properties, '$.raw_form'),
                               morpheme.form) AS raw_form,
                      json_extract(edge.properties, '$.source_kind') source_kind,
                      json_extract(edge.properties, '$.source_entry_id') source_entry_id,
                      scaffold.evidence_id,
                      {observation_column},
                      assertion.assertion_id AS legacy_assertion_id,
                      {repair_columns}
               FROM entity_edges edge
               JOIN morphemes morpheme
                 ON morpheme.entity_id = edge.target_entity_id
               JOIN terms term ON term.entity_id = edge.source_entity_id
               JOIN morphology_source_scaffolds scaffold
                 ON scaffold.source_kind =
                    json_extract(edge.properties, '$.source_kind')
                AND scaffold.source_entry_id =
                    json_extract(edge.properties, '$.source_entry_id')
               {observation_join}
               LEFT JOIN relation_assertions assertion
                 ON assertion.subject_entity_id = edge.source_entity_id
                AND assertion.source_entity_id = edge.source_entity_id
                AND assertion.target_entity_id = edge.target_entity_id
                AND assertion.relation = edge.relation
               {repair_join}
               WHERE edge.relation = 'has-component'
                 AND json_extract(edge.properties, '$.source_scaffold') = 1
               ORDER BY edge.edge_id""",
            parameters,
        ).fetchall()
    return [
        row
        for row in rows
        if (str(row["source_kind"] or ""), str(row["source_entry_id"] or ""))
        in covered_entries
    ]


def _pilot_repair_action(
    row: sqlite3.Row,
    desired_by_entry: dict[tuple[str, str], set[tuple[str, str]]],
    typed_by_entry_form: dict[tuple[str, str, str], set[str]],
) -> tuple[str, set[tuple[str, str]], str]:
    key = (str(row["source_kind"] or ""), str(row["source_entry_id"] or ""))
    pair = (str(row["source_entity_id"]), str(row["target_entity_id"]))
    desired = desired_by_entry.get(key, set())
    if pair in desired:
        return "retain", set(), "same typed clause-local claim"
    replacements = {
        (pair[0], candidate)
        for candidate in typed_by_entry_form.get(
            (*key, _letters(str(row["form"]))), set()
        )
        if (pair[0], candidate) in desired
    }
    if len(replacements) == 1:
        return "repoint", replacements, "unique typed clause-local replacement"
    recorded = str(row["recorded_action"] or "")
    if recorded == "archive":
        return recorded, set(), "no authoritative typed clause-local replacement"
    if recorded == "already-nonaccepted":
        return recorded, set(), "legacy edge was already nonaccepted"
    if str(row["status"]) == "accepted":
        return "archive", set(), "no authoritative typed clause-local replacement"
    return "already-nonaccepted", set(), "legacy edge was already nonaccepted"


def _legacy_forced_nonliteral(row: sqlite3.Row) -> bool:
    word = _letters(str(row["term_text"]))
    parts = _form_parts(str(row["raw_form"])) or (_letters(str(row["form"])),)
    return bool(word and parts and not any(part and part in word for part in parts))


def _repair_properties(
    raw: str, action: str, reason: str, replacement_edge_id: str
) -> str:
    properties = _json_object(raw)
    properties["morphology_v3_repair"] = {
        "action": action,
        "parser_version": PARSER_VERSION,
        "reason": reason,
        "replacement_edge_id": replacement_edge_id,
    }
    return json.dumps(properties, ensure_ascii=False, sort_keys=True)


def pilot_repair_plan(
    knowledge_db: Path, parsed: Sequence[ParsedRecordV3]
) -> dict[str, int]:
    desired, typed, covered = _pilot_repair_projection(parsed)
    globally_desired = set().union(*desired.values()) if desired else set()
    rows = _pilot_repair_rows(knowledge_db, covered)
    counts = collections.Counter()
    for row in rows:
        action, replacements, _ = _pilot_repair_action(row, desired, typed)
        pair = (str(row["source_entity_id"]), str(row["target_entity_id"]))
        if action in {"archive", "already-nonaccepted"} and pair in globally_desired:
            action = "preserve-v3"
        before_status = str(row["recorded_before_status"] or row["status"])
        forced_nonliteral = _legacy_forced_nonliteral(row)
        counts[action] += 1
        counts["accepted_before"] += int(before_status == "accepted")
        counts["legacy_unknown"] += int(str(row["kind"]) == "unknown")
        counts["accepted_legacy_unknown"] += int(
            str(row["kind"]) == "unknown" and before_status == "accepted"
        )
        counts["forced_nonliteral"] += int(forced_nonliteral)
        counts["accepted_forced_nonliteral"] += int(
            forced_nonliteral and before_status == "accepted"
        )
        counts["unique_repoint_targets"] += len(replacements)
    counts["examined"] = len(rows)
    return dict(counts)


def repair_legacy_pilot(
    knowledge_db: Path, parsed: Sequence[ParsedRecordV3]
) -> dict[str, int]:
    """Report legacy pilot candidates; v7 cleanup is owner-led elsewhere.

    Older implementations mutated aggregate assertions and normalized links
    directly.  That can erase an independent atomic or review owner sharing the
    same endpoints.  The production v7 finalizer performs the destructive work
    only after exact corpus coverage and per-assertion ownership are proved.
    """

    plan = pilot_repair_plan(knowledge_db, parsed)
    with closing(_open_knowledge(knowledge_db)) as connection:
        graph_revision = _current_graph_revision(connection)
    return {
        **plan,
        "dry_run": True,
        "owner_ledger_required": True,
        "graph_revision_before": graph_revision,
        "graph_revision_after": graph_revision,
    }

    desired, typed, covered = _pilot_repair_projection(parsed)
    globally_desired = set().union(*desired.values()) if desired else set()
    rows = _pilot_repair_rows(knowledge_db, covered)
    timestamp = _now()
    counts = collections.Counter()
    counts["unavailable_repoint_targets"] = 0
    with closing(_open_knowledge(knowledge_db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        graph_revision_before = _current_graph_revision(connection)
        repair_term_ids = {
            str(row["source_entity_id"]).strip()
            for row in rows
            if str(row["source_entity_id"]).strip()
        }
        roles_before = _accepted_component_roles(connection, repair_term_ids)
        candidate_unknowns: set[str] = set()
        for row in rows:
            action, replacements, reason = _pilot_repair_action(row, desired, typed)
            legacy_pair = (
                str(row["source_entity_id"]), str(row["target_entity_id"])
            )
            if (
                action in {"archive", "already-nonaccepted"}
                and legacy_pair in globally_desired
            ):
                action = "preserve-v3"
                reason = (
                    "legacy evidence superseded; canonical pair independently "
                    "proved by v3 clause-local evidence"
                )
            replacement_pair = next(iter(replacements)) if replacements else None
            replacement_morpheme_id = replacement_pair[1] if replacement_pair else ""
            replacement_edge_id = ""
            if action == "repoint":
                replacement = connection.execute(
                    """SELECT edge_id FROM entity_edges
                       WHERE source_entity_id = ? AND target_entity_id = ?
                         AND relation = 'has-component' AND status = 'accepted'""",
                    replacement_pair,
                ).fetchone()
                if replacement is None:
                    counts["unavailable_repoint_targets"] += 1
                    action = "archive"
                    reason = (
                        "projected typed replacement was unavailable or "
                        "nonaccepted at repair time"
                    )
                    replacement_pair = None
                    replacement_morpheme_id = ""
                else:
                    replacement_edge_id = str(replacement[0])
            counts[action] += 1
            before_status = str(row["recorded_before_status"] or row["status"])
            forced_nonliteral = _legacy_forced_nonliteral(row)
            counts["legacy_unknown"] += int(str(row["kind"]) == "unknown")
            counts["accepted_legacy_unknown"] += int(
                str(row["kind"]) == "unknown" and before_status == "accepted"
            )
            counts["forced_nonliteral"] += int(forced_nonliteral)
            counts["accepted_forced_nonliteral"] += int(
                forced_nonliteral and before_status == "accepted"
            )
            if str(row["kind"]) == "unknown":
                candidate_unknowns.add(str(row["target_entity_id"]))

            if action not in {"retain", "preserve-v3"}:
                edge = connection.execute(
                    """SELECT status, properties FROM entity_edges
                       WHERE edge_id = ?""",
                    (str(row["edge_id"]),),
                ).fetchone()
                repaired_properties = _repair_properties(
                    str(edge["properties"]), action, reason, replacement_edge_id
                )
                if (
                    str(edge["status"]) != "archived"
                    or str(edge["properties"]) != repaired_properties
                ):
                    connection.execute(
                        """UPDATE entity_edges SET status = 'archived',
                                  properties = ?, updated_at = ?
                           WHERE edge_id = ?""",
                        (repaired_properties, timestamp, str(row["edge_id"])),
                    )
                    counts["archived_edges"] += int(
                        str(edge["status"]) != "archived"
                    )
                    counts["canonical_changes"] += 1
                assertion_id = str(row["legacy_assertion_id"] or "")
                if assertion_id:
                    assertion = connection.execute(
                        """SELECT status, properties_json
                           FROM relation_assertions WHERE assertion_id = ?""",
                        (assertion_id,),
                    ).fetchone()
                    repaired_assertion = _repair_properties(
                        str(assertion["properties_json"]), action, reason,
                        replacement_edge_id,
                    )
                    if (
                        str(assertion["status"]) != "archived"
                        or str(assertion["properties_json"]) != repaired_assertion
                    ):
                        connection.execute(
                            """UPDATE relation_assertions
                               SET status = 'archived', properties_json = ?,
                                   revision = revision + 1, updated_at = ?
                               WHERE assertion_id = ?""",
                            (repaired_assertion, timestamp, assertion_id),
                        )
                        counts["archived_assertions"] += int(
                            str(assertion["status"]) != "archived"
                        )
                        counts["canonical_changes"] += 1
                removed = connection.execute(
                    """DELETE FROM term_morphemes
                       WHERE term_id = ? AND morpheme_id = ?""",
                    (
                        str(row["source_entity_id"]),
                        str(row["target_entity_id"]),
                    ),
                )
                counts["normalized_links_removed"] += int(removed.rowcount)
                counts["canonical_changes"] += int(removed.rowcount)

            audit_properties = json.dumps(
                {
                    "source_kind": str(row["source_kind"]),
                    "source_entry_id": str(row["source_entry_id"]),
                    "term_text": str(row["term_text"]),
                    "legacy_form": str(row["form"]),
                    "legacy_kind": str(row["kind"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            repair_id = _identifier(
                "morph-pilot-repair", f"{PARSER_VERSION}\0{row['edge_id']}"
            )
            existing_audit = connection.execute(
                """SELECT action, reason, replacement_edge_id,
                          replacement_morpheme_id, legacy_unknown,
                          forced_nonliteral, before_status, properties_json
                   FROM morphology_pilot_repairs_v3 WHERE repair_id = ?""",
                (repair_id,),
            ).fetchone()
            audit_values = (
                action, reason, replacement_edge_id or None,
                replacement_morpheme_id or None,
                int(str(row["kind"]) == "unknown"), int(forced_nonliteral),
                before_status, audit_properties,
            )
            if existing_audit is None:
                connection.execute(
                    """INSERT INTO morphology_pilot_repairs_v3(
                           repair_id, parser_version, legacy_edge_id,
                           legacy_assertion_id, legacy_source_entity_id,
                           legacy_target_entity_id, source_observation_id,
                           evidence_id, action, reason, replacement_edge_id,
                           replacement_morpheme_id, legacy_unknown,
                           forced_nonliteral, before_status, properties_json,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, NULLIF(?, ''), ?, ?, NULLIF(?, ''),
                                 ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        repair_id, PARSER_VERSION, str(row["edge_id"]),
                        str(row["legacy_assertion_id"] or ""),
                        str(row["source_entity_id"]),
                        str(row["target_entity_id"]),
                        str(row["source_observation_id"] or ""),
                        str(row["evidence_id"]), *audit_values,
                        timestamp, timestamp,
                    ),
                )
                counts["repair_audits_created"] += 1
            elif tuple(existing_audit) != audit_values:
                connection.execute(
                    """UPDATE morphology_pilot_repairs_v3
                       SET action = ?, reason = ?, replacement_edge_id = ?,
                           replacement_morpheme_id = ?, legacy_unknown = ?,
                           forced_nonliteral = ?, before_status = ?,
                           properties_json = ?, updated_at = ?
                       WHERE repair_id = ?""",
                    (*audit_values, timestamp, repair_id),
                )
                counts["repair_audits_updated"] += 1

        scaffold_table = connection.execute(
            """SELECT 1 FROM sqlite_master WHERE type = 'table'
               AND name = 'morphology_source_scaffolds'"""
        ).fetchone()
        isolated_unknowns = (
            connection.execute(
                """SELECT DISTINCT morpheme.entity_id
                   FROM morphemes morpheme
                   JOIN entities entity ON entity.entity_id = morpheme.entity_id
                   JOIN entity_evidence link ON link.entity_id = morpheme.entity_id
                   JOIN morphology_source_scaffolds scaffold
                     ON scaffold.evidence_id = link.evidence_id
                   WHERE morpheme.kind = 'unknown'
                     AND json_extract(entity.payload, '$.acquisition') =
                         'book-morphology-scaffold'"""
            ).fetchall()
            if scaffold_table is not None
            else []
        )
        candidate_unknowns.update(str(row[0]) for row in isolated_unknowns)
        for morpheme_id in sorted(candidate_unknowns):
            unsafe = connection.execute(
                """SELECT COUNT(*) FROM entity_edges
                   WHERE status = 'accepted'
                     AND (source_entity_id = ? OR target_entity_id = ?)
                     AND relation <> 'has-meaning'""",
                (morpheme_id, morpheme_id),
            ).fetchone()[0]
            if int(unsafe):
                counts["unknown_entities_preserved_for_other_claims"] += 1
                continue
            meanings = [
                str(item[0])
                for item in connection.execute(
                    """SELECT entity_id FROM meanings
                       WHERE subject_entity_id = ?""",
                    (morpheme_id,),
                )
            ]
            for edge in connection.execute(
                """SELECT edge_id, status, properties FROM entity_edges
                   WHERE status = 'accepted' AND source_entity_id = ?
                     AND relation = 'has-meaning'""",
                (morpheme_id,),
            ).fetchall():
                properties = _repair_properties(
                    str(edge["properties"]), "archive",
                    "meaning owner was an untyped legacy scaffold morpheme", "",
                )
                connection.execute(
                    """UPDATE entity_edges SET status = 'archived',
                              properties = ?, updated_at = ? WHERE edge_id = ?""",
                    (properties, timestamp, str(edge["edge_id"])),
                )
                counts["archived_unknown_meaning_edges"] += 1
                counts["canonical_changes"] += 1
            for entity_id in (*meanings, morpheme_id):
                entity = connection.execute(
                    """SELECT status, payload FROM entities WHERE entity_id = ?""",
                    (entity_id,),
                ).fetchone()
                if entity is None:
                    continue
                payload = _json_object(str(entity["payload"]))
                payload["morphology_v3_repair"] = {
                    "action": "archive-untyped-legacy-entity",
                    "parser_version": PARSER_VERSION,
                }
                payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if (
                    str(entity["status"]) != "archived"
                    or str(entity["payload"]) != payload_json
                ):
                    connection.execute(
                        """UPDATE entities SET status = 'archived', payload = ?,
                                  updated_at = ? WHERE entity_id = ?""",
                        (payload_json, timestamp, entity_id),
                    )
                    counts["archived_unknown_entities"] += int(
                        entity_id == morpheme_id
                        and str(entity["status"]) != "archived"
                    )
                    counts["archived_unknown_meanings"] += int(
                        entity_id != morpheme_id
                        and str(entity["status"]) != "archived"
                    )
                    counts["canonical_changes"] += 1

        roles_after = _accepted_component_roles(connection, repair_term_ids)
        changed_role_terms = {
            term_entity_id
            for term_entity_id in repair_term_ids
            if roles_before.get(term_entity_id, ())
            != roles_after.get(term_entity_id, ())
        }
        counts["projection_repairs_invalidated"] += (
            _invalidate_lexical_projection_repairs(
                connection, changed_role_terms, timestamp
            )
        )

        if counts["canonical_changes"]:
            graph_revision_after = _increment_graph_revision(connection)
            counts["revision_bumps"] = 1
        else:
            graph_revision_after = graph_revision_before
        connection.commit()
    counts["examined"] = len(rows)
    counts["graph_revision_before"] = graph_revision_before
    counts["graph_revision_after"] = graph_revision_after
    return dict(counts)


def simulate(
    sources: Sequence[tuple[str, Path]], knowledge_db: Path | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    specs, parsed = load_and_parse(sources)
    result = _simulation_summary(specs, parsed)
    if knowledge_db is not None:
        result["pilot_repair_plan"] = pilot_repair_plan(knowledge_db, parsed)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["records_per_second"] = round(
        len(parsed) / max(time.monotonic() - started, 0.000001), 1
    )
    return result


def _source_arguments(values: Sequence[str]) -> list[tuple[str, Path]]:
    result = []
    for value in values:
        if "=" not in value:
            raise ValueError("sources must be passed as root=/path or affix=/path")
        source_kind, raw_path = value.split("=", 1)
        if source_kind not in {"root", "affix"}:
            raise ValueError(f"unsupported source kind: {source_kind!r}")
        result.append((source_kind, Path(raw_path)))
    if not result:
        raise ValueError("at least one morphology source is required")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Current versioned, evidence-first morphology ingestion"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    simulation = subparsers.add_parser("simulate")
    simulation.add_argument("sources", nargs="+")
    simulation.add_argument("--knowledge-db", type=Path)
    ingestion = subparsers.add_parser("run")
    ingestion.add_argument("knowledge_db", type=Path)
    ingestion.add_argument("sources", nargs="+")
    ingestion.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ingestion.add_argument(
        "--allow-write",
        action="store_true",
        help="required acknowledgement; use a database copy until reviewed",
    )
    report = subparsers.add_parser("report")
    report.add_argument("knowledge_db", type=Path)
    report.add_argument("sources", nargs="*")
    args = parser.parse_args()
    if args.command == "simulate":
        output = simulate(_source_arguments(args.sources), args.knowledge_db)
    elif args.command == "run":
        if not args.allow_write:
            parser.error("run requires --allow-write; use a database copy")
        output = run(
            args.knowledge_db,
            _source_arguments(args.sources),
            batch_size=args.batch_size,
        )
    else:
        sources = _source_arguments(args.sources) if args.sources else []
        specs = [load_and_parse([item])[0][0] for item in sources]
        output = structural_invariants(args.knowledge_db, specs)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
