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
    def test_arrow_keys_map_vertical_modes_and_horizontal_cards(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "lkt" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('ArrowUp: { axis: "mode", step: -1 }', script)
        self.assertIn('ArrowDown: { axis: "mode", step: 1 }', script)
        self.assertIn('ArrowLeft: { axis: "card", step: -1 }', script)
        self.assertIn('ArrowRight: { axis: "card", step: 1 }', script)
        self.assertIn(
            "const nextIndex = (currentIndex + step + buttons.length) % buttons.length;",
            script,
        )
        self.assertIn(
            "carouselIndex = (carouselIndex + step + carouselCards.length) % carouselCards.length;",
            script,
        )
        self.assertIn(
            'if (navigation.axis === "mode") navigateModes(navigation.step);',
            script,
        )
        self.assertIn(
            'if (navigation.axis === "card") navigateCards(navigation.step);',
            script,
        )

    def test_arrow_navigation_preserves_editing_and_modified_keys(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "lkt" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "input, textarea, select, [contenteditable]:not([contenteditable='false'])",
            script,
        )
        self.assertIn(
            "event.altKey || event.ctrlKey || event.metaKey || event.shiftKey",
            script,
        )
        self.assertIn(
            "navigation && !hasNavigationModifier(event) && !isEditingTarget(event.target)",
            script,
        )

    def test_click_controls_share_mode_and_card_navigation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            'button.addEventListener("click", () => activateMode(button.dataset.mode))',
            script,
        )
        self.assertIn(
            '$("#previous-card").addEventListener("click", () => navigateCards(-1));',
            script,
        )
        self.assertIn(
            '$("#next-card").addEventListener("click", () => navigateCards(1));',
            script,
        )
        self.assertIn('id="previous-card"', page)
        self.assertIn('id="next-card"', page)

    def test_rapid_tab_history_ignores_out_of_order_responses(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "lkt" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        stale_guard = (
            "if (requestRevision !== historyRequestRevision "
            "|| mode !== requestedMode) return;"
        )
        self.assertIn("const requestedMode = mode;", script)
        self.assertIn("const requestRevision = ++historyRequestRevision;", script)
        self.assertGreaterEqual(script.count(stale_guard), 2)

        current_mode = "question"
        current_revision = 0
        rendered: list[str] = []
        errors: list[str] = []

        def begin(requested_mode: str) -> tuple[int, str]:
            nonlocal current_mode, current_revision
            current_mode = requested_mode
            current_revision += 1
            return current_revision, requested_mode

        def finish(
            request: tuple[int, str], label: str, *, failed: bool = False
        ) -> None:
            revision, requested_mode = request
            if revision != current_revision or requested_mode != current_mode:
                return
            (errors if failed else rendered).append(label)

        old_request = begin("question")
        newest_request = begin("knowledge")
        finish(newest_request, "newest word deck")
        finish(old_request, "stale question deck")
        finish(old_request, "stale error", failed=True)

        self.assertEqual(rendered, ["newest word deck"])
        self.assertEqual(errors, [])

    def test_health_marks_a_missing_correction_index_unready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "missing-freedict.sqlite3"
            jmdict = Path(temp) / "missing-jmdict.sqlite3"
            status = correction_source_status(
                SimpleNamespace(freedict_db=database, jmdict_db=jmdict)
            )
            self.assertEqual(
                status,
                {
                    "freedict_eng_ara": {
                        "ready": False,
                        "database": str(database.resolve()),
                    },
                    "jmdict": {
                        "ready": False,
                        "database": str(jmdict.resolve()),
                    },
                },
            )

    def test_health_marks_a_damaged_correction_index_unready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "damaged-freedict.sqlite3"
            database.write_text("not sqlite", encoding="utf-8")
            status = correction_source_status(
                SimpleNamespace(
                    freedict_db=database,
                    jmdict_db=Path(temp) / "missing-jmdict.sqlite3",
                )
            )
            self.assertFalse(status["freedict_eng_ara"]["ready"])

    def test_bare_terminal_defaults_to_the_selected_ambient_loop(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        style = (root / "lkt" / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('let mode = "question";', script)
        self.assertIn(': activeAmbientModeOrder()[0];', script)
        self.assertIn('class="mode active" data-mode="question"', page)
        self.assertIn("shuffledModeDeck(carouselCards)", script)
        self.assertIn("limit=1000", script)
        self.assertIn("await loadHistory();", script)
        self.assertIn("carouselCards.length > 1", script)
        self.assertIn("const INNER_SLIDE_DWELL_MS = 18000;", script)
        self.assertIn("const CARD_MIN_DWELL_MS = 30000;", script)
        self.assertIn("activeInnerSlideCount() * INNER_SLIDE_DWELL_MS", script)
        self.assertIn('const DISPLAY_SETTINGS_STORAGE_KEY = "lkt-display-settings-v1";', script)
        self.assertIn('book: ["en", "ja", "zh"]', script)
        self.assertIn('lexical: ["en", "ja", "zh", "fr", "ar"]', script)
        self.assertIn('if (!displaySettings.randomCards) return deck;', script)
        self.assertIn('languageEnabled("en", card.mode)', script)
        self.assertIn('languageEnabled("ja", card.mode)', script)
        self.assertIn('languageEnabled("zh", card.mode)', script)
        self.assertIn('id="settings-button"', page)
        self.assertIn('id="settings-dialog"', page)
        self.assertIn('id="random-cards-setting"', page)
        self.assertIn('id="settings-mode-options"', page)
        self.assertEqual(page.count("data-setting-mode"), 6)
        self.assertIn('ambientModes: [...AMBIENT_MODE_ORDER]', script)
        self.assertIn("function activeAmbientModeOrder()", script)
        self.assertIn("function ambientIndexAfter(cardMode)", script)
        self.assertIn("ambientModeIndex = ambientIndexAfter(initialMode)", script)
        self.assertIn('all("[data-setting-mode]:checked")', script)
        self.assertIn("displaySettings.ambientModes = AMBIENT_MODE_ORDER.filter", script)
        self.assertIn(".settings-dialog", style)
        self.assertIn("const ACCEPTED_DECK_SYNC_MS = 30000;", script)
        self.assertIn("const HISTORY_DOT_LIMIT = 18;", script)
        self.assertIn("carouselCards.slice(start, start + HISTORY_DOT_LIMIT)", script)
        self.assertIn("button.dataset.carouselIndex = String(index)", script)
        self.assertIn("async function syncAcceptedDeck()", script)
        self.assertIn('["card", "empty"].includes(visibleView)', script)
        self.assertIn("acceptedIds.has(visibleCurrent?.card_id)", script)
        self.assertIn("[current, ...newlyAccepted, ...remaining]", script)
        self.assertIn("card-switch-enter", style)
        self.assertIn("inner-slide-enter", style)
        self.assertIn('fetch("/api/intent"', script)
        self.assertIn('ambientRouting = !initialParameters.has("mode")', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn('new URLSearchParams(location.search).has("display")', script)
        self.assertIn("window.close();", script)
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
        self.assertRegex(
            style,
            r"\.graph-node-copy\s*\{[^}]*overflow: hidden;",
        )
        self.assertRegex(
            style,
            r"\.graph-node-term\s*\{[^}]*overflow: hidden;",
        )
        self.assertRegex(
            style,
            r"\.graph-node-meaning\s*\{[^}]*overflow: hidden;",
        )
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

    def test_word_card_tickers_move_only_measured_overflow(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        style = (root / "lkt" / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const KNOWLEDGE_TICKER_SELECTORS", script)
        self.assertIn('[data-overflow-role]:not(#evidence-list)', script)
        self.assertIn('lane.dataset.overflowRole === "term"', script)
        self.assertIn("copyWidth > lane.clientWidth + 1", script)
        self.assertIn("copyHeight > lane.clientHeight + 1", script)
        self.assertIn('duplicate.setAttribute("aria-hidden", "true")', script)
        self.assertIn('lane.classList.add("is-overflowing")', script)
        self.assertIn('lane.dataset.motionAxis = axis', script)
        self.assertIn('"--ticker-duration",', script)
        self.assertIn("(copyWidth + gap) / 34", script)
        self.assertIn("(travel + gap) / 24", script)
        self.assertIn("document.fonts.ready.then", script)
        self.assertIn('window.addEventListener("resize", scheduleKnowledgeTickers)', script)
        self.assertIn("knowledge-bank-roll", style)
        self.assertIn("knowledge-bank-roll-vertical", style)
        vertical_copy = style.split(
            '.knowledge-ticker-lane[data-motion-axis="vertical"] .knowledge-ticker-copy {',
            1,
        )[1].split("}", 1)[0]
        self.assertIn("min-width: 0", vertical_copy)
        self.assertIn("width: 100%", vertical_copy)
        self.assertIn("max-width: 100%", vertical_copy)
        self.assertIn("white-space: normal", vertical_copy)
        self.assertIn("overflow-wrap: anywhere", vertical_copy)
        self.assertIn('data-overflow-role="term"', page)
        self.assertIn('data-overflow-role="meaning"', page)
        self.assertIn("prefers-reduced-motion: reduce", style)
        self.assertIn("scrollbar-width: none", style)
        self.assertNotIn("fitKnowledgeTickerFont", script)

    def test_graph_copy_autofits_and_derivatives_stay_outside_canvas(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        style = (root / "lkt" / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function fitGraphNodeCopy(box)", script)
        self.assertIn(
            'copy.querySelectorAll(".graph-node-term, .graph-node-meaning")',
            script,
        )
        self.assertIn("part.scrollWidth <= part.clientWidth + 1", script)
        self.assertIn("part.scrollHeight <= part.clientHeight + 1", script)
        self.assertIn("const meaningRatio = meaning ? baseMeaning / baseTerm : 0", script)
        self.assertIn("badges.forEach(fitGraphNodeCopy)", script)
        derivative_filter = script.index("const derivativeNodeIds = new Set()")
        normalized_nodes = script.index("const nodes = view.nodes.map", derivative_filter)
        normalized_edges = script.index("const edges = displayEdges.map", normalized_nodes)
        self.assertLess(derivative_filter, normalized_nodes)
        self.assertLess(normalized_nodes, normalized_edges)
        self.assertIn(
            'if (relationship !== "shares-component") return true;', script
        )
        self.assertIn("if (source) derivativeNodeIds.add(source)", script)
        self.assertIn("!derivativeNodeIds.has(node.id)", script)
        self.assertIn(
            'if (!["word", "root", "affix"].includes(card.mode)) return [];',
            script,
        )
        self.assertIn("const GRAPH_DERIVATIVE_LIMIT = 6", script)
        self.assertIn("for (const item of card.related_terms || [])", script)
        self.assertIn("derivatives.length >= GRAPH_DERIVATIVE_LIMIT", script)
        self.assertIn('element("strong", "graph-derivative-term", item.term)', script)
        self.assertIn('id="graph-layout"', page)
        self.assertIn('id="graph-viewport"', page)
        self.assertIn('id="graph-derivatives-left"', page)
        self.assertIn('id="graph-derivatives-right"', page)
        viewport = page.index('id="graph-viewport"')
        canvas = page.index('id="origin-canvas"')
        rails = page.index('id="graph-derivative-rails"')
        self.assertLess(viewport, canvas)
        self.assertLess(canvas, rails)
        self.assertIn('grid-template-areas: "left viewport right"', style)
        self.assertIn('grid-template-areas: "viewport" "derivatives"', style)

    def test_graph_prefers_canonical_lexical_view_with_legacy_fallback(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "lkt" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        lexical = script.index(
            "const lexical = normalizeLexicalView(card, card.extensions?.lexical_view);"
        )
        compatibility = script.index(
            "const rich = card.extensions?.morphology_graph;", lexical
        )
        legacy = script.index(
            "const legacy = Array.isArray(card.origin_graph)", compatibility
        )
        self.assertLess(lexical, compatibility)
        self.assertLess(compatibility, legacy)
        self.assertIn("function normalizeLexicalView(card, view)", script)
        self.assertIn("node?.id || node?.entity_id", script)
        self.assertIn("edge?.id || edge?.assertion_id", script)
        self.assertIn("edge?.relation || edge?.relationship", script)
        self.assertIn("view.focus_entity_ids", script)
        self.assertIn("evidence_ids: Array.isArray(edge?.evidence_ids)", script)

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
        self.assertIn("const enabledModes = activeAmbientModeOrder();", script)
        self.assertIn("attempt < enabledModes.length", script)
        self.assertIn("takeAmbientCard(nextMode, cards)", script)
        self.assertIn("!previous.acceptedIds.has(card.card_id)", script)
        self.assertIn("shuffledAmbientPass(cards, previous?.lastCardId", script)
        self.assertIn("activityRevision !== userActivityRevision", script)
        self.assertIn("function noteActivity(userInitiated = false)", script)
        self.assertIn("if (ambientRouting) {\n      advanceAmbientMode(userActivityRevision);", script)
        self.assertIn('document.addEventListener("pointerdown", () => noteActivity(true)', script)

    def test_browser_renders_all_bounded_evidence_with_recorded_metadata(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "lkt" / "static" / "app.js").read_text(encoding="utf-8")
        style = (root / "lkt" / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "lkt" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("evidenceItems.forEach((item) => {", script)
        self.assertNotIn("(card.evidence || []).slice(0, 1)", script)
        self.assertIn("metadata.push(pagesLabel(item.pages));", script)
        self.assertIn("metadata.push(`Section · ${item.section}`);", script)
        self.assertIn("metadata.push(`Locator · ${item.locator}`);", script)
        self.assertNotIn("Source location recorded by corpus", script)
        self.assertIn("#evidence-list { display: flex; min-height: 0;", style)
        self.assertIn(".evidence-list-many .evidence", style)
        self.assertIn("function setMorphologyEvidenceExpanded(expanded)", script)
        self.assertIn("toggleMorphologyEvidencePanel", script)
        self.assertIn("scheduleGraphViewportFit(120)", script)
        self.assertIn('id="evidence-toggle"', page)
        self.assertIn('aria-controls="evidence-panel"', page)
        self.assertIn(".card-view.mode-root.evidence-expanded", style)
        self.assertIn("minmax(118px, 12%)", style)
        self.assertIn("minmax(270px, 23%)", style)
        self.assertIn(".mode-root #evidence-list .evidence blockquote", style)

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

    def test_card_submission_keeps_its_mode_and_cancels_stale_polling(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "lkt" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("const submittedQuery = String(query || \"\").trim();", script)
        self.assertIn("const submittedMode = mode;", script)
        self.assertIn("const requestRevision = cardSubmissionRevision;", script)
        self.assertIn("activeCardSubmissionController?.abort();", script)
        self.assertIn("signal: requestController.signal,", script)
        self.assertIn("if (requestRevision !== cardSubmissionRevision) return;", script)
        self.assertIn("query: submittedQuery,", script)
        self.assertIn("mode: submittedMode,", script)
        self.assertIn("retry_failed: poll === 0,", script)
        self.assertIn("if (nextMode !== mode) {", script)
        self.assertNotIn("JSON.stringify({ query, mode, ...context })", script)

    def test_chat_rejects_an_empty_message(self) -> None:
        with self.assertRaises(ValueError):
            chat_messages({"message": "  "})

    def test_web_reuses_accepted_cards_unless_refresh_is_requested(self) -> None:
        source = Path(__file__).resolve().parents[1] / "lkt" / "web.py"
        script = source.read_text(encoding="utf-8")
        self.assertIn('payload.get("refresh") is not True', script)
        self.assertIn("service.store.find_active(requested_mode, query)", script)
        self.assertIn('payload.get("retry_failed") is True', script)
        self.assertIn("knowledge.requeue_failed_jobs(plan.jobs.values())", script)

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
            self.assertFalse(state["generation_ready"])
            self.assertIn("heartbeat", state["generation_blocker"])

            store.record_worker_heartbeat("")
            state = word_card_preparation_state(plan, store)
            self.assertTrue(state["generation_ready"])

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
                            job["prompt_version"] == "interactive-origin-graph-v4"
                            for job in jobs
                        )
                    )
                state = word_card_preparation_state(plan, store, mode)
                self.assertEqual(state["mode"], mode)
                self.assertEqual(state["current_job"], "retrieve-evidence")
                self.assertTrue(all(job["priority"] < 0 for job in jobs))

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
