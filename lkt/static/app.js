"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
let mode = "word";
let activeCardId = null;
let activeCard = null;
let visibleView = "empty";
let chatHistory = [];
let chatContextCardId = "";
let chatContextTitle = "";
let carouselCards = [];
let carouselIndex = -1;
let autoplayEnabled = true;
let autoplayTimer = null;

const MODE_COPY = {
  word: {
    card: "WORD ORIGIN",
    label: "Enter a word",
    placeholder: "Try “abacus”",
    examples: ["abacus", "algorithm", "memory"],
    kicker: "WORD ORIGINS · MULTILINGUAL MEMORY",
    title: "Trace a word through time, then carry it across languages.",
    description: "Every historical claim stays beside the passage and page that supports it.",
    narrative: "ORIGIN STORY",
  },
  knowledge: {
    card: "WORD CARD",
    label: "Ask from Word Origins",
    placeholder: "Try “How did counting tools evolve?”",
    examples: ["counting tools", "language change", "memory"],
    kicker: "WORD CARD · BOOK-GROUNDED NOTE",
    title: "Connect several entries into one memorable idea.",
    description: "The local model can explain, but retrieved book passages remain the evidence.",
    narrative: "BOOK-GROUNDED NOTE",
  },
  answer: {
    card: "BOOK ANSWER",
    label: "Ask, then draw an answer",
    placeholder: "Try “Should I begin now?”",
    examples: ["Should I begin now?", "What matters today?", "Is this the right time?"],
    kicker: "BOOK OF ANSWERS · REFLECTION",
    title: "Hold a question in mind, then draw a cited answer.",
    description: "The selected answer and reviewed translations come directly from your local book.",
    narrative: "REFLECTION",
  },
  question: {
    card: "BOOK QUESTION",
    label: "Choose a theme",
    placeholder: "Try “technology”",
    examples: ["technology", "friendship", "courage"],
    kicker: "BOOK OF QUESTIONS · CURIOSITY",
    title: "Find a question worth carrying through the day.",
    description: "Search by theme; when no direct match exists, LKT draws a reproducible question.",
    narrative: "REFLECTION PROMPT",
  },
  chat: {
    card: "MODEL LAB",
    label: "Prompt the local model",
    placeholder: "Try “Explain recursion simply”",
    examples: ["Explain recursion simply", "用中文解释RAG", "日本語で短い物語を書いて"],
    kicker: "RAW QWEN · QUALITY + SPEED",
    title: "Test the local model without visual noise.",
    description: "This benchmark reports time and token speed; it does not attach book citations.",
    narrative: "RAW RESPONSE",
  },
};

const SOURCE_TITLES = {
  word: "Word Origins",
  knowledge: "Word Origins",
  answer: "The Book of Answers",
  question: "The Book of Questions",
};

function show(name) {
  const views = {
    empty: "#empty-state",
    loading: "#loading-state",
    error: "#error-state",
    card: "#card-view",
    chat: "#chat-view",
  };
  Object.entries(views).forEach(([item, selector]) => {
    $(selector).classList.toggle("hidden", item !== name);
  });
  visibleView = name;
}

function text(selector, value) {
  $(selector).textContent = value || "—";
}

function optionalText(selector, value) {
  const node = $(selector);
  node.textContent = value || "";
  node.classList.toggle("hidden", !value);
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function renderChatMessage(role, content, metrics = null, pending = false) {
  const article = element("article", `chat-message ${role}${pending ? " pending" : ""}`);
  article.append(element("span", "", role === "user" ? "YOU" : "QWEN · LOCAL"));
  article.append(element("p", "", content));
  if (metrics) {
    const row = element("div", "chat-metrics");
    const values = [
      ["TOTAL", `${Number(metrics.elapsed_seconds || 0).toFixed(2)} s`],
      ["OUTPUT", `${metrics.completion_tokens || 0} tokens`],
      ["SPEED", `${Number(metrics.tokens_per_second || 0).toFixed(2)} tok/s`],
      ["PROMPT", `${metrics.prompt_tokens || 0} tokens`],
    ];
    if (metrics.saved) values.push(["LEDGER", "SAVED"]);
    values.forEach(([label, value]) => {
      const item = element("span");
      item.append(element("strong", "", `${label} `), document.createTextNode(value));
      row.append(item);
    });
    article.append(row);
  }
  $("#chat-messages").append(article);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
  return article;
}

function resetChat() {
  chatHistory = [];
  $("#chat-messages").replaceChildren();
  $("#lab-context").textContent = chatContextCardId
    ? `Discussing saved card · ${chatContextTitle}`
    : "Uncited · saved separately · never mixed with book cards";
  renderChatMessage(
    "assistant",
    chatContextCardId
      ? "Ask about this card. I have its saved text and retrieved source excerpt as context."
      : "Enter a prompt below. I will answer locally and report generation speed. Use the four book modes when you need citations.",
  );
}

function pagesLabel(pages) {
  if (!pages || pages.length === 0) return "Page not recorded";
  return `${pages.length > 1 ? "Pages" : "Page"} ${pages.join(", ")}`;
}

function locatorLabel(item) {
  if (item.pages && item.pages.length) return pagesLabel(item.pages);
  if (item.locator) return item.locator.replace(/^.*\//, "Digital source · ");
  return "Source location recorded by corpus";
}

function renderCard(card, refreshHistory = true) {
  setMode(card.mode, true);
  activeCardId = card.card_id;
  activeCard = card;
  const copy = MODE_COPY[card.mode] || MODE_COPY.word;
  const cardView = $("#card-view");
  cardView.className = `card-view mode-${card.mode}`;
  text("#card-mode", copy.card);
  text("#card-model", `${card.model} · LOCAL`);
  const primaryTitle = card.mode === "answer" ? (card.english.term || card.title) : card.title;
  text("#card-title", primaryTitle);
  cardView.classList.toggle("long-title", primaryTitle.length > 80);
  optionalText("#card-subtitle", card.subtitle);
  text("#card-summary", card.summary_en);
  text("#origin-story", card.origin_story);
  text("#english-term", card.english.term || card.title);
  optionalText("#english-pronunciation", card.english.pronunciation);
  optionalText("#english-meaning", card.english.meaning);
  optionalText("#japanese-meaning", card.japanese.meaning);
  const showTraditional = card.chinese.traditional
    && card.chinese.traditional !== card.chinese.simplified
    && card.chinese.simplified.length < 20;
  text("#chinese-term", `${card.chinese.simplified}${showTraditional ? `／${card.chinese.traditional}` : ""}`);
  $("#chinese-term").title = card.chinese.traditional || "";
  optionalText("#chinese-pinyin", card.chinese.pinyin);
  optionalText("#chinese-meaning", card.chinese.meaning);
  text("#memory-hook", card.memory_hook);

  const ruby = $("#japanese-ruby");
  ruby.replaceChildren();
  const tokens = Array.isArray(card.japanese.ruby_tokens) ? card.japanese.ruby_tokens : [];
  if (tokens.length) {
    tokens.forEach((token) => {
      if (!token.r) {
        ruby.append(document.createTextNode(token.t || ""));
        return;
      }
      const node = document.createElement("ruby");
      node.append(document.createTextNode(token.t || ""), element("rt", "", token.r));
      ruby.append(node);
    });
  } else {
    const rubyNode = document.createElement("ruby");
    rubyNode.append(document.createTextNode(card.japanese.term || "—"));
    if (card.japanese.reading) rubyNode.append(element("rt", "", card.japanese.reading));
    ruby.append(rubyNode);
  }

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
  text(
    "#evidence-title",
    card.extensions?.source_title
      || card.evidence?.[0]?.source_title
      || SOURCE_TITLES[card.mode]
      || "Local library",
  );
  text("#narrative-label", copy.narrative);
  (card.evidence || []).slice(0, 1).forEach((item) => {
    const section = element("section", "evidence");
    section.append(element("h4", "", item.headword));
    section.append(element("span", "page", locatorLabel(item)));
    const excerpt = item.excerpt?.length > 320 ? `${item.excerpt.slice(0, 317)}…` : item.excerpt;
    section.append(element("blockquote", "", `“${excerpt}”`));
    if (item.section) section.append(element("span", "section", item.section));
    evidence.append(section);
  });
  show("card");
  updateCarouselChrome();
  if (refreshHistory) loadHistory();
}

function setMode(nextMode, preserveView = false) {
  const previousMode = mode;
  mode = MODE_COPY[nextMode] ? nextMode : "word";
  const copy = MODE_COPY[mode] || MODE_COPY.word;
  all(".mode").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  text("#query-label", copy.label);
  $("#query").placeholder = copy.placeholder;
  $("#query").maxLength = mode === "chat" ? 2000 : 240;
  $("#generate-button").textContent = mode === "chat" ? "Send" : "Create";
  text("#empty-kicker", copy.kicker);
  text("#empty-title", copy.title);
  text("#empty-description", copy.description);
  all(".examples button").forEach((button, index) => {
    button.dataset.query = copy.examples[index];
    button.dataset.mode = mode;
    button.textContent = copy.examples[index];
  });
  if (preserveView) return;
  if (mode === "chat") {
    show("chat");
  } else if (visibleView === "chat" || previousMode !== mode) {
    show("empty");
  }
}

async function submitChat(message) {
  const priorHistory = chatHistory.slice(-10);
  renderChatMessage("user", message);
  chatHistory.push({ role: "user", content: message });
  $("#query").value = "";
  $("#generate-button").disabled = true;
  const pending = renderChatMessage("assistant", "Generating locally…", null, true);
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history: priorHistory,
        card_id: chatContextCardId,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    pending.remove();
    chatHistory.push({ role: "assistant", content: payload.message });
    renderChatMessage("assistant", payload.message, { ...payload.metrics, saved: Boolean(payload.observation_id) });
  } catch (error) {
    pending.remove();
    renderChatMessage("assistant", `Chat failed: ${error.message}`);
  } finally {
    $("#generate-button").disabled = false;
  }
}

function discussCurrentCard() {
  if (!activeCardId || !activeCard) return;
  chatContextCardId = activeCardId;
  chatContextTitle = activeCard.title;
  setMode("chat");
  resetChat();
  $("#query").focus();
}

async function loadObservations() {
  try {
    const response = await fetch("/api/observations?limit=3");
    const observations = await response.json();
    if (!response.ok || !Array.isArray(observations) || !observations.length) return;
    observations.reverse().forEach((item) => {
      renderChatMessage("user", item.prompt);
      renderChatMessage("assistant", item.response, { ...item.metrics, saved: true });
    });
  } catch (_error) {
    // Model Lab remains usable even if the optional ledger cannot be read.
  }
}

async function submitQuery(query, requestedMode = mode) {
  query = String(query || "").trim();
  if (!query) return;
  setMode(requestedMode);
  if (mode === "chat") {
    await submitChat(query);
    return;
  }
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
    const bookItems = Object.values(health.card_books || {}).reduce((total, item) => total + (item.items || 0), 0);
    const sourceCount = (health.corpus.entries || 0) + bookItems;
    text("#state-label", ready ? `${sourceCount.toLocaleString()} sources · model ready` : "Model or corpus is starting…");
  } catch (_error) {
    text("#state-label", "Terminal unavailable");
  }
}

async function loadHistory() {
  const history = $("#history");
  try {
    const response = await fetch("/api/cards?limit=30");
    const cards = await response.json();
    carouselCards = cards;
    history.replaceChildren();
    if (!cards.length) {
      carouselIndex = -1;
      history.append(element("p", "quiet", "No cards yet."));
      updateCarouselChrome();
      return;
    }
    carouselIndex = Math.max(0, cards.findIndex((card) => card.card_id === activeCardId));
    cards.forEach((card, index) => {
      const button = element("button");
      button.title = `${card.title} · ${MODE_COPY[card.mode]?.card || card.mode}`;
      button.classList.toggle("active", index === carouselIndex);
      button.addEventListener("click", () => {
        carouselIndex = index;
        renderCard(card, false);
      });
      history.append(button);
    });
    if (!activeCardId && mode !== "chat") renderCard(cards[0], false);
    updateCarouselChrome();
    scheduleCarousel();
  } catch (_error) {
    history.replaceChildren(element("p", "quiet", "History unavailable."));
  }
}

function updateCarouselChrome() {
  const found = carouselCards.findIndex((card) => card.card_id === activeCardId);
  if (found >= 0) carouselIndex = found;
  text("#carousel-position", carouselCards.length ? `${carouselIndex + 1} / ${carouselCards.length}` : "0 / 0");
  all("#history button").forEach((button, index) => button.classList.toggle("active", index === carouselIndex));
  $("#toggle-autoplay").textContent = autoplayEnabled ? "Ⅱ" : "▶";
  $("#toggle-autoplay").title = autoplayEnabled ? "Pause carousel" : "Play carousel";
}

function navigateCards(step) {
  if (!carouselCards.length || visibleView === "loading") return;
  carouselIndex = (carouselIndex + step + carouselCards.length) % carouselCards.length;
  renderCard(carouselCards[carouselIndex], false);
}

function scheduleCarousel() {
  if (autoplayTimer) clearInterval(autoplayTimer);
  if (!autoplayEnabled) return;
  autoplayTimer = setInterval(() => {
    if (mode !== "chat" && carouselCards.length > 1) navigateCards(1);
  }, 30000);
}

async function toggleFullscreen() {
  try {
    if (!document.fullscreenElement) {
      document.body.classList.add("display-mode");
      await document.documentElement.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch (_error) {
    document.body.classList.toggle("display-mode");
  }
}

all(".mode").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.mode === "chat") {
    chatContextCardId = "";
    chatContextTitle = "";
    resetChat();
  }
  setMode(button.dataset.mode);
}));
all(".examples button").forEach((button) => button.addEventListener("click", () => submitQuery(button.dataset.query, button.dataset.mode || mode)));
$("#card-form").addEventListener("submit", (event) => { event.preventDefault(); submitQuery($("#query").value); });
$("#refresh-history").addEventListener("click", loadHistory);
$("#clear-chat").addEventListener("click", resetChat);
$("#discuss-card").addEventListener("click", discussCurrentCard);
$("#previous-card").addEventListener("click", () => navigateCards(-1));
$("#next-card").addEventListener("click", () => navigateCards(1));
$("#toggle-autoplay").addEventListener("click", () => {
  autoplayEnabled = !autoplayEnabled;
  updateCarouselChrome();
  scheduleCarousel();
});
$("#fullscreen-button").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && !new URLSearchParams(location.search).has("display")) {
    document.body.classList.remove("display-mode");
  }
});

const initialParameters = new URLSearchParams(location.search);
const initialMode = MODE_COPY[initialParameters.get("mode")] ? initialParameters.get("mode") : "word";
setMode(initialMode);
if (initialParameters.has("display")) document.body.classList.add("display-mode");
loadHealth();
loadHistory();
loadObservations();
setInterval(loadHealth, 30000);
