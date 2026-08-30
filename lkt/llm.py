from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import Evidence
from .pronunciation import is_arabic_script_text


class ModelUnavailable(RuntimeError):
    pass


class InvalidModelOutput(ModelUnavailable):
    """A bounded record of semantic model failures after the fresh retry."""

    def __init__(
        self,
        message: str,
        *,
        model: str,
        failures: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.model = model
        self.failures = tuple(dict(item) for item in failures[:2])


class CardModel(Protocol):
    model_name: str

    def generate(self, query: str, mode: str, evidence: list[Evidence]) -> dict[str, Any]:
        ...


def _evidence_context(
    evidence: list[Evidence], *, excerpt_limit: int = 1600
) -> str:
    blocks = []
    for index, item in enumerate(evidence, 1):
        pages = ", ".join(str(page) for page in item.pages) or "not applicable"
        translations = (
            json.dumps(item.translations, ensure_ascii=False)
            if item.translations
            else "not supplied"
        )
        blocks.append(
            f"SOURCE {index}\n"
            f"Book: {item.source_title}\n"
            f"Record ID: {item.entry_id}\n"
            f"Record kind: {item.kind}\n"
            f"Headword: {item.headword}\n"
            f"Section: {item.section or 'unknown'}\n"
            f"Date label: {item.date_label or 'unknown'}\n"
            f"Book pages: {pages}\n"
            f"Locator: {item.locator or 'not applicable'}\n"
            f"Authoritative source text: {item.excerpt[:max(120, excerpt_limit)]}\n"
            f"Reviewed translations: {translations}"
        )
    return "\n\n".join(blocks)


WORD_ORIGIN_PROMPT = """You prepare the durable Word Origin graph in Local Knowledge Terminal.
Use every relevant supplied Word Origins, Root Dictionary, and Affix Dictionary record. Decompose
the center word into all useful evidenced components, then trace each component's history
recursively until another step becomes uncertain or stops aiding understanding. BOOK EVIDENCE is
authoritative. You may add established linguistic knowledge, but label it model. Never invent a
quotation, record ID, or page. A book node must list an exact supplied Record ID in evidence_ids.

Return exactly one JSON object with no markdown:
{
  "title": "center English word",
  "summary_en": "one concise modern definition",
  "english": {"term": "", "pronunciation": "IPA", "meaning": "short meaning"},
  "japanese": {"term": "established equivalent", "reading": "exact kana", "meaning": "short Japanese meaning", "ruby_tokens": [{"t": "visible segment", "r": "kana or empty"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"},
  "morphology_graph": {
    "center_id": "word",
    "nodes": [{"id": "unique-id", "type": "word|prefix|root|suffix|historical|related", "form": "visible form", "meaning": "at most 10 words", "language": "language or era", "history": "one concise factual sentence", "basis": "book|model", "evidence_ids": ["exact Record ID"], "confidence": "high|medium"}],
    "edges": [{"source": "earlier/component id", "target": "later/formed word id", "relationship": "developed-into|prefix-of|root-of|suffix-of|related-form"}],
    "focus_areas": [{"id": "overview-or-branch-id", "label": "short slide label", "kind": "overview|root|prefix|suffix|history", "node_ids": ["visible ids"], "headline": "one core idea", "explanation": "one short teaching sentence"}]
  }
}

Use 7 to 14 connected nodes. Components point into the word or compound they form; historical
forms point into descendants. Sibling components never parent one another. Begin focus_areas with
an overview containing every node, then add one area per important component/history branch. The
saved graph may be detailed; each focus explanation must stay sparse enough for one screen.
Japanese ruby token text must concatenate exactly to japanese.term. Use Unicode directly. Keep the
whole response under 700 words. /no_think"""


WORD_CARD_PROMPT = """You are the independent multilingual Word Card engine in Local Knowledge
Terminal. Use the Word Origins excerpts as retrieved reference, then make one compact memory card.
The core learning object is the established equivalent in English, Japanese, Chinese, French, and
Arabic. Never invent a quotation, source, or page. Return exactly one JSON object with no markdown.
Fill every required English, Japanese, and Chinese field with real content; never return a blank
template or placeholder.

Required JSON shape:
{
  "title": "the English word",
  "english": {"term": "", "pronunciation": "IPA", "meaning": "short modern English meaning"},
  "japanese": {"term": "established equivalent", "reading": "exact kana reading", "meaning": "short meaning written in Japanese", "ruby_tokens": [{"t": "kanji or kana segment", "r": "exact kana reading; empty for plain kana"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short meaning written in Chinese"},
  "french": {"term": "established equivalent", "pronunciation": "IPA if known", "meaning": "short meaning written in French"},
  "arabic": {"term": "established modern Arabic equivalent", "reading": "simple transliteration", "meaning": "short meaning written in Arabic"}
}
All five term fields must express the same current everyday sense, not merely the literal ancient
root. Prefer common lexical equivalents over transliterations. Japanese ruby token text must
concatenate exactly to japanese.term. If you are not confident in a French or Arabic equivalent,
return its object with empty strings instead of guessing. Before returning, verify each reading
against its term and keep every meaning in its requested language. Use Unicode directly. Keep every
meaning to one short phrase and the whole response under 300 words so it fits one screen. /no_think"""


ANSWER_PROMPT = """You are the independent Book Answer engine in Local Knowledge Terminal.
The selected answer and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. The application attaches those translations itself. Return
exactly one compact JSON object with no markdown or commentary:

{"title": "2 to 5 word title", "origin_story": "one concise reflection sentence"}
Use Unicode directly. Keep the response under 40 words. /no_think"""


QUESTION_PROMPT = """You are the independent Book Question engine in Local Knowledge Terminal.
The selected question and reviewed translations in BOOK EVIDENCE are authoritative: preserve them
exactly and never invent a citation. The application attaches those translations itself. Return
exactly one compact JSON object with no markdown or commentary:

{"title": "2 to 5 word title", "origin_story": "one concise reflection prompt"}
Use Unicode directly. Keep the response under 40 words. /no_think"""


MORPHOLOGY_PROMPT = """You prepare a durable morphology graph for Local Knowledge Terminal.
The requested mode is Root or Affix. Keep the exact primary Root/Affix book headword as the center;
do not invent a whole word merely to hold it. Build one complete graph around that form: its real
meaning/function, useful historical ancestors, variants, and a few high-value words that genuinely
contain or descend from it. Include other prefixes, roots, or suffixes only when they explain one of
those words. Use supplied Word Origins records for historical support when present. BOOK EVIDENCE
is authoritative. You may add established linguistic knowledge, but label it model. Never invent a
quotation, record ID, or page. A book node must list at least one exact supplied Record ID in
evidence_ids; otherwise label it model. Prefer a smaller accurate graph over speculative
decomposition.

Return exactly one JSON object with no markdown:
{
  "title": "center English word",
  "summary_en": "one concise modern definition",
  "morphology_graph": {
    "center_id": "word",
    "nodes": [{"id": "unique-id", "type": "word|prefix|root|suffix|historical|related", "form": "visible form", "meaning": "at most 8 words", "language": "English/Latin/Greek/etc", "history": "at most 12 words", "basis": "book|model", "evidence_ids": ["exact Record ID"], "confidence": "high|medium"}],
    "edges": [{"source": "earlier/component node id", "target": "later/formed word id", "relationship": "developed-into|prefix-of|root-of|suffix-of|related-form"}],
    "focus_areas": [{"id": "overview-or-node-id", "label": "short slide label", "kind": "overview|root|prefix|suffix|history", "node_ids": ["ids visible in this area"], "headline": "one core idea", "explanation": "one short teaching sentence"}]
  }
}

Use 7 to 14 nodes and a connected directed graph. Components point into words; historical forms
point into descendants. Never chain sibling components into each other. Start focus_areas with an
overview containing all node IDs, then add one area for each important root, prefix, and suffix.
Recursive history should stop when evidence becomes uncertain or ceases to aid understanding.
Use Unicode directly. Keep the graph response under 750 words. /no_think"""


MORPHOLOGY_LANGUAGE_PROMPT = """You prepare only the compact multilingual presentation for one
already reviewed Root or Affix graph. Do not change its structure, history, or citations. Return
exactly one JSON object with no markdown:
{
  "english": {"term": "exact center form", "pronunciation": "IPA if useful", "meaning": "short English meaning"},
  "japanese": {"term": "established equivalent", "reading": "exact kana", "meaning": "short Japanese meaning", "ruby_tokens": [{"t": "visible segment", "r": "kana or empty"}]},
  "chinese": {"simplified": "established equivalent", "traditional": "", "pinyin": "tone-marked pinyin", "meaning": "short Chinese meaning"},
  "french": {"term": "established equivalent", "pronunciation": "IPA if known", "meaning": "short French meaning"},
  "arabic": {"term": "established Arabic equivalent", "reading": "simple transliteration", "meaning": "short Arabic meaning"}
}
Japanese ruby token text must concatenate exactly to japanese.term. Use Unicode directly. Keep each
meaning to one short phrase and the entire response under 140 words. If French or Arabic has no
honest compact equivalent, choose a conservative established equivalent. Arabic term and meaning
must contain Arabic-script letters only; only arabic.reading may use Latin letters. /no_think"""


# Runtime evidence-first prompts replace the older expansive graph contract above.
WORD_ORIGIN_PROMPT = """Create one compact multilingual Word Origin card from
the supplied retrieved book evidence. Return one JSON object only. Work
evidence-first, then add only useful local-model knowledge as visibly labeled
hypotheses. A `book` node or edge must cite exact supplied `evidence_ids`; a
`model` node or edge must use empty evidence_ids and low confidence. Never attach
a citation to model knowledge. Include one to six useful nodes. The
center node form and english.term must exactly match the requested word.
Components point into words. Historical forms point toward later forms.
A model prefix-of edge must match the word start, suffix-of the word end, and
root-of must visibly occur inside the word.
Sibling components never parent one another. Include at least one focus_areas entry.
Return title, summary_en, morphology_graph, english, japanese, chinese, french,
and arabic. Japanese and Chinese ruby token text must concatenate exactly to the
displayed term. Root Dictionary, and Affix Dictionary evidence may be used only
when present in supplied records. Never copy instruction or example labels."""

MORPHOLOGY_PROMPT = """Create one compact connected evidence-first morphology
graph. Return title, summary_en, and
morphology_graph. The graph has center_id, nodes, edges, and focus_areas. Use one
to six nodes, proportional to explicit evidence; a one-node graph is valid when
only the center is useful. Every node and edge has basis, evidence_ids, and
confidence. Use basis `book` only when exact supplied evidence IDs support the
claim. Local-model knowledge is allowed with basis `model`, empty evidence_ids,
and low confidence. Never relabel a model claim as book. Historical hypotheses
may be uncited model nodes. Model prefix-of, suffix-of, and root-of edges must
also match the visible surface shape. Do not add decorative filler. The center
form must exactly equal the
requested primary component. Components point into words. Historical forms
point toward later forms. Include one or more focus_areas."""

MORPHOLOGY_LANGUAGE_PROMPT = """Return one JSON object containing english,
japanese, chinese, french, and arabic objects for the accepted graph. Copy the
user's EXACT CENTER FORM exactly into english.term. Supply concise natural
meanings and established translations, but never copy an instruction label or
placeholder into a value. Japanese needs term, reading, meaning; Chinese needs
simplified, pinyin, meaning; French needs term, meaning; Arabic needs term and
meaning. Use Arabic-script letters only in both Arabic fields. Return JSON only."""

_PROMPT_PLACEHOLDER_KEYS = frozenset(
    {"exactcenterform", "establishedequivalent", "conciseenglishgloss", "rootmeaning"}
)


def _normalized_english_term(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _contains_prompt_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_prompt_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_prompt_placeholder(item) for item in value)
    return (
        isinstance(value, str)
        and _normalized_english_term(value) in _PROMPT_PLACEHOLDER_KEYS
    )


def _evidence_form_visible(item: Evidence, form: str) -> bool:
    def tokens(value: Any) -> tuple[str, ...]:
        decomposed = unicodedata.normalize("NFKD", str(value)).casefold()
        normalized = "".join(
            character
            for character in decomposed
            if not unicodedata.combining(character)
        )
        return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))

    needle = tokens(form)
    if not needle:
        return False
    width = len(needle)
    return any(
        any(
            haystack[index : index + width] == needle
            for index in range(len(haystack) - width + 1)
        )
        for haystack in (tokens(item.headword), tokens(item.excerpt))
    )


def _evidence_supports_node(item: Evidence, node: dict[str, Any]) -> bool:
    if not _evidence_form_visible(item, str(node.get("form", ""))):
        return False
    meaning_tokens = {
        token
        for token in re.findall(r"[a-z]{3,}", str(node.get("meaning", "")).casefold())
        if token not in {"the", "and", "from", "form", "word", "related"}
    }
    evidence_tokens = set(
        re.findall(r"[a-z]{3,}", f"{item.headword} {item.excerpt}".casefold())
    )
    return not meaning_tokens or bool(meaning_tokens & evidence_tokens)


def _evidence_supports_edge(
    item: Evidence,
    edge: dict[str, Any],
    source: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    if not (
        _evidence_form_visible(item, str(source.get("form", "")))
        and _evidence_form_visible(item, str(target.get("form", "")))
    ):
        return False
    relation = str(edge.get("relationship", "")).strip().casefold()
    text = f"{item.headword} {item.excerpt}".casefold()
    owner = _normalized_english_term(item.headword)
    endpoints = {
        _normalized_english_term(source.get("form", "")),
        _normalized_english_term(target.get("form", "")),
    }
    if relation.endswith("-of"):
        return owner in endpoints
    if relation in {"developed-into", "derived-from", "descends-from"}:
        return any(
            cue in text
            for cue in (" from ", "derived", "descend", "develop", "became", "<", "→")
        )
    return True


def _model_confidence_is_conservative(value: dict[str, Any]) -> bool:
    confidence = value.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        return 0.0 <= float(confidence) <= 0.7
    return str(confidence).strip().casefold() in {"low", "conservative"}


def _model_relationship_matches_surface(
    edge: dict[str, Any], source: dict[str, Any], target: dict[str, Any]
) -> bool:
    relation = str(edge.get("relationship", "")).strip().casefold()
    source_form = _normalized_english_term(source.get("form", ""))
    target_form = _normalized_english_term(target.get("form", ""))
    if not source_form or not target_form:
        return False
    if relation == "prefix-of":
        return target_form.startswith(source_form)
    if relation == "suffix-of":
        return target_form.endswith(source_form)
    if relation == "root-of":
        return source_form in target_form
    return True


def _validate_grounded_morphology_graph(
    value: dict[str, Any], requested_form: str, evidence: list[Evidence]
) -> None:
    _validate_morphology_graph_draft(value)
    graph = value["morphology_graph"]
    nodes = graph["nodes"]
    node_by_id = {str(node["id"]).strip(): node for node in nodes}
    center = node_by_id[str(graph["center_id"]).strip()]
    warnings: list[dict[str, str]] = []

    def warn(scope: str, claim_id: str, code: str, message: str) -> None:
        warnings.append(
            {
                "scope": scope,
                "claim_id": claim_id,
                "code": code,
                "message": message,
            }
        )

    expected = str(requested_form).strip()
    if expected and str(center.get("form", "")).strip() != expected:
        previous = str(center.get("form", "")).strip()
        center["form"] = expected
        warn(
            "graph",
            str(graph["center_id"]).strip(),
            "center-form-normalized",
            f"center form {previous!r} was normalized to the requested form",
        )

    evidence_by_id = {str(item.entry_id): item for item in evidence}

    def sanitize_claim(
        claim: dict[str, Any],
        *,
        scope: str,
        claim_id: str,
        support_check: Any,
    ) -> None:
        cited = claim.get("evidence_ids")
        supplied_ids = (
            list(dict.fromkeys(str(item) for item in cited))
            if isinstance(cited, list)
            else []
        )
        valid_ids = [item for item in supplied_ids if item in evidence_by_id]
        if valid_ids != supplied_ids:
            warn(
                scope,
                claim_id,
                "evidence-ids-filtered",
                "non-retrieved evidence IDs were removed",
            )

        basis = str(claim.get("basis", "")).strip().casefold()
        if basis == "book" and valid_ids:
            claim["basis"] = "book"
            claim["evidence_ids"] = valid_ids
            if not any(support_check(evidence_by_id[item]) for item in valid_ids):
                warn(
                    scope,
                    claim_id,
                    "evidence-support-warning",
                    "retrieved provenance exists but semantic support is imperfect",
                )
            return

        if basis == "book":
            warn(
                scope,
                claim_id,
                "book-claim-downgraded",
                "book claim had no retrieved evidence ID and became model-owned",
            )
        elif basis != "model":
            warn(
                scope,
                claim_id,
                "basis-normalized",
                "unknown basis became model-owned",
            )
        if supplied_ids:
            warn(
                scope,
                claim_id,
                "model-citations-stripped",
                "model-owned claims cannot retain citations",
            )
        claim["basis"] = "model"
        claim["evidence_ids"] = []
        if not _model_confidence_is_conservative(claim):
            claim["confidence"] = "low"
            warn(
                scope,
                claim_id,
                "model-confidence-normalized",
                "model-owned confidence was normalized to low",
            )

    for node in nodes:
        node_id = str(node.get("id", "")).strip()
        sanitize_claim(
            node,
            scope="node",
            claim_id=node_id,
            support_check=lambda item, node=node: _evidence_supports_node(item, node),
        )
    for index, edge in enumerate(graph.get("edges", [])):
        source = node_by_id.get(str(edge.get("source", "")).strip(), {})
        target = node_by_id.get(str(edge.get("target", "")).strip(), {})
        edge_id = f"edge-{index}"
        sanitize_claim(
            edge,
            scope="edge",
            claim_id=edge_id,
            support_check=lambda item, edge=edge, source=source, target=target: (
                _evidence_supports_edge(item, edge, source, target)
            ),
        )
        if (
            str(edge.get("basis", "")).strip().casefold() == "model"
            and not _model_relationship_matches_surface(edge, source, target)
        ):
            warn(
                "edge",
                edge_id,
                "model-surface-shape-warning",
                "model relationship does not match the visible surface shape",
            )
    graph["provenance_warnings"] = warnings


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model response did not contain a JSON object")


def _nonempty_path(value: dict[str, Any], *path: str) -> bool:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return isinstance(current, str) and bool(current.strip())


def _morphology_graph_errors(value: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    graph = value.get("morphology_graph")
    if not isinstance(graph, dict):
        return ["morphology_graph"]
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    focuses = graph.get("focus_areas")
    center_id = str(graph.get("center_id", "")).strip()
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 6:
        missing.append("morphology_graph.nodes[1-6]")
        nodes = []
    node_ids = {
        str(node.get("id", "")).strip()
        for node in nodes
        if isinstance(node, dict)
    }
    if not center_id or center_id not in node_ids:
        missing.append("morphology_graph.center_id")
    if any(
        not isinstance(node, dict)
        or any(
            not str(node.get(key, "")).strip()
            for key in ("id", "type", "form", "meaning", "basis")
        )
        for node in nodes
    ):
        missing.append("morphology_graph.complete_nodes")
    valid_edges = (
        [
            edge
            for edge in edges
            if isinstance(edge, dict)
            and str(edge.get("source", "")).strip() in node_ids
            and str(edge.get("target", "")).strip() in node_ids
        ]
        if isinstance(edges, list)
        else []
    )
    if len(valid_edges) < max(0, len(nodes) - 1):
        missing.append("morphology_graph.connected_edges")
    else:
        adjacency = {node_id: set() for node_id in node_ids}
        for edge in valid_edges:
            source = str(edge["source"]).strip()
            target = str(edge["target"]).strip()
            adjacency[source].add(target)
            adjacency[target].add(source)
        reached = {center_id} if center_id in adjacency else set()
        frontier = list(reached)
        while frontier:
            current = frontier.pop()
            for neighbor in adjacency[current] - reached:
                reached.add(neighbor)
                frontier.append(neighbor)
        if reached != node_ids:
            missing.append("morphology_graph.connected")
    if not isinstance(focuses, list) or not focuses:
        missing.append("morphology_graph.focus_areas[1+]")
    return missing


def _validate_morphology_graph_draft(value: dict[str, Any]) -> None:
    missing = [
        path
        for path in ("title", "summary_en")
        if not _nonempty_path(value, path)
    ]
    missing.extend(_morphology_graph_errors(value))
    if missing:
        raise ValueError(
            f"model returned incomplete morphology graph: {', '.join(missing)}"
        )


def _validate_morphology_language_draft(
    value: dict[str, Any], expected_term: str = ""
) -> None:
    required = (
        ("english", "term"),
        ("english", "meaning"),
        ("japanese", "term"),
        ("japanese", "reading"),
        ("japanese", "meaning"),
        ("chinese", "simplified"),
        ("chinese", "pinyin"),
        ("chinese", "meaning"),
        ("french", "term"),
        ("french", "meaning"),
        ("arabic", "term"),
        ("arabic", "meaning"),
    )
    missing = [".".join(path) for path in required if not _nonempty_path(value, *path)]
    if _contains_prompt_placeholder(value):
        missing.append("prompt_placeholder")
    if expected_term and (
        _normalized_english_term(value.get("english", {}).get("term", ""))
        != _normalized_english_term(expected_term)
    ):
        missing.append("english.term.exact_center")
    arabic = value.get("arabic") if isinstance(value.get("arabic"), dict) else {}
    if arabic and (
        not is_arabic_script_text(str(arabic.get("term", "")))
        or not is_arabic_script_text(str(arabic.get("meaning", "")))
    ):
        missing.append("arabic.valid_script")
    if missing:
        raise ValueError(
            f"model returned incomplete morphology languages: {', '.join(missing)}"
        )


def _validate_card_draft(
    value: dict[str, Any],
    mode: str,
    *,
    expected_term: str = "",
    evidence: list[Evidence] | None = None,
) -> None:
    required_paths = {
        "word": (
            ("title",),
            ("summary_en",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("japanese", "meaning"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
            ("chinese", "meaning"),
        ),
        "knowledge": (
            ("title",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("japanese", "meaning"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
            ("chinese", "meaning"),
        ),
        "answer": (("title",), ("origin_story",)),
        "question": (("title",), ("origin_story",)),
        "root": (
            ("title",),
            ("summary_en",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
        ),
        "affix": (
            ("title",),
            ("summary_en",),
            ("english", "term"),
            ("english", "meaning"),
            ("japanese", "term"),
            ("japanese", "reading"),
            ("chinese", "simplified"),
            ("chinese", "pinyin"),
        ),
    }
    missing = [
        ".".join(path)
        for path in required_paths[mode]
        if not _nonempty_path(value, *path)
    ]
    if mode in {"word", "root", "affix"}:
        graph_errors = _morphology_graph_errors(value)
        missing.extend(graph_errors)
        if not graph_errors and expected_term and evidence is not None:
            _validate_grounded_morphology_graph(value, expected_term, evidence)
        graph = value.get("morphology_graph", {})
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        center_id = str(graph.get("center_id", "")) if isinstance(graph, dict) else ""
        center = next(
            (
                node
                for node in nodes
                if isinstance(node, dict) and str(node.get("id", "")) == center_id
            ),
            {},
        )
        english_term = value.get("english", {}).get("term", "")
        if center and (
            _normalized_english_term(english_term)
            != _normalized_english_term(center.get("form", ""))
        ):
            missing.append("english.term.graph_center")
        if expected_term and (
            _normalized_english_term(english_term)
            != _normalized_english_term(expected_term)
        ):
            missing.append("english.term.requested")
        if _contains_prompt_placeholder(value):
            missing.append("prompt_placeholder")
    if missing:
        raise ValueError(f"model returned incomplete card fields: {', '.join(missing)}")


class LlamaCppClient:
    def __init__(self, url: str, model_name: str, timeout: int = 240):
        self.url = url
        self.model_name = model_name
        self.timeout = timeout

    def health(self, timeout: float = 2.0) -> bool:
        health_url = self.url.split("/v1/", 1)[0].rstrip("/") + "/health"
        try:
            with urllib.request.urlopen(health_url, timeout=timeout) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False

    def _request(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise ModelUnavailable(f"model HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelUnavailable(f"local model unavailable: {exc}") from exc
        if not isinstance(body, dict):
            raise ModelUnavailable("unexpected response from local model")
        return body, time.monotonic() - started

    @staticmethod
    def _content(body: dict[str, Any]) -> str:
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable("unexpected response from local model") from exc
        return str(content)

    def chat(
        self, messages: list[dict[str, str]], context: str = ""
    ) -> dict[str, Any]:
        system_content = (
            "You are the local Qwen model inside Local Knowledge Terminal. "
            "Answer clearly and directly in the language used by the user. "
            "This is raw chat, so never imply that an answer is book-cited."
        )
        if context:
            system_content += (
                " The user is discussing the saved card below. Use its retrieved "
                "source excerpts as the only authority for historical or book claims.\n\n"
                f"CURRENT CARD\n{context}"
            )
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": system_content,
                },
                *messages[:-1],
                {
                    **messages[-1],
                    "content": f"{messages[-1]['content']}\n\n/no_think",
                },
            ],
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 1.2,
            "max_tokens": 640,
            "stream": False,
        }
        body, elapsed = self._request(payload)
        content = re.sub(
            r"<think>.*?</think>", "", self._content(body), flags=re.DOTALL
        ).strip()
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
        completion_tokens = int(usage.get("completion_tokens") or timings.get("predicted_n") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0)
        measured_rate = completion_tokens / elapsed if elapsed > 0 else 0.0
        generation_rate = float(timings.get("predicted_per_second") or measured_rate)
        return {
            "message": content,
            "model": self.model_name,
            "grounded": False,
            "contextual": bool(context),
            "metrics": {
                "elapsed_seconds": round(elapsed, 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tokens_per_second": round(generation_rate, 2),
            },
        }

    def complete_json(
        self,
        system: str,
        prompt: str,
        *,
        max_tokens: int = 256,
    ) -> dict[str, Any]:
        """Run one bounded, low-temperature atomic preparation task."""

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system.strip()},
                {"role": "user", "content": f"{prompt.strip()}\n\n/no_think"},
            ],
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 20,
            "max_tokens": max(64, min(int(max_tokens), 512)),
            "stream": False,
        }
        body, elapsed = self._request(payload)
        content = re.sub(
            r"<think>.*?</think>", "", self._content(body), flags=re.DOTALL
        ).strip()
        value = _extract_json(content)
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        timings = body.get("timings") if isinstance(body.get("timings"), dict) else {}
        completion_tokens = int(
            usage.get("completion_tokens") or timings.get("predicted_n") or 0
        )
        return {
            "value": value,
            "raw": content,
            "model": self.model_name,
            "metrics": {
                "elapsed_seconds": round(elapsed, 2),
                "completion_tokens": completion_tokens,
                "tokens_per_second": round(
                    float(timings.get("predicted_per_second") or 0), 2
                ),
            },
        }

    def _complete_card_stage(
        self,
        payload: dict[str, Any],
        validator: Any,
        *,
        repair_tokens: int,
        repair_instruction: str,
    ) -> dict[str, Any]:
        """Run one bounded JSON stage with one fresh, non-recursive retry."""

        failures: list[dict[str, Any]] = []
        for attempt in range(2):
            body, elapsed = self._request(payload)
            content = self._content(body)
            try:
                value = _extract_json(str(content))
                validator(value)
                return {
                    "value": value,
                    "raw": str(content),
                    "model": self.model_name,
                    "attempts": attempt + 1,
                    "elapsed_seconds": round(elapsed, 2),
                    "failed_attempts": failures,
                }
            except ValueError as exc:
                usage = (
                    body.get("usage")
                    if isinstance(body.get("usage"), dict)
                    else {}
                )
                timings = (
                    body.get("timings")
                    if isinstance(body.get("timings"), dict)
                    else {}
                )
                failures.append(
                    {
                        "attempt": attempt + 1,
                        "error": str(exc)[:500],
                        "raw": str(content)[:4_000],
                        "metrics": {
                            "elapsed_seconds": round(elapsed, 2),
                            "prompt_tokens": int(
                                usage.get("prompt_tokens")
                                or timings.get("prompt_n")
                                or 0
                            ),
                            "completion_tokens": int(
                                usage.get("completion_tokens")
                                or timings.get("predicted_n")
                                or 0
                            ),
                            "tokens_per_second": round(
                                float(timings.get("predicted_per_second") or 0),
                                2,
                            ),
                        },
                    }
                )
                if attempt:
                    raise InvalidModelOutput(
                        "model stage was invalid after one fresh repair attempt",
                        model=self.model_name,
                        failures=failures,
                    ) from exc
                # Never feed a ceiling-truncated JSON document back into Qwen.
                # A fresh deterministic attempt uses the original evidence and
                # a larger ceiling, avoiding recursive context inflation.
                payload = {
                    **payload,
                    "temperature": 0.0,
                    "presence_penalty": 0.0,
                    "max_tokens": repair_tokens,
                    "messages": [
                        *payload["messages"],
                        {
                            "role": "user",
                            "content": f"{repair_instruction} /no_think",
                        },
                    ],
                }
        raise AssertionError("unreachable")

    def generate_morphology_graph(
        self, query: str, mode: str, evidence: list[Evidence]
    ) -> dict[str, Any]:
        instruction = (
            f"Prepare a complete {mode.upper()}-FOCUSED graph for the exact "
            f"primary component {query!r}."
        )
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": MORPHOLOGY_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"{instruction}\n\nBOOK EVIDENCE\n"
                        f"{_evidence_context(evidence, excerpt_limit=600)}\n\n/no_think"
                    ),
                },
            ],
            "temperature": 0.2,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 0.1,
            "max_tokens": 760,
            "stream": False,
        }
        return self._complete_card_stage(
            payload,
            lambda value: _validate_grounded_morphology_graph(value, query, evidence),
            repair_tokens=900,
            repair_instruction=(
                "Start again from the supplied evidence. Return one complete, concise "
                "graph JSON object; close every array and object"
            ),
        )

    def generate_morphology_languages(
        self,
        query: str,
        mode: str,
        evidence: list[Evidence],
        graph: dict[str, Any],
    ) -> dict[str, Any]:
        primary = evidence[0]
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": MORPHOLOGY_LANGUAGE_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"MODE: {mode}\nEXACT CENTER FORM: {query}\n"
                        f"ACCEPTED SUMMARY: {graph.get('summary_en', '')}\n"
                        f"PRIMARY BOOK HEADWORD: {primary.headword}\n"
                        f"PRIMARY BOOK TEXT: {primary.excerpt[:1200]}\n\n/no_think"
                    ),
                },
            ],
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 0.0,
            "max_tokens": 512,
            "stream": False,
        }
        return self._complete_card_stage(
            payload,
            lambda value: _validate_morphology_language_draft(value, query),
            repair_tokens=640,
            repair_instruction=(
                "Start again. Return all five non-empty compact language objects. "
                "Use valid Japanese kana, tone-marked Chinese pinyin, and Arabic "
                "script only for both arabic.term and arabic.meaning; never put "
                "Latin letters in either Arabic field"
            ),
        )

    def generate(self, query: str, mode: str, evidence: list[Evidence]) -> dict[str, Any]:
        if mode in {"root", "affix"}:
            graph_stage = self.generate_morphology_graph(query, mode, evidence)
            language_stage = self.generate_morphology_languages(
                query, mode, evidence, graph_stage["value"]
            )
            draft = {**graph_stage["value"], **language_stage["value"]}
            # The graph stage has already sanitized provenance and recorded
            # warnings. Do not run it twice and erase why a claim was repaired.
            _validate_card_draft(draft, mode)
            draft["_preparation_stages"] = {
                "model-morphology-graph": graph_stage,
                "model-morphology-languages": language_stage,
            }
            return draft

        instructions = {
            "word": "Create a WORD ORIGIN card grounded in the retrieved dictionary entry",
            "knowledge": "Create a WORD CARD grounded in the retrieved Word Origins entries",
            "answer": (
                "Create a BOOK ANSWER card. Preserve the selected answer and its reviewed "
                "translations exactly; add a thoughtful reflection, not a prediction"
            ),
            "question": (
                "Create a BOOK QUESTION card. Preserve the selected question and its reviewed "
                "translations exactly; add prompts for thoughtful reflection"
            ),
        }
        instruction = instructions.get(mode, "Create a grounded learning card")
        evidence_context = (
            _evidence_context(evidence, excerpt_limit=400)
            if mode == "word"
            else _evidence_context(evidence)
        )
        user_prompt = (
            f"{instruction} for this request: {query}\n\n"
            f"BOOK EVIDENCE\n{evidence_context}\n\n"
            "/no_think"
        )
        prompts = {
            "word": WORD_ORIGIN_PROMPT,
            "knowledge": WORD_CARD_PROMPT,
            "answer": ANSWER_PROMPT,
            "question": QUESTION_PROMPT,
        }
        token_budgets = {
            "word": 1200,
            "knowledge": 512,
            "answer": 192,
            "question": 192,
        }
        repair_token_budgets = {
            "word": 1600,
            "knowledge": 640,
            "answer": 256,
            "question": 256,
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": prompts[mode],
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.25 if mode in {"word", "root", "affix"} else 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 0.2 if mode in {"word", "root", "affix"} else 1.5,
            "max_tokens": token_budgets[mode],
            "stream": False,
        }
        for attempt in range(2):
            body, _elapsed = self._request(payload)
            content = self._content(body)
            try:
                draft = _extract_json(str(content))
                _validate_card_draft(
                    draft, mode, expected_term=query, evidence=evidence
                )
                return draft
            except ValueError as exc:
                if attempt:
                    raise ModelUnavailable(
                        "model response was not valid JSON after one repair attempt"
                    ) from exc
                payload = {
                    **payload,
                    "temperature": 0.0,
                    "presence_penalty": 0.0,
                    "max_tokens": repair_token_budgets[mode],
                    "messages": [
                        payload["messages"][0],
                        {
                            "role": "user",
                            "content": (
                                f"{user_prompt}\n\nStart again from the supplied evidence. The previous "
                                "response was invalid. Fill every required field with real, "
                                "non-empty content and return exactly one valid JSON object matching "
                                "the required shape, with no markdown. /no_think"
                            ),
                        },
                    ],
                }
        raise AssertionError("unreachable")
