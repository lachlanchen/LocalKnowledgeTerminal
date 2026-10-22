from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lkt.store import CardStore


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for browser logic tests")
class PageViewJavaScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (ROOT / "lkt/static/app.js").read_text(encoding="utf-8")

    def run_js(self, names: tuple[str, ...], body: str) -> None:
        functions = []
        for name in names:
            match = re.search(
                rf"^(?:async )?function {re.escape(name)}\(", self.script, re.MULTILINE
            )
            self.assertIsNotNone(match, name)
            start = match.start()
            end = self.script.index("\n}", start) + 2
            functions.append(self.script[start:end])
        prelude = """
const assert = require("node:assert/strict");
const MODE_COPY = Object.fromEntries(
  ["question", "answer", "knowledge", "word", "root", "affix", "chat"].map(mode => [mode, {}])
);
const activeAmbientModeOrder = () => ["question", "answer", "word"];
const NON_VISUAL_GRAPH_RELATIONS = new Set(["has-language", "has-period", "in-language", "language", "language-of", "period"]);
"""
        result = subprocess.run(
            [shutil.which("node"), "-"],
            input=prelude + "\n".join(functions) + "\n" + body,
            text=True, capture_output=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_json_timeout_covers_response_body_consumption(self) -> None:
        self.run_js(("fetchJsonWithTimeout",), """
const window={setTimeout,clearTimeout};
let requestSignal=null;
const fetch=async(_resource,options)=>{
 requestSignal=options.signal;
 return {json:()=>new Promise((_resolve,reject)=>{
  requestSignal.addEventListener("abort",()=>{
   const error=new Error("aborted");error.name="AbortError";reject(error);
  },{once:true});
 })};
};
(async()=>{
 await assert.rejects(fetchJsonWithTimeout("/slow",{},5),error=>error.name==="AbortError");
 assert.equal(requestSignal.aborted,true);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_current_word_keeps_its_component_edges(self) -> None:
        self.run_js(("normalizeLexicalView",), """
const view = {
  subject_entity_id: "current", nodes: [
    {id:"current",type:"term",label:"evoke"},
    {id:"root",type:"morpheme",payload:{kind:"root"},label:"vok"},
    {id:"prefix",type:"morpheme",payload:{kind:"prefix"},label:"ex-"},
    {id:"derivative",type:"term",label:"related word"}
  ], edges: [
    {id:"shared",source:"current",target:"root",relation:"shares-component"},
    {id:"part",source:"current",target:"prefix",relation:"has-component"},
    {id:"other",source:"derivative",target:"root",relation:"shares-component"}
  ]
};
const graph = normalizeLexicalView({mode:"word",title:"evoke"},view);
assert.equal(graph.center_id,"current");
assert.deepEqual(graph.nodes.map(node=>node.id),["current","root","prefix"]);
assert.deepEqual(graph.edges.map(edge=>edge.id),["shared","part"]);
""")

    def test_reverse_edges_and_explicit_focus_are_not_hidden(self) -> None:
        self.run_js(("normalizeLexicalView",), """
const view = {
  focus_entity_ids:["word"], nodes:[
    {entity_id:"word",kind:"term",form:"focus"},
    {entity_id:"root",kind:"root",form:"root"},
    {entity_id:"other",kind:"word",form:"other"}
  ], edges:[
    {source_entity_id:"root",target_entity_id:"word",relationship:"shares-component"},
    {source_entity_id:"root",target_entity_id:"other",relationship:"shares-component"}
  ]
};
const graph=normalizeLexicalView({mode:"word"},view);
assert.equal(graph.center_id,"word");
assert.deepEqual(graph.nodes.map(node=>node.id),["word","root"]);
assert.equal(graph.edges.length,1);
""")

    def test_subject_prefix_resolves_without_hiding_the_subject(self) -> None:
        self.run_js(("normalizeLexicalView",), """
const graph=normalizeLexicalView(
  {mode:"word",extensions:{knowledge_subject:"term:term-abc"}},
  {nodes:[{id:"term-abc",type:"term"},{id:"root",kind:"root"}],
   edges:[{source:"term-abc",target:"root",relation:"shares-component"}]}
);
assert.equal(graph.center_id,"term-abc");
assert.equal(graph.edges.length,1);
""")

    def test_morphology_and_recentered_views_keep_derived_words(self) -> None:
        self.run_js(("normalizeLexicalView",), """
for (const mode of ["root","affix","word"]) {
  const view={subject_entity_id:"root",include_shared_words:mode==="word",
    nodes:[{id:"root",kind:"root"},{id:"other",type:"term"}],
    edges:[{source:"other",target:"root",relation:"shares-component"}]};
  const graph=normalizeLexicalView({mode},view);
  assert.equal(graph.nodes.length,2);
  assert.equal(graph.edges.length,1);
}
""")

    def test_language_and_period_remain_metadata(self) -> None:
        self.run_js(("normalizeLexicalView",), """
const graph=normalizeLexicalView({mode:"word"},{
 subject_entity_id:"w",
 nodes:[{id:"w",type:"term"},{id:"lang",kind:"language",label:"Latin"},
        {id:"period",payload:{kind:"period",label:"Classical"}}],
 edges:[{source:"w",target:"lang",relation:"has-language"},
        {source:"w",target:"period",relation:"has-period"}]
});
assert.equal(graph.nodes.length,1);
assert.equal(graph.edges.length,0);
assert.equal(graph.nodes[0].language,"Latin");
assert.equal(graph.nodes[0].period,"Classical");
""")

    def test_page_url_round_trip_preserves_unrelated_parameters(self) -> None:
        self.run_js(("readPageRoute", "pageViewUrl", "pageSlideIndex"), """
const url=pageViewUrl("http://pi:8090/?display=1&theme=paper#canvas",{
 mode:"root",cardId:"saved-card",slide:3,focusId:"branch:ex- / root",
 entityId:"morpheme-123",language:"ar",loop:true});
const route=readPageRoute(url.href);
assert.equal(route.mode,"root");
assert.equal(route.cardId,"saved-card");
assert.equal(route.slide,3);
assert.equal(route.focusId,"branch:ex- / root");
assert.equal(route.entityId,"morpheme-123");
assert.equal(route.language,"ar");
assert.equal(route.display,true);
assert.equal(route.loop,true);
assert.equal(url.searchParams.get("theme"),"paper");
assert.equal(url.hash,"#canvas");
assert.equal(pageSlideIndex(route.slide,7),2);
""")

    def test_new_tab_removes_stale_card_branch_and_language(self) -> None:
        self.run_js(("pageViewUrl",), """
const url=pageViewUrl("http://pi/?card=old&mode=root&slide=9&entity=e&focus=f&language=ar&display",{
 mode:"chat"});
assert.equal(url.searchParams.get("mode"),"chat");
for(const name of ["card","slide","focus","entity","language"]) assert.equal(url.searchParams.has(name),false);
assert.equal(url.searchParams.has("display"),true);
""")

    def test_bad_modes_and_slide_numbers_are_bounded(self) -> None:
        self.run_js(("readPageRoute", "pageSlideIndex"), """
for (const mode of ["missing","__proto__","constructor"]) {
 assert.equal(readPageRoute("http://pi/?mode="+mode).mode,"question");
}
for(const number of [-9,0,NaN,Infinity]) assert.equal(pageSlideIndex(number,4),0);
assert.equal(pageSlideIndex(999,4),3);
assert.equal(pageSlideIndex(3.9,4),2);
assert.equal(pageSlideIndex(8,0),0);
assert.equal(readPageRoute("http://pi/?card="+"x".repeat(500)).cardId.length,100);
""")

    def test_url_updates_are_coalesced_without_reload_or_history_growth(self) -> None:
        self.run_js(("syncPageUrl", "pageViewUrl"), """
let restoringPageRoute=false,pageUrlSyncQueued=false;
let ambientRouting=true,autoplayEnabled=true;
let mode="word",visibleView="card",activeCard={mode:"word",card_id:"saved"};
let graphFocusAreas=[{id:"branch-1"}],graphFocusIndex=0,recenteredEntityId="";
let sentenceSlides=[],sentenceSlideIndex=0,alternateIndex=0;
let writes=0;
const window={location:{href:"http://pi/?display=1"},history:{state:{kept:1},
 replaceState(state,title,url){writes++;assert.equal(state.kept,1);window.location.href=url.href;}}};
(async()=>{
 syncPageUrl();syncPageUrl();await Promise.resolve();
 assert.equal(writes,1);
 assert.equal(new URL(window.location.href).searchParams.get("card"),"saved");
 assert.equal(new URL(window.location.href).searchParams.get("slide"),"1");
 restoringPageRoute=true;syncPageUrl();await Promise.resolve();assert.equal(writes,1);
 restoringPageRoute=false;mode="affix";syncPageUrl();await Promise.resolve();
 assert.equal(writes,2);
 assert.equal(new URL(window.location.href).searchParams.has("card"),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_pinned_card_survives_recent_history_window(self) -> None:
        self.run_js(("loadHistory",), """
let mode="word",historyRequestRevision=0,pinnedPageCardId="older";
let activeCard={card_id:"older",mode:"word"},allSavedCards=[];
const fetchModeSummaries=async()=>({cards:[{card_id:"new",mode:"word"}]});
let rebuilt=false;
function rebuildModeCarousel(openLatest){
 assert.equal(openLatest,false);
 assert.deepEqual(allSavedCards.map(card=>card.card_id),["older","new"]);
 rebuilt=true;
}
(async()=>{await loadHistory();assert.equal(rebuilt,true);})()
 .catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_late_background_sync_cannot_replace_shared_view(self) -> None:
        self.run_js(("syncAcceptedDeck",), """
let restoringPageRoute=false,pinnedPageCardId="",mode="word",visibleView="card";
let activeCard={mode:"word",card_id:"old"},pageRouteRevision=1,allSavedCards=["unchanged"];
let deckSyncInFlight=false;
let resolve,calls=0;
const fetchModeSummaries=()=>{calls++;return new Promise(done=>{resolve=done;});};
(async()=>{
 const pending=syncAcceptedDeck();
 pinnedPageCardId="linked";pageRouteRevision++;
 resolve({cards:[],generationChanged:true});await pending;
 assert.deepEqual(allSavedCards,["unchanged"]);
 await syncAcceptedDeck();assert.equal(calls,1);
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_ambient_card_survives_optional_summary_failure(self) -> None:
        self.run_js(("fetchAmbientMode",), """
const modeSummaryCache=new Map([["word",{cards:[{card_id:"new",mode:"word"}]}]]);
const takeAmbientCard=(_mode,cards)=>cards[0];
const hydrateAcceptedSummary=async(card)=>({...card,summary_only:false});
const randomAcceptedCard=async()=>{throw new Error("random fallback not expected");};
const fetchModeSummaries=async()=>{throw new Error("optional summary failed");};
(async()=>{
 const result=await fetchAmbientMode("word");
 assert.equal(result.selected.card_id,"new");
 assert.equal(result.cards[0].card_id,"new");
 await Promise.resolve();
})().catch(error=>{console.error(error);process.exitCode=1;});
""")

    def test_single_card_carousel_rearms_its_watchdog(self) -> None:
        self.run_js(("scheduleCarousel",), """
let autoplayTimer=null,autoplayEnabled=true,ambientRouting=false,mode="word";
let carouselCards=[{card_id:"only"}],userActivityRevision=0;
const CARD_MIN_DWELL_MS=30000,INNER_SLIDE_DWELL_MS=18000;
const activeInnerSlideCount=()=>1;
const prefetchAmbientMode=()=>{};
const runAmbientAdvance=()=>{};
const navigateCards=()=>{throw new Error("single card cannot advance");};
const callbacks=[];
const clearTimeout=()=>{};
const setTimeout=(callback)=>{callbacks.push(callback);return callbacks.length;};
scheduleCarousel();
assert.equal(callbacks.length,1);
callbacks.shift()();
assert.equal(callbacks.length,1);
""")

    def test_cancelled_restore_invalidates_late_fetches(self) -> None:
        self.run_js(("cancelPageRestore",), """
let pageRouteRevision=1,pageRouteController=new AbortController();
let restoringPageRoute=true,pinnedPageCardId="linked",cardHydrationRevision=1;
let activeCardHydrationController=new AbortController();
let graphRecenterRevision=1,userActivityRevision=1;
const routeSignal=pageRouteController.signal,cardSignal=activeCardHydrationController.signal;
cancelPageRestore();
assert.equal(routeSignal.aborted,true);
assert.equal(cardSignal.aborted,true);
assert.equal(restoringPageRoute,false);
assert.equal(pinnedPageCardId,"");
assert.equal(pageRouteRevision,2);
assert.equal(graphRecenterRevision,2);
""")


class PublishedRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = CardStore(Path(self.temporary.name) / "cards.sqlite3")

    def insert(self, status: str, validation: str = "accepted") -> str:
        card_id = f"{status}-{validation}"
        payload = {"card_id": card_id, "mode": "word", "title": "saved"}
        with closing(sqlite3.connect(self.store.database)) as connection:
            connection.execute(
                """INSERT INTO cards
                   (card_id,mode,query,title,created_at,payload,status,validation_state)
                   VALUES (?, 'word', 'saved', 'saved', '2026-09-24', ?, ?, ?)""",
                (card_id, json.dumps(payload), status, validation),
            )
            connection.commit()
        return card_id

    def test_accepted_current_and_superseded_links_resolve(self) -> None:
        for status in ("active", "superseded"):
            with self.subTest(status=status):
                card_id = self.insert(status)
                self.assertEqual(self.store.get_published_revision(card_id)["card_id"], card_id)
        self.assertIsNone(self.store.get_accepted("superseded-accepted"))
        self.assertEqual(len(self.store.recent()), 1)

    def test_private_withdrawn_and_unaccepted_cards_never_resolve(self) -> None:
        for status, validation in (
            ("candidate", "accepted"), ("archived", "accepted"),
            ("active", "candidate"), ("active", "rejected"),
            ("superseded", "rejected"), ("superseded", "legacy-unreviewed"),
        ):
            with self.subTest(status=status, validation=validation):
                card_id = self.insert(status, validation)
                self.assertIsNone(self.store.get_published_revision(card_id))

    def test_missing_and_sql_like_ids_do_not_resolve(self) -> None:
        self.insert("active")
        self.assertIsNone(self.store.get_published_revision("missing"))
        self.assertIsNone(self.store.get_published_revision("' OR 1=1 --"))


if __name__ == "__main__":
    unittest.main()
