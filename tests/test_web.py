from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from lkt.knowledge import KnowledgeStore
from lkt.preparation import PreparationPlanner
from lkt.web import (
    card_chat_context,
    chat_messages,
    correction_source_status,
    plan_interactive_word,
    renderable_card,
    word_card_preparation_state,
)


class WebInputTests(unittest.TestCase):
    def test_health_marks_a_missing_correction_index_unready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "missing-freedict.sqlite3"
            status = correction_source_status(SimpleNamespace(freedict_db=database))
            self.assertEqual(
                status,
                {
                    "freedict_eng_ara": {
                        "ready": False,
                        "database": str(database.resolve()),
                    }
                },
            )

    def test_health_marks_a_damaged_correction_index_unready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "damaged-freedict.sqlite3"
            database.write_text("not sqlite", encoding="utf-8")
            status = correction_source_status(SimpleNamespace(freedict_db=database))
            self.assertFalse(status["freedict_eng_ara"]["ready"])

    def test_bare_terminal_defaults_to_the_answer_carousel(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        style = (root / "lkt" / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('let mode = "answer";', script)
        self.assertIn('initialParameters.get("mode") : "answer"', script)
        self.assertIn('class="mode active" data-mode="answer"', page)
        self.assertIn("shuffledModeDeck(carouselCards)", script)
        self.assertIn("limit=1000", script)
        self.assertIn("await loadHistory();", script)
        self.assertIn("carouselCards.length > 1", script)
        self.assertIn("const INNER_SLIDE_DWELL_MS = 18000;", script)
        self.assertIn("const CARD_MIN_DWELL_MS = 30000;", script)
        self.assertIn("activeInnerSlideCount() * INNER_SLIDE_DWELL_MS", script)
        self.assertIn("const ACCEPTED_DECK_SYNC_MS = 30000;", script)
        self.assertIn("async function syncAcceptedDeck()", script)
        self.assertIn("[current, ...newlyAccepted, ...remaining]", script)
        self.assertIn("card-switch-enter", style)
        self.assertIn("inner-slide-enter", style)
        self.assertIn('fetch("/api/intent"', script)
        self.assertIn('ambientRouting = !initialParameters.has("mode")', script)
        self.assertIn('{ selector: ".dimmed", style: { display: "none" } }', script)
        self.assertIn("visibleGraphElements", script)
        self.assertIn('label: [node.form || "—", node.meaning]', script)
        self.assertIn('.join("\\n")', script)
        self.assertIn('element("strong", "graph-node-term"', script)
        self.assertIn('element("span", "graph-node-meaning"', script)
        self.assertIn("graphLanguageCode", script)
        self.assertIn("graphNodeMetrics", script)
        self.assertIn('width: "data(nodeWidth)"', script)
        self.assertIn('height: "data(nodeHeight)"', script)
        self.assertIn("repelGraphNodes", script)
        self.assertIn("+ 42", script)
        self.assertIn("+ 18", script)
        self.assertIn("minZoom: .08", script)
        self.assertIn('"text-overflow-wrap": "anywhere"', script)
        self.assertIn("graph-node-badge-box", style)
        self.assertIn(".graph-node-badge-box.lang-ja", style)
        self.assertIn(".graph-node-badge-box.lang-zh", style)
        self.assertIn("color: #11182b", style)
        self.assertIn("-webkit-text-stroke: 1px rgba(255, 255, 255, .98)", style)
        self.assertIn("const meaningFontSize = isCenter ? 17 : 15", script)
        self.assertIn("const minimumWidth = isCenter ? 300 : 240", script)
        self.assertIn("layoutFocusedGraphForCanvas", script)
        self.assertIn("layoutGraphForCurrentFocus(focus)", script)
        self.assertIn("clockwiseGraphOrder", script)
        self.assertIn("layoutClockwiseGraphNodes", script)
        self.assertIn("graphNodes.length >= 4", script)
        self.assertIn("placeRows(components, -140", script)
        self.assertIn("canvasWidth * .32", script)
        self.assertIn("layoutGraphForCanvas", script)
        self.assertNotIn("-webkit-line-clamp: 3", style)
        self.assertIn("overflow: visible", style)
        self.assertIn('id="graph-node-badges"', page)
        self.assertIn('id="fit-graph"', page)
        self.assertIn("resetGraphAutofit", script)
        self.assertIn("scheduleGraphViewportFit", script)
        self.assertIn("renderGraphFocusAnnotations(focus)", script)
        self.assertIn("appendArabicLetters", script)
        self.assertIn('for (const marker of [". ", "? ", "! ", "; ", ", "])', script)
        self.assertIn("carry.unshift(numericToken)", script)
        self.assertIn("[。！？?!、，；;]", script)
        self.assertIn('element("span", "ruby-cluster")', script)
        self.assertIn("while (counterIndex < tokens.length", script)
        self.assertIn(".ruby-cluster { display: inline-block; white-space: nowrap; }", style)
        self.assertIn("line-height: 2.65", style)
        self.assertIn("inset-block-start: -.32em", style)
        self.assertIn("padding-block-end: .22em", style)
        self.assertIn("margin-inline: .18em", style)
        self.assertIn("thread_id: chatThreadId", script)
        self.assertIn("parent_event_id: chatParentEventId", script)
        self.assertIn('language: "investigation"', script)
        self.assertIn("{ source_card_id: slide.sourceCardId }", script)
        self.assertIn(".investigation-term", style)
        self.assertIn('response.status === 202', script)
        self.assertIn("Each finished step is saved", script)
        self.assertIn("health.lexicons", script)
        self.assertIn("health.autonomous_deck", script)
        self.assertIn("health.autonomous_lexical", script)
        self.assertIn("book cards", script)
        self.assertIn("words planned", script)
        self.assertIn('grammarAnalysis(card, "en", englishText)', script)
        self.assertIn("annotateRubyGrammar", script)
        self.assertIn("grammarParts.forEach", script)
        self.assertIn(".grammar-part.role-subject", style)
        self.assertIn(".grammar-part.role-predicate", style)
        self.assertIn('id="loading-kicker"', page)
        source = (root / "lkt" / "web.py").read_text(encoding="utf-8")
        self.assertIn('requested_mode in _ATOMIC_WORD_MODES', source)
        self.assertIn('extensions["grammar_analyses"]', source)

    def test_bare_ambient_tour_crosses_accepted_mode_decks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'const AMBIENT_MODE_ORDER = ["question", "answer", "knowledge", "word", "root", "affix"];',
            script,
        )
        self.assertIn("const ambientModeDecks = new Map();", script)
        self.assertIn("async function acceptedCardsForMode(cardMode)", script)
        self.assertIn("async function advanceAmbientMode(activityRevision)", script)
        self.assertIn("takeAmbientCard(nextMode, cards)", script)
        self.assertIn("!previous.acceptedIds.has(card.card_id)", script)
        self.assertIn("shuffledAmbientPass(cards, previous?.lastCardId", script)
        self.assertIn("activityRevision !== userActivityRevision", script)
        self.assertIn("function noteActivity(userInitiated = false)", script)
        self.assertIn("if (ambientRouting) {\n      advanceAmbientMode(userActivityRevision);", script)
        self.assertIn('document.addEventListener("pointerdown", () => noteActivity(true)', script)

    def test_old_cards_receive_chinese_ruby_without_database_migration(self) -> None:
        card = {"chinese": {"simplified": "中国", "pinyin": "zhōng guó"}}
        rendered = renderable_card(card)
        self.assertEqual(
            rendered["chinese"]["ruby_tokens"][1],
            {"t": "国", "r": "guó"},
        )

    def test_chat_messages_keep_only_bounded_user_and_assistant_history(self) -> None:
        messages = chat_messages(
            {
                "message": "new question",
                "history": [
                    {"role": "system", "content": "discard me"},
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "earlier reply"},
                    {"role": "invalid", "content": "discard me too"},
                ],
            }
        )
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier reply"},
                {"role": "user", "content": "new question"},
            ],
        )

    def test_chat_rejects_an_empty_message(self) -> None:
        with self.assertRaises(ValueError):
            chat_messages({"message": "  "})

    def test_web_reuses_accepted_cards_unless_refresh_is_requested(self) -> None:
        source = Path(__file__).resolve().parents[1] / "lkt" / "web.py"
        script = source.read_text(encoding="utf-8")
        self.assertIn('payload.get("refresh") is not True', script)
        self.assertIn("service.store.find_active(requested_mode, query)", script)

    def test_linked_word_preparation_exposes_bounded_polling_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = KnowledgeStore(Path(temp) / "knowledge.sqlite3")
            plan = PreparationPlanner(
                store,
                model="Qwen3-4B-Q4_K_M",
                prompt_version="linked-word-v1",
            ).plan_word_card("breakthrough")
            state = word_card_preparation_state(plan, store)
            self.assertEqual(state["status"], "preparing")
            self.assertEqual(state["current_job"], "retrieve-evidence")
            self.assertEqual(state["completed_jobs"], 0)
            self.assertGreater(state["total_jobs"], 1)

            retrieval = store.claim_next_job()
            store.finish_job(retrieval["job_id"])
            state = word_card_preparation_state(plan, store)
            self.assertEqual(state["completed_jobs"], 1)
            self.assertEqual(state["current_job"], "prepare-meaning")

    def test_every_lexical_view_uses_the_resumable_atomic_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for mode in ("knowledge", "word", "root", "affix"):
                store = KnowledgeStore(Path(temp) / f"{mode}.sqlite3")
                plan = plan_interactive_word(store, "inspection", mode, "local-qwen")
                jobs = store.jobs_for_subject(plan.subject_key)
                job_types = {job["job_type"] for job in jobs}
                if mode == "knowledge":
                    self.assertIn("compose-word-card", job_types)
                    self.assertNotIn("expand-origin-branches", job_types)
                    self.assertTrue(
                        all(
                            job["prompt_version"] == "interactive-word-card-v1"
                            for job in jobs
                        )
                    )
                else:
                    self.assertIn("expand-origin-branches", job_types)
                    self.assertIn("compose-origin-card", job_types)
                    self.assertTrue(
                        all(
                            job["prompt_version"] == "interactive-origin-graph-v3"
                            for job in jobs
                        )
                    )
                state = word_card_preparation_state(plan, store, mode)
                self.assertEqual(state["mode"], mode)
                self.assertEqual(state["current_job"], "retrieve-evidence")

    def test_card_chat_context_keeps_retrieved_source(self) -> None:
        context = card_chat_context(
            {
                "title": "Abacus",
                "summary_en": "A counting frame.",
                "origin_story": "It passed through Greek and Latin.",
                "english": {"term": "abacus"},
                "evidence": [
                    {
                        "entry_id": "entry-0003",
                        "pages": [12],
                        "excerpt": "The source book excerpt.",
                    }
                ],
            }
        )
        self.assertIn("Abacus", context)
        self.assertIn("entry-0003 page 12", context)
        self.assertIn("The source book excerpt", context)


if __name__ == "__main__":
    unittest.main()
