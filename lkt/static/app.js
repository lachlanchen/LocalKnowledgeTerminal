"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
let mode = "word";
let activeCardId = null;

function show(name) {
  ["empty", "loading", "error", "card"].forEach((item) => {
    $(`#${item}-${item === "card" ? "view" : "state"}`).classList.toggle("hidden", item !== name);
  });
}

function text(selector, value) {
  $(selector).textContent = value || "—";
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function pagesLabel(pages) {
  if (!pages || pages.length === 0) return "Page not recorded";
  return `${pages.length > 1 ? "Pages" : "Page"} ${pages.join(", ")}`;
}

function renderCard(card, refreshHistory = true) {
  activeCardId = card.card_id;
  text("#card-mode", card.mode === "word" ? "WORD ORIGIN" : "KNOWLEDGE CARD");
  text("#card-model", `${card.model} · LOCAL`);
  text("#card-title", card.title);
  text("#card-subtitle", card.subtitle);
  text("#card-summary", card.summary_en);
  text("#origin-story", card.origin_story);
  text("#english-term", card.english.term || card.title);
  text("#english-pronunciation", card.english.pronunciation);
  text("#english-meaning", card.english.meaning);
  text("#japanese-meaning", card.japanese.meaning);
  text("#chinese-term", [card.chinese.simplified, card.chinese.traditional && card.chinese.traditional !== card.chinese.simplified ? `／${card.chinese.traditional}` : ""].join(""));
  text("#chinese-pinyin", card.chinese.pinyin);
  text("#chinese-meaning", card.chinese.meaning);
  text("#memory-hook", card.memory_hook);

  const ruby = $("#japanese-ruby");
  ruby.replaceChildren();
  const rubyNode = document.createElement("ruby");
  rubyNode.append(document.createTextNode(card.japanese.term || "—"));
  if (card.japanese.reading) rubyNode.append(element("rt", "", card.japanese.reading));
  ruby.append(rubyNode);

  const points = $("#key-points");
  points.replaceChildren(...(card.key_points || []).map((item) => element("li", "", item)));

  const related = $("#related-terms");
  related.replaceChildren();
  (card.related_terms || []).forEach((item) => {
    const button = element("button", "", item.term);
    button.title = item.note || "Generate this word";
    button.addEventListener("click", () => submitQuery(item.term, "word"));
    related.append(button);
  });

  const evidence = $("#evidence-list");
  evidence.replaceChildren();
  (card.evidence || []).forEach((item) => {
    const section = element("section", "evidence");
    section.append(element("h4", "", item.headword));
    section.append(element("span", "page", pagesLabel(item.pages)));
    section.append(element("blockquote", "", `“${item.excerpt}”`));
    if (item.section) section.append(element("span", "section", item.section));
    evidence.append(section);
  });
  show("card");
  if (refreshHistory) loadHistory();
}

function setMode(nextMode) {
  mode = nextMode;
  all(".mode").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  text("#query-label", mode === "word" ? "Enter a word" : "Ask from the book");
  $("#query").placeholder = mode === "word" ? "Try “abacus”" : "Try “How did counting tools evolve?”";
}

async function submitQuery(query, requestedMode = mode) {
  query = String(query || "").trim();
  if (!query) return;
  setMode(requestedMode);
  $("#query").value = query;
  $("#generate-button").disabled = true;
  show("loading");
  try {
    const response = await fetch("/api/cards", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, mode }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    renderCard(payload);
  } catch (error) {
    text("#error-message", error.message);
    show("error");
  } finally {
    $("#generate-button").disabled = false;
  }
}

async function loadHealth() {
  const container = $("#system-state");
  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    const ready = health.status === "ready";
    container.classList.toggle("ready", ready);
    text("#state-label", ready ? `${health.corpus.entries.toLocaleString()} entries · model ready` : "Model or corpus is starting…");
  } catch (_error) {
    text("#state-label", "Terminal unavailable");
  }
}

async function loadHistory() {
  const history = $("#history");
  try {
    const response = await fetch("/api/cards?limit=10");
    const cards = await response.json();
    history.replaceChildren();
    if (!cards.length) {
      history.append(element("p", "quiet", "No cards yet."));
      return;
    }
    cards.forEach((card) => {
      const button = element("button", "", card.title);
      button.append(element("small", "", card.mode));
      button.addEventListener("click", () => renderCard(card));
      history.append(button);
    });
    if (!activeCardId) renderCard(cards[0], false);
  } catch (_error) {
    history.replaceChildren(element("p", "quiet", "History unavailable."));
  }
}

all(".mode").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
all(".examples button").forEach((button) => button.addEventListener("click", () => submitQuery(button.dataset.query, "word")));
$("#card-form").addEventListener("submit", (event) => { event.preventDefault(); submitQuery($("#query").value); });
$("#refresh-history").addEventListener("click", loadHistory);

setMode("word");
loadHealth();
loadHistory();
setInterval(loadHealth, 30000);
