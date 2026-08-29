"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
let mode = "answer";
let activeCardId = null;
let activeCard = null;
let visibleView = "empty";
let chatHistory = [];
let chatContextCardId = "";
let chatContextTitle = "";
let chatThreadId = "";
let chatParentEventId = "";
let carouselCards = [];
let carouselIndex = -1;
let autoplayEnabled = true;
let autoplayTimer = null;
let alternateTimer = null;
let alternateIndex = 0;
let originCy = null;
let overviewCy = null;
let originGraphData = null;
let graphFocusAreas = [];
let graphFocusIndex = 0;
let graphNodeBadgeFrame = null;
let allSavedCards = [];
let sentenceSlides = [];
let sentenceSlideIndex = 0;
let sentenceSlideTimer = null;
let graphFocusTimer = null;
let graphViewportFitTimer = null;
let chromeTimer = null;
let ambientRouting = false;
let ambientModeIndex = 0;
let userActivityRevision = 0;
const ambientModeDecks = new Map();

const INNER_SLIDE_DWELL_MS = 18000;
const CARD_MIN_DWELL_MS = 30000;
const ACCEPTED_DECK_SYNC_MS = 30000;
const AMBIENT_MODE_ORDER = ["question", "answer", "knowledge", "word", "root", "affix"];

const ALTERNATE_LANGUAGES = {
  french: { label: "FRANÇAIS · PRONONCIATION", className: "french" },
  arabic: { label: "العربية · النطق", className: "arabic" },
};

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
    label: "Enter an English word",
    placeholder: "Try “wanderlust”",
    examples: ["wanderlust", "serendipity", "ephemeral"],
    kicker: "WORD CARD · FIVE LANGUAGES",
    title: "Learn one word clearly across languages.",
    description: "Large English and IPA lead into Japanese, Chinese, and a rotating French or Arabic equivalent.",
    narrative: "WORD NOTE",
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
  root: {
    card: "ROOT GRAPH",
    label: "Enter a word or root",
    placeholder: "Try “inspection”",
    examples: ["inspection", "spect", "transport"],
    kicker: "ROOT GRAPH · WORD FAMILIES",
    title: "See the roots inside a word and the families growing from them.",
    description: "The complete saved graph uses both root and affix books, then focuses one branch per slide.",
    narrative: "ROOT FOCUS",
  },
  affix: {
    card: "AFFIX GRAPH",
    label: "Enter a word or affix",
    placeholder: "Try “abnormal”",
    examples: ["abnormal", "preview", "happiness"],
    kicker: "AFFIX GRAPH · PREFIX + SUFFIX",
    title: "See how prefixes and suffixes reshape a word.",
    description: "One complete morphology graph, with a clean focus slide for every useful affix.",
    narrative: "AFFIX FOCUS",
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
  root: "New Oriental English Root Dictionary",
  affix: "English Affix Dictionary",
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
  noteActivity();
}

function noteActivity(userInitiated = false) {
  document.body.classList.remove("chrome-collapsed");
  window.clearTimeout(chromeTimer);
  if (userInitiated) {
    userActivityRevision += 1;
    if (ambientRouting && autoplayEnabled && visibleView === "card") scheduleCarousel();
  }
  if (visibleView !== "card" || document.body.classList.contains("display-mode")) return;
  chromeTimer = window.setTimeout(() => {
    if (!["INPUT", "BUTTON", "TEXTAREA"].includes(document.activeElement?.tagName)) {
      document.body.classList.add("chrome-collapsed");
    }
  }, 9000);
}

function text(selector, value) {
  $(selector).textContent = value || "—";
}

function optionalText(selector, value) {
  const node = $(selector);
  node.textContent = value || "";
  node.classList.toggle("hidden", !value);
}

function renderRubyElement(container, tokens, term, fallbackReading = "") {
  container.replaceChildren();
  if (Array.isArray(tokens) && tokens.length) {
    const appendToken = (target, token) => {
      const role = ["subject", "predicate", "object", "modifier", "connector", "clause", "other"]
        .includes(token.grammarRole) ? token.grammarRole : "";
      const tokenTarget = role ? element("span", `grammar-part role-${role}`) : target;
      if (!token.r) {
        tokenTarget.append(document.createTextNode(token.t || ""));
        if (tokenTarget !== target) target.append(tokenTarget);
        return;
      }
      const ruby = document.createElement("ruby");
      ruby.append(document.createTextNode(token.t || ""), element("rt", "", token.r));
      tokenTarget.append(ruby);
      if (tokenTarget !== target) target.append(tokenTarget);
    };
    for (let index = 0; index < tokens.length; index += 1) {
      const token = tokens[index];
      let counterIndex = index + 1;
      while (counterIndex < tokens.length && /^\d+$/.test(String(tokens[counterIndex]?.t || ""))) counterIndex += 1;
      if (/^\d+$/.test(String(token.t || "")) && /^[万億年月日人個本枚台歳％%]/.test(String(tokens[counterIndex]?.t || ""))) {
        const cluster = element("span", "ruby-cluster");
        while (index < counterIndex) {
          appendToken(cluster, tokens[index]);
          index += 1;
        }
        while (index < tokens.length && /^[万億年月日人個本枚台歳％%]+$/.test(String(tokens[index]?.t || ""))) {
          appendToken(cluster, tokens[index]);
          index += 1;
        }
        index -= 1;
        container.append(cluster);
      } else {
        appendToken(container, token);
      }
    }
    return;
  }
  const ruby = document.createElement("ruby");
  ruby.append(document.createTextNode(term || "—"));
  if (fallbackReading) ruby.append(element("rt", "", fallbackReading));
  container.append(ruby);
}

function renderRuby(selector, tokens, term, fallbackReading = "") {
  renderRubyElement($(selector), tokens, term, fallbackReading);
}

function splitText(text, maxCharacters) {
  const remainingParts = [];
  let remaining = String(text || "").trim();
  while (remaining.length > maxCharacters) {
    const floor = Math.floor(maxCharacters * 0.58);
    const windowText = remaining.slice(0, maxCharacters + 1);
    let boundary = -1;
    for (const marker of [". ", "? ", "! ", "; ", ", "]) {
      const candidate = windowText.lastIndexOf(marker);
      if (candidate >= floor) {
        boundary = candidate + 1;
        break;
      }
    }
    if (boundary < floor) {
      const wordBoundary = windowText.lastIndexOf(" ");
      boundary = wordBoundary >= floor ? wordBoundary : maxCharacters;
    }
    remainingParts.push(remaining.slice(0, boundary).trim());
    remaining = remaining.slice(boundary).trim();
  }
  if (remaining) remainingParts.push(remaining);
  return remainingParts.length ? remainingParts : ["—"];
}

function splitRubyTokens(tokens, maxCharacters) {
  if (!Array.isArray(tokens) || !tokens.length) return [];
  const chunks = [];
  let chunk = [];
  let length = 0;
  tokens.forEach((token) => {
    const tokenLength = String(token.t || "").length;
    if (chunk.length && length + tokenLength > maxCharacters) {
      const nextText = String(token.t || "");
      const carry = [];
      if (/^[万億年月日人個本枚台歳％%]/.test(nextText)) {
        while (chunk.length && /^\d+$/.test(String(chunk.at(-1)?.t || ""))) {
          const numericToken = chunk.pop();
          carry.unshift(numericToken);
          length -= String(numericToken.t || "").length;
        }
      }
      if (chunk.length) chunks.push(chunk);
      chunk = carry;
      length = carry.reduce((total, item) => total + String(item.t || "").length, 0);
    }
    chunk.push(token);
    length += tokenLength;
    if (/[。！？?!、，；;]$/.test(String(token.t || "")) && length >= maxCharacters * 0.55) {
      chunks.push(chunk);
      chunk = [];
      length = 0;
    }
  });
  if (chunk.length) chunks.push(chunk);
  return chunks;
}

function grammarAnalysis(card, language, sourceText) {
  const analysis = card.extensions?.grammar_analyses?.[language];
  const parts = Array.isArray(analysis?.parts) ? analysis.parts : [];
  if (!sourceText || !parts.length) return null;
  if (parts.map((part) => String(part.surface || "")).join("") !== sourceText) return null;
  const roles = new Set(["subject", "predicate", "object", "modifier", "connector", "clause", "other"]);
  if (parts.some((part) => !part.surface || !roles.has(part.role))) return null;
  return { ...analysis, parts };
}

function grammarChunks(parts, maxCharacters) {
  const chunks = [];
  let chunk = [];
  let length = 0;
  parts.forEach((part) => {
    const partLength = String(part.surface || "").length;
    if (chunk.length && length + partLength > maxCharacters) {
      chunks.push(chunk);
      chunk = [];
      length = 0;
    }
    chunk.push(part);
    length += partLength;
  });
  if (chunk.length) chunks.push(chunk);
  return chunks;
}

function annotateRubyGrammar(tokens, analysis, sourceText) {
  if (!analysis || !Array.isArray(tokens) || !tokens.length) return tokens;
  if (tokens.map((token) => String(token.t || "")).join("") !== sourceText) return tokens;
  const roles = [];
  analysis.parts.forEach((part) => {
    for (let index = 0; index < String(part.surface).length; index += 1) {
      roles.push(part.role);
    }
  });
  let cursor = 0;
  return tokens.map((token) => {
    const textValue = String(token.t || "");
    const tokenRoles = roles.slice(cursor, cursor + textValue.length)
      .filter((role, index) => !/\s/.test(textValue[index] || ""));
    cursor += textValue.length;
    const grammarRole = tokenRoles.sort((left, right) => (
      tokenRoles.filter((item) => item === right).length
      - tokenRoles.filter((item) => item === left).length
    ))[0];
    return grammarRole ? { ...token, grammarRole } : token;
  });
}

function buildSentenceSlides(card) {
  const slides = [];
  const englishText = card.english.term || card.title;
  const englishGrammar = grammarAnalysis(card, "en", englishText);
  if (englishGrammar) {
    grammarChunks(englishGrammar.parts, 165).forEach((grammarParts) => {
      slides.push({
        language: "english",
        label: "ENGLISH",
        term: grammarParts.map((part) => part.surface).join(""),
        tokens: [],
        grammarParts,
      });
    });
  } else {
    splitText(englishText, 165).forEach((term) => {
      slides.push({ language: "english", label: "ENGLISH", term, tokens: [] });
    });
  }
  const japaneseTokens = Array.isArray(card.japanese.ruby_tokens) ? card.japanese.ruby_tokens : [];
  const japaneseGrammar = grammarAnalysis(card, "ja", card.japanese.term);
  const japaneseChunks = splitRubyTokens(
    annotateRubyGrammar(japaneseTokens, japaneseGrammar, card.japanese.term),
    64,
  );
  if (japaneseChunks.length) {
    japaneseChunks.forEach((tokens) => slides.push({ language: "japanese", label: "日本語 · FURIGANA", term: "", tokens }));
  } else {
    splitText(card.japanese.term, 64).forEach((term) => {
      slides.push({ language: "japanese", label: "日本語 · FURIGANA", term, tokens: [], reading: card.japanese.reading });
    });
  }
  const chineseTokens = Array.isArray(card.chinese.ruby_tokens) ? card.chinese.ruby_tokens : [];
  const chineseGrammar = grammarAnalysis(card, "zh", card.chinese.simplified);
  const chineseChunks = splitRubyTokens(
    annotateRubyGrammar(chineseTokens, chineseGrammar, card.chinese.simplified),
    48,
  );
  if (chineseChunks.length) {
    chineseChunks.forEach((tokens) => slides.push({ language: "chinese", label: "中文 · PINYIN", term: "", tokens }));
  } else {
    splitText(card.chinese.simplified, 48).forEach((term) => {
      slides.push({ language: "chinese", label: "中文 · PINYIN", term, tokens: [], reading: card.chinese.pinyin });
    });
  }
  const investigationTerms = Array.isArray(card.extensions?.investigation_terms)
    ? card.extensions.investigation_terms.slice(0, 3)
    : [];
  if (investigationTerms.length) {
    slides.push({
      language: "investigation",
      label: "EXPLORE · WORD CARD",
      terms: investigationTerms,
      sourceCardId: card.card_id,
    });
  }
  return slides;
}

function fitSentenceSlide() {
  const stage = $("#sentence-stage");
  stage.style.removeProperty("--sentence-size");
  let size = Number.parseFloat(getComputedStyle(stage).fontSize);
  while (stage.scrollHeight > stage.clientHeight && size > 24) {
    size -= 2;
    stage.style.setProperty("--sentence-size", `${size}px`);
  }
}

function restartTransition(node, className) {
  node.classList.remove(className);
  void node.offsetWidth;
  node.classList.add(className);
}

function showSentenceSlide(requestedIndex) {
  if (!sentenceSlides.length) return;
  sentenceSlideIndex = (requestedIndex + sentenceSlides.length) % sentenceSlides.length;
  const slide = sentenceSlides[sentenceSlideIndex];
  const stage = $("#sentence-stage");
  stage.className = `sentence-stage language-${slide.language}`;
  text("#sentence-language", slide.label);
  if (slide.language === "investigation") {
    const panel = element("div", "investigation-slide");
    panel.append(element("span", "investigation-kicker", "CHOOSE ONE WORD TO CONTINUE"));
    slide.terms.forEach((item) => {
      const button = element("button", "investigation-term");
      button.type = "button";
      button.append(
        element("strong", "", item.term),
        element("span", "", item.note || "Open its multilingual Word Card"),
      );
      button.addEventListener("click", () => submitQuery(
        item.term,
        "knowledge",
        { source_card_id: slide.sourceCardId },
      ));
      panel.append(button);
    });
    stage.replaceChildren(panel);
    text("#sentence-position", `${sentenceSlideIndex + 1} / ${sentenceSlides.length}`);
    all("#sentence-dots button").forEach((button, index) => button.classList.toggle("active", index === sentenceSlideIndex));
    restartTransition(stage, "inner-slide-enter");
    return;
  }
  const content = element("p", "sentence-text");
  stage.replaceChildren(content);
  if (slide.language === "english") {
    if (Array.isArray(slide.grammarParts) && slide.grammarParts.length) {
      slide.grammarParts.forEach((part) => {
        content.append(element("span", `grammar-part role-${part.role}`, part.surface));
      });
    } else {
      content.textContent = slide.term;
    }
  } else {
    renderRubyElement(content, slide.tokens, slide.term, slide.reading || "");
  }
  text("#sentence-position", `${sentenceSlideIndex + 1} / ${sentenceSlides.length}`);
  all("#sentence-dots button").forEach((button, index) => button.classList.toggle("active", index === sentenceSlideIndex));
  restartTransition(stage, "inner-slide-enter");
  window.requestAnimationFrame(fitSentenceSlide);
}

function renderSentenceCarousel(card) {
  window.clearInterval(sentenceSlideTimer);
  const carousel = $("#sentence-carousel");
  const enabled = ["answer", "question"].includes(card.mode);
  carousel.classList.toggle("hidden", !enabled);
  sentenceSlides = enabled ? buildSentenceSlides(card) : [];
  sentenceSlideIndex = 0;
  if (!enabled) return;
  text("#sentence-kind", MODE_COPY[card.mode].card);
  const dots = $("#sentence-dots");
  dots.replaceChildren(...sentenceSlides.map((_slide, index) => {
    const button = element("button");
    button.type = "button";
    button.title = `Slide ${index + 1}`;
    button.addEventListener("click", () => {
      window.clearInterval(sentenceSlideTimer);
      showSentenceSlide(index);
      scheduleCarousel();
    });
    return button;
  }));
  showSentenceSlide(0);
  if (sentenceSlides.length > 1) {
    sentenceSlideTimer = window.setInterval(
      () => {
        if (sentenceSlideIndex >= sentenceSlides.length - 1) {
          window.clearInterval(sentenceSlideTimer);
          return;
        }
        showSentenceSlide(sentenceSlideIndex + 1);
      },
      INNER_SLIDE_DWELL_MS,
    );
  }
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

function renderLabStarters() {
  let starters = $("#lab-starters");
  if (!starters) {
    starters = element("div", "lab-starters");
    starters.id = "lab-starters";
    $("#chat-messages").append(starters);
  }
  const prompts = chatContextCardId
    ? [
        "Explain the core idea more simply.",
        "Compare its Japanese and Chinese wording.",
        "Give me one stronger memory hook.",
      ]
    : [
        "Explain RAG in two vivid sentences.",
        "用中文解释为什么词源有趣。",
        "日本語で「記憶」を短く説明して。",
      ];
  starters.replaceChildren(...prompts.map((prompt, index) => {
    const button = element("button", `starter starter-${index + 1}`);
    button.append(element("span", "", `0${index + 1}`), element("strong", "", prompt));
    button.addEventListener("click", () => submitQuery(prompt, "chat"));
    return button;
  }));
}

function resetChat() {
  chatHistory = [];
  chatThreadId = "";
  chatParentEventId = "";
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
  renderLabStarters();
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

function renderLegacyOriginGraph(card) {
  const graph = $("#origin-graph");
  const canvas = $("#origin-canvas");
  if (originCy) {
    originCy.destroy();
    originCy = null;
  }
  canvas.replaceChildren();
  const nodes = Array.isArray(card.origin_graph) ? card.origin_graph.slice(0, 7) : [];
  graph.classList.toggle("hidden", card.mode !== "word" || nodes.length < 2);
  if (card.mode !== "word" || nodes.length < 2) return;

  const normalized = nodes.map((item, index) => ({
    ...item,
    id: item.id || `origin-${index}`,
    parent: Object.hasOwn(item, "parent")
      ? item.parent
      : (index < nodes.length - 1 ? (nodes[index + 1].id || `origin-${index + 1}`) : ""),
  }));
  const ids = new Set(normalized.map((item) => item.id));
  const root = normalized.find((item) => !item.parent || !ids.has(item.parent)) || normalized.at(-1);
  normalized.forEach((item) => {
    if (item !== root && (!item.parent || !ids.has(item.parent))) item.parent = root.id;
  });

  if (typeof window.cytoscape !== "function") {
    canvas.append(element("p", "graph-error", "Graph renderer unavailable."));
    return;
  }
  const graphNodes = normalized.map((item) => ({
    data: {
      id: item.id,
      label: [
        item.form || "—",
        item.id === root.id
          ? [card.japanese?.term, card.chinese?.simplified].filter(Boolean).join(" · ")
          : "",
        item.meaning,
      ].filter(Boolean).join("\n"),
      basis: item.basis === "book" ? "book" : "model",
      root: item.id === root.id ? "yes" : "no",
    },
    classes: `${item.basis === "book" ? "book" : "model"}${item.id === root.id ? " root" : ""}`,
  }));
  const graphEdges = normalized
    .filter((item) => item.parent && ids.has(item.parent))
    .map((item, index) => ({
      data: {
        id: `edge-${index}-${item.parent}-${item.id}`,
        source: item.id,
        target: item.parent,
        stage: item.stage || "earlier form",
      },
    }));
  originCy = window.cytoscape({
    container: canvas,
    elements: [...graphNodes, ...graphEdges],
    minZoom: 0.65,
    maxZoom: 1.8,
    wheelSensitivity: 0.18,
    boxSelectionEnabled: false,
    autoungrabify: true,
    style: [
      {
        selector: "node",
        style: {
          width: 148,
          height: 72,
          shape: "round-rectangle",
          "background-color": "#eaf2ff",
          "border-width": 3,
          "border-color": "#1769ff",
          label: "data(label)",
          color: "#17213d",
          "font-family": "Inter, Segoe UI, Noto Sans CJK SC, sans-serif",
          "font-size": 15,
          "font-weight": 700,
          "text-wrap": "wrap",
          "text-max-width": 128,
          "text-valign": "center",
          "text-halign": "center",
        },
      },
      { selector: "node.book", style: { "background-color": "#fff0e8", "border-color": "#ff5d45" } },
      {
        selector: "node.root",
        style: {
          width: 176,
          height: 104,
          shape: "ellipse",
          "background-color": "#ffcf3d",
          "border-color": "#17213d",
          "border-width": 4,
          "font-size": 17,
          "text-max-width": 154,
        },
      },
      {
        selector: "edge",
        style: {
          width: 3,
          "curve-style": "bezier",
          "line-color": "#6e7d9c",
          "target-arrow-color": "#6e7d9c",
          "target-arrow-shape": "triangle",
          label: "data(stage)",
          color: "#13725f",
          "font-size": 10,
          "font-weight": 800,
          "text-background-color": "#ffffff",
          "text-background-opacity": 0.9,
          "text-background-padding": 3,
          "text-rotation": "autorotate",
        },
      },
    ],
  });
  originCy.layout({
    name: "breadthfirst",
    roots: originCy.$id(root.id),
    directed: false,
    circle: false,
    spacingFactor: 1.18,
    padding: 24,
    animate: false,
    transform: (_node, position) => ({ x: -position.y, y: position.x }),
  }).run();
  originCy.fit(originCy.elements(), 22);
}

function unifiedGraph(card) {
  const rich = card.extensions?.morphology_graph;
  if (Array.isArray(rich?.nodes) && rich.nodes.length > 1) return rich;
  const legacy = Array.isArray(card.origin_graph) ? card.origin_graph : [];
  if (legacy.length < 2) return null;
  const ids = new Set(legacy.map((node, index) => node.id || `origin-${index}`));
  const nodes = legacy.map((node, index) => ({
    id: node.id || `origin-${index}`,
    type: !node.parent || !ids.has(node.parent) ? "word" : "historical",
    form: node.form,
    meaning: node.meaning,
    language: node.stage,
    basis: node.basis,
  }));
  const center = nodes.find((node) => node.type === "word") || nodes[0];
  return {
    center_id: center.id,
    nodes,
    edges: legacy
      .filter((node) => node.parent && ids.has(node.parent))
      .map((node, index) => ({
        id: `legacy-edge-${index}`,
        source: node.id,
        target: node.parent,
        relationship: "developed-into",
      })),
    focus_areas: [{
      id: "overview",
      label: "Whole history",
      kind: "overview",
      node_ids: nodes.map((node) => node.id),
      headline: card.title,
      explanation: card.summary_en,
    }],
  };
}

function graphStyles(compact = false) {
  if (compact) return [
    { selector: "node", style: { width: 15, height: 15, label: "", "background-color": "#1769ff", "border-width": 1, "border-color": "#ffffff" } },
    { selector: "node.book", style: { "background-color": "#ff5d45" } },
    { selector: "node.center", style: { width: 23, height: 23, "background-color": "#ffcf3d", "border-color": "#17213d", "border-width": 2 } },
    { selector: "node.focus-node", style: { width: 23, height: 23, "border-color": "#17213d", "border-width": 3 } },
    { selector: "edge", style: { width: 1.5, "curve-style": "bezier", "line-color": "#9aa7bf", "target-arrow-color": "#9aa7bf", "target-arrow-shape": "triangle", "arrow-scale": .55 } },
  ];
  return [
    {
      selector: "node",
      style: {
        width: "data(nodeWidth)",
        height: "data(nodeHeight)",
        shape: "round-rectangle",
        "background-color": "#eaf2ff",
        "border-width": 3,
        "border-color": "#1769ff",
        label: "",
        color: "#17213d",
        "font-family": "Inter, Segoe UI, Noto Sans CJK SC, sans-serif",
        "font-size": "data(fontSize)",
        "font-weight": 700,
        "text-wrap": "wrap",
        "text-overflow-wrap": "anywhere",
        "text-max-width": "data(labelWidth)",
        "text-valign": "center",
        "text-halign": "center",
        "transition-property": "opacity, border-width, width, height",
        "transition-duration": "250ms",
      },
    },
    { selector: "node.book", style: { "background-color": "#fff0e8", "border-color": "#ff5d45" } },
    { selector: "node.prefix", style: { "background-color": "#e5faf4", "border-color": "#00a98f" } },
    { selector: "node.root", style: { "background-color": "#f2eaff", "border-color": "#8b3dff" } },
    { selector: "node.suffix", style: { "background-color": "#fff7d6", "border-color": "#e29a00" } },
    { selector: "node.center", style: { "background-color": "#ffcf3d", "border-color": "#17213d", "border-width": 4 } },
    { selector: ".dimmed", style: { display: "none" } },
    { selector: "node.focus-node", style: { "border-width": 5 } },
    {
      selector: "edge",
      style: {
        width: 3,
        "curve-style": "bezier",
        "line-color": "#71809e",
        "target-arrow-color": "#71809e",
        "target-arrow-shape": "triangle",
      },
    },
  ];
}

function graphLanguageCode(value) {
  const language = String(value || "").trim();
  if (!language) return "";
  const normalized = language.toLocaleLowerCase();
  const aliases = [
    [/proto-indo-european|\bpie\b/, "PIE"],
    [/classical latin|late latin|medieval latin|\blatin\b/, "LA"],
    [/ancient greek|classical greek|\bgreek\b/, "EL"],
    [/old french|middle french|\bfrench\b/, "FR"],
    [/old english|middle english|modern english|\benglish\b/, "EN"],
    [/germanic/, "GM"],
    [/arabic/, "AR"],
    [/chinese/, "ZH"],
    [/japanese/, "JA"],
  ];
  return aliases.find(([pattern]) => pattern.test(normalized))?.[1]
    || language.slice(0, 4).toLocaleUpperCase();
}

function graphTextWidth(value, fontSize, fontWeight = 650) {
  const canvas = graphTextWidth.canvas || (graphTextWidth.canvas = document.createElement("canvas"));
  const context = canvas.getContext("2d");
  context.font = `${fontWeight} ${fontSize}px Inter, Segoe UI, Noto Sans CJK SC, sans-serif`;
  return context.measureText(String(value || "")).width;
}

function graphTextLines(value, maxWidth, fontSize, fontWeight = 650) {
  const textValue = String(value || "").trim();
  if (!textValue) return 0;
  const graphemes = typeof Intl.Segmenter === "function"
    ? [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(textValue)].map((item) => item.segment)
    : Array.from(textValue);
  let lines = 1;
  let line = "";
  graphemes.forEach((grapheme) => {
    if (grapheme === "\n") {
      lines += 1;
      line = "";
      return;
    }
    const candidate = `${line}${grapheme}`;
    if (line && graphTextWidth(candidate, fontSize, fontWeight) > maxWidth) {
      lines += 1;
      line = grapheme.trimStart();
    } else {
      line = candidate;
    }
  });
  return lines;
}

function graphNodeMetrics(node, isCenter) {
  const term = String(node.form || "—");
  const meaning = String(node.meaning || "");
  const termFontSize = isCenter ? 22 : 18;
  const meaningFontSize = isCenter ? 15 : 13.5;
  const minimumWidth = isCenter ? 238 : 184;
  const maximumWidth = isCenter ? 380 : 320;
  const contentLength = [...`${term} ${meaning}`].length;
  const naturalTermWidth = graphTextWidth(term, termFontSize, 950) + (isCenter ? 52 : 28);
  const densityWidth = minimumWidth + Math.sqrt(Math.max(0, contentLength - 28)) * 10;
  const nodeWidth = Math.round(Math.max(
    minimumWidth,
    Math.min(maximumWidth, Math.max(naturalTermWidth, densityWidth)),
  ));
  const contentWidth = nodeWidth - (isCenter ? 42 : 24);
  const termLines = graphTextLines(term, contentWidth, termFontSize, 950);
  const meaningLines = graphTextLines(meaning, contentWidth, meaningFontSize, 650);
  const copyHeight = termLines * termFontSize * 1.08
    + (meaningLines ? 7 + meaningLines * meaningFontSize * 1.22 : 0);
  const nodeHeight = Math.ceil(Math.max(isCenter ? 138 : 106, 37 + copyHeight + 14));
  return {
    nodeWidth,
    nodeHeight,
    termFontSize,
    meaningFontSize,
  };
}

function clearGraphNodeBadges() {
  if (graphNodeBadgeFrame !== null) window.cancelAnimationFrame(graphNodeBadgeFrame);
  graphNodeBadgeFrame = null;
  $("#graph-node-badges").replaceChildren();
}

function updateGraphNodeBadges() {
  graphNodeBadgeFrame = null;
  const layer = $("#graph-node-badges");
  const graph = $("#origin-graph");
  const canvas = $("#origin-canvas");
  if (!originCy || graph.classList.contains("hidden")) {
    layer.replaceChildren();
    return;
  }
  const graphRect = graph.getBoundingClientRect();
  const canvasRect = canvas.getBoundingClientRect();
  const offsetX = canvasRect.left - graphRect.left;
  const offsetY = canvasRect.top - graphRect.top;
  const badges = [];
  originCy.nodes().forEach((node) => {
    if (node.hasClass("dimmed") || node.style("display") === "none") return;
    const position = node.renderedPosition();
    const width = node.renderedWidth();
    const height = node.renderedHeight();
    const type = String(node.data("type") || "related");
    const language = graphLanguageCode(node.data("language"));
    const safeLanguage = language.toLocaleLowerCase().replace(/[^a-z0-9-]/g, "");
    const box = element(
      "span",
      `graph-node-badge-box type-${type}${safeLanguage ? ` lang-${safeLanguage}` : ""}${node.hasClass("center") ? " center" : ""}`,
    );
    box.dataset.nodeId = node.id();
    box.style.left = `${offsetX + position.x - width / 2}px`;
    box.style.top = `${offsetY + position.y - height / 2}px`;
    box.style.width = `${width}px`;
    box.style.height = `${height}px`;
    const scale = width / node.data("nodeWidth");
    box.style.setProperty("--graph-node-scale", scale);
    box.style.setProperty("--graph-term-size", `${node.data("termFontSize") * scale}px`);
    box.style.setProperty("--graph-meaning-size", `${node.data("meaningFontSize") * scale}px`);
    box.style.setProperty("--graph-copy-top", `${32 * scale}px`);
    box.style.setProperty("--graph-copy-side", `${10 * scale}px`);
    box.style.setProperty("--graph-copy-bottom", `${10 * scale}px`);
    box.style.setProperty("--graph-copy-gap", `${7 * scale}px`);
    box.style.setProperty("--graph-badge-top", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-side", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-space", `${9 * scale}px`);
    box.style.setProperty("--graph-badge-radius", `${8 * scale}px`);
    box.style.setProperty("--graph-badge-padding-y", `${3 * scale}px`);
    box.style.setProperty("--graph-badge-padding-x", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-size", `${10 * scale}px`);
    box.append(element("i", "graph-node-type", type.toLocaleUpperCase()));
    if (language) box.append(element("i", "graph-node-language", language));
    const copy = element("span", "graph-node-copy");
    const term = element("strong", "graph-node-term", node.data("form") || "—");
    term.dir = "auto";
    copy.append(term);
    if (node.data("meaning")) {
      const meaning = element("span", "graph-node-meaning", node.data("meaning"));
      meaning.dir = "auto";
      copy.append(meaning);
    }
    box.append(copy);
    badges.push(box);
  });
  layer.replaceChildren(...badges);
}

function scheduleGraphNodeBadges() {
  if (graphNodeBadgeFrame !== null) return;
  graphNodeBadgeFrame = window.requestAnimationFrame(updateGraphNodeBadges);
}

function appendArabicLetters(container, value) {
  const textValue = String(value || "");
  const letters = typeof Intl.Segmenter === "function"
    ? [...new Intl.Segmenter("ar", { granularity: "grapheme" }).segment(textValue)].map((item) => item.segment)
    : Array.from(textValue);
  let colorIndex = 0;
  letters.forEach((letter) => {
    if (/^\s+$/.test(letter)) {
      container.append(document.createTextNode(letter));
      return;
    }
    container.append(element("span", `arabic-letter tone-${colorIndex % 6}`, letter));
    colorIndex += 1;
  });
}

function renderGraphFocusAnnotations(focus) {
  const container = $("#graph-focus-annotations");
  const explicit = focus?.annotations && typeof focus.annotations === "object"
    ? focus.annotations
    : null;
  const values = [
    ["JA", explicit?.ja?.term || activeCard?.japanese?.term, explicit?.ja?.meaning || activeCard?.japanese?.meaning],
    ["ZH", explicit?.zh?.term || activeCard?.chinese?.simplified, explicit?.zh?.meaning || activeCard?.chinese?.meaning],
    ["FR", explicit?.fr?.term || activeCard?.extra_languages?.french?.term, explicit?.fr?.meaning || activeCard?.extra_languages?.french?.meaning],
    ["AR", explicit?.ar?.term || activeCard?.extra_languages?.arabic?.term, explicit?.ar?.meaning || activeCard?.extra_languages?.arabic?.meaning],
  ].filter(([, term]) => term);
  container.replaceChildren(...values.map(([language, value, meaning]) => {
    const item = element("span", `graph-focus-annotation language-${language.toLocaleLowerCase()}`);
    item.append(element("small", "", language));
    const term = element("b", "", value);
    term.dir = "auto";
    if (language === "AR") {
      term.replaceChildren();
      term.dir = "rtl";
      appendArabicLetters(term, value);
    }
    item.append(term);
    if (meaning) {
      const explanation = element("em", "", meaning);
      explanation.dir = "auto";
      item.append(explanation);
    }
    return item;
  }));
  container.classList.toggle("hidden", values.length === 0);
}

function visibleGraphElements() {
  if (!originCy) return null;
  const nodes = originCy.nodes().filter((node) => !node.hasClass("dimmed"));
  const ids = new Set(nodes.map((node) => node.id()));
  const edges = originCy.edges().filter((edge) => (
    !edge.hasClass("dimmed") && ids.has(edge.source().id()) && ids.has(edge.target().id())
  ));
  return nodes.union(edges);
}

function fitGraphView({ wholeGraph = false, animate = false } = {}) {
  if (!originCy || $("#origin-graph").classList.contains("hidden")) return;
  originCy.resize();
  const target = wholeGraph ? originCy.elements() : visibleGraphElements();
  if (!target?.length) return;
  const canvas = $("#origin-canvas");
  const shortestSide = Math.min(canvas.clientWidth, canvas.clientHeight);
  const padding = Math.max(24, Math.min(72, Math.round(shortestSide * (wholeGraph ? .055 : .09))));
  originCy.stop();
  if (animate) {
    originCy.animate({ fit: { eles: target, padding }, duration: 350 });
  } else {
    originCy.fit(target, padding);
  }
  overviewCy?.resize();
  overviewCy?.fit(overviewCy.elements(), 9);
  scheduleGraphNodeBadges();
}

function scheduleGraphViewportFit(delay = 90) {
  window.clearTimeout(graphViewportFitTimer);
  graphViewportFitTimer = window.setTimeout(() => {
    layoutGraphForCanvas();
    fitGraphView();
  }, delay);
}

function resetGraphAutofit() {
  if (!originCy || !graphFocusAreas.length) return;
  layoutGraphForCanvas();
  const overviewIndex = Math.max(0, graphFocusAreas.findIndex((area) => area.kind === "overview"));
  showGraphFocus(overviewIndex, false);
  fitGraphView({ wholeGraph: true, animate: true });
}

function showGraphFocus(requestedIndex, animate = true) {
  if (!originCy || !graphFocusAreas.length) return;
  graphFocusIndex = (requestedIndex + graphFocusAreas.length) % graphFocusAreas.length;
  const focus = graphFocusAreas[graphFocusIndex];
  const ids = new Set(focus.node_ids || []);
  originCy.elements().removeClass("dimmed focus-node");
  overviewCy?.nodes().removeClass("focus-node");
  const focusNodes = originCy.nodes().filter((node) => ids.has(node.id()));
  const focusEdges = originCy.edges().filter((edge) => (
    ids.has(edge.source().id()) && ids.has(edge.target().id())
  ));
  if (focus.kind !== "overview") {
    originCy.nodes().not(focusNodes).addClass("dimmed");
    originCy.edges().not(focusEdges).addClass("dimmed");
    focusNodes.addClass("focus-node");
    overviewCy?.nodes().filter((node) => ids.has(node.id())).addClass("focus-node");
  }
  fitGraphView({ wholeGraph: focus.kind === "overview", animate });
  text("#graph-focus-headline", focus.headline || focus.label);
  optionalText("#graph-focus-explanation", focus.explanation);
  renderGraphFocusAnnotations(focus);
  text("#graph-focus-position", `${graphFocusIndex + 1} / ${graphFocusAreas.length}`);
  all("#graph-focus-dots button").forEach((button, index) => {
    button.classList.toggle("active", index === graphFocusIndex);
  });
  scheduleGraphNodeBadges();
}

function semanticGraphPositions(data, canvasWidth = 1280) {
  const positions = new Map([[data.center_id, { x: 0, y: 0 }]]);
  const components = data.nodes.filter((node) => ["prefix", "root", "suffix"].includes(node.type));
  const histories = data.nodes.filter((node) => node.type === "historical");
  const related = data.nodes.filter((node) => (
    node.id !== data.center_id
    && !["prefix", "root", "suffix", "historical"].includes(node.type)
  ));
  const perRow = Math.max(3, Math.min(4, Math.floor(canvasWidth / 300)));
  const gapX = Math.max(260, Math.min(440, canvasWidth * .32));
  const rowGap = 125;
  const placeRows = (nodes, firstY, direction) => {
    for (let start = 0, row = 0; start < nodes.length; start += perRow, row += 1) {
      const group = nodes.slice(start, start + perRow);
      group.forEach((node, column) => {
        positions.set(node.id, {
          x: (column - (group.length - 1) / 2) * gapX,
          y: firstY + direction * row * rowGap,
        });
      });
    }
  };
  placeRows(components, -140, -1);
  const componentRows = components.length ? Math.ceil(components.length / perRow) : 0;
  placeRows(histories, componentRows ? -265 - (componentRows - 1) * rowGap : -140, -1);
  placeRows(related, 140, 1);
  data.nodes.forEach((node, index) => {
    if (!positions.has(node.id)) positions.set(node.id, { x: index * 170, y: 145 });
  });
  return positions;
}

function layoutGraphForCanvas() {
  if (!originCy || !originGraphData) return;
  const positions = semanticGraphPositions(originGraphData, $("#origin-canvas").clientWidth);
  originCy.nodes().positions((node) => positions.get(node.id()));
  repelGraphNodes(originCy, originGraphData.center_id);
  if (overviewCy) {
    overviewCy.nodes().positions((node) => originCy.$id(node.id()).position());
    overviewCy.fit(overviewCy.elements(), 9);
  }
}

function repelGraphNodes(cy, centerId, iterations = 160) {
  const nodes = cy.nodes().toArray();
  for (let iteration = 0; iteration < iterations; iteration += 1) {
    const offsets = new Map(nodes.map((node) => [node.id(), { x: 0, y: 0 }]));
    let collisionCount = 0;
    for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
        const left = nodes[leftIndex];
        const right = nodes[rightIndex];
        const leftPosition = left.position();
        const rightPosition = right.position();
        const dx = rightPosition.x - leftPosition.x;
        const dy = rightPosition.y - leftPosition.y;
        const requiredX = (left.data("nodeWidth") + right.data("nodeWidth")) / 2 + 42;
        const requiredY = (left.data("nodeHeight") + right.data("nodeHeight")) / 2 + 18;
        const overlapX = requiredX - Math.abs(dx);
        const overlapY = requiredY - Math.abs(dy);
        if (overlapX <= 0 || overlapY <= 0) continue;
        collisionCount += 1;
        const moveAlongX = overlapX / requiredX < overlapY / requiredY;
        const axis = moveAlongX ? "x" : "y";
        const delta = (moveAlongX ? overlapX : overlapY) / 2 + .6;
        const direction = (moveAlongX ? dx : dy) === 0
          ? (left.id().localeCompare(right.id()) < 0 ? 1 : -1)
          : Math.sign(moveAlongX ? dx : dy);
        const leftShare = left.id() === centerId ? 0 : (right.id() === centerId ? 2 : 1);
        const rightShare = right.id() === centerId ? 0 : (left.id() === centerId ? 2 : 1);
        offsets.get(left.id())[axis] -= direction * delta * leftShare;
        offsets.get(right.id())[axis] += direction * delta * rightShare;
      }
    }
    nodes.forEach((node) => {
      if (node.id() === centerId) return;
      const offset = offsets.get(node.id());
      node.position({ x: node.position("x") + offset.x, y: node.position("y") + offset.y });
    });
    if (!collisionCount) break;
  }
}

function renderOriginGraph(card) {
  const graph = $("#origin-graph");
  const canvas = $("#origin-canvas");
  const overview = $("#graph-overview");
  originCy?.destroy();
  overviewCy?.destroy();
  window.clearInterval(graphFocusTimer);
  originCy = null;
  overviewCy = null;
  originGraphData = null;
  clearGraphNodeBadges();
  canvas.replaceChildren();
  overview.replaceChildren();
  const data = unifiedGraph(card);
  const enabled = ["word", "root", "affix"].includes(card.mode)
    && Array.isArray(data?.nodes) && data.nodes.length > 1;
  graph.classList.toggle("hidden", !enabled);
  if (!enabled) return;
  originGraphData = data;
  text("#graph-kind", card.mode === "word" ? "WORD ORIGIN GRAPH" : `${card.mode.toUpperCase()} GRAPH`);
  if (typeof window.cytoscape !== "function") {
    canvas.append(element("p", "graph-error", "Graph renderer unavailable."));
    return;
  }
  const semanticPositions = semanticGraphPositions(data, canvas.clientWidth);
  const graphNodes = data.nodes.map((node) => {
    const isCenter = node.id === data.center_id;
    const metrics = graphNodeMetrics(node, isCenter);
    return {
      data: {
        id: node.id,
        label: [node.form || "—", node.meaning].filter(Boolean).join("\n"),
        form: node.form || "—",
        meaning: node.meaning || "",
        type: node.type || "related",
        language: node.language || "",
        ...metrics,
      },
      classes: [
        node.basis === "book" ? "book" : "model",
        node.type || "related",
        isCenter ? "center" : "",
      ].filter(Boolean).join(" "),
      position: semanticPositions.get(node.id),
    };
  });
  const ids = new Set(data.nodes.map((node) => node.id));
  const graphEdges = (data.edges || [])
    .filter((edge) => ids.has(edge.source) && ids.has(edge.target))
    .map((edge, index) => ({
      data: {
        id: edge.id || `edge-${index}`,
        source: edge.source,
        target: edge.target,
        relationship: edge.relationship || "related",
      },
    }));
  originCy = window.cytoscape({
    container: canvas,
    elements: [...graphNodes, ...graphEdges],
    minZoom: .08,
    maxZoom: 2.2,
    wheelSensitivity: .16,
    boxSelectionEnabled: false,
    autoungrabify: true,
    layout: { name: "preset", fit: true, padding: 24 },
    style: graphStyles(),
  });
  repelGraphNodes(originCy, data.center_id);
  fitGraphView({ wholeGraph: true });
  originCy.on("render", scheduleGraphNodeBadges);
  const overviewNodes = graphNodes.map((item) => ({
    ...item,
    position: originCy.$id(item.data.id).position(),
  }));
  overviewCy = window.cytoscape({
    container: overview,
    elements: [...overviewNodes, ...graphEdges],
    layout: { name: "preset", fit: true, padding: 9 },
    userZoomingEnabled: false,
    userPanningEnabled: false,
    autoungrabify: true,
    style: graphStyles(true),
  });
  const allIds = data.nodes.map((node) => node.id);
  graphFocusAreas = Array.isArray(data.focus_areas) && data.focus_areas.length
    ? data.focus_areas.map((area, index) => ({
      ...area,
      id: area.id || `focus-${index}`,
      node_ids: (area.node_ids || []).filter((id) => ids.has(id)),
    }))
    : [{ id: "overview", label: "Overview", kind: "overview", node_ids: allIds, headline: card.title, explanation: card.summary_en }];
  if (!graphFocusAreas.some((area) => area.kind === "overview")) {
    graphFocusAreas.unshift({
      id: "overview",
      label: "Overview",
      kind: "overview",
      node_ids: allIds,
      headline: card.title,
      explanation: card.summary_en,
    });
  }
  const dots = $("#graph-focus-dots");
  dots.replaceChildren(...graphFocusAreas.map((area, index) => {
    const button = element("button");
    button.type = "button";
    button.title = area.label || `Area ${index + 1}`;
    button.addEventListener("click", () => {
      window.clearInterval(graphFocusTimer);
      showGraphFocus(index);
      scheduleCarousel();
    });
    return button;
  }));
  $("#previous-graph-focus").disabled = graphFocusAreas.length < 2;
  $("#next-graph-focus").disabled = graphFocusAreas.length < 2;
  $("#graph-focus-dots").classList.toggle("hidden", graphFocusAreas.length < 2);
  originCy.on("tap", "node", (event) => {
    const index = graphFocusAreas.findIndex((area) => (
      area.kind !== "overview" && area.node_ids?.includes(event.target.id())
    ));
    if (index >= 0) showGraphFocus(index);
  });
  showGraphFocus(0, false);
  if (graphFocusAreas.length > 1) {
    graphFocusTimer = window.setInterval(
      () => {
        if (graphFocusIndex >= graphFocusAreas.length - 1) {
          window.clearInterval(graphFocusTimer);
          return;
        }
        showGraphFocus(graphFocusIndex + 1);
      },
      INNER_SLIDE_DWELL_MS,
    );
  }
}

function showAlternate(card, requestedIndex = alternateIndex) {
  const block = $("#alternate-block");
  const available = Object.entries(card.extra_languages || {})
    .filter(([language, value]) => ALTERNATE_LANGUAGES[language] && value?.term);
  const enabled = card.mode === "knowledge" && available.length > 0;
  block.classList.toggle("hidden", !enabled);
  $("#language-grid").classList.toggle("has-alternate", enabled);
  if (!enabled) return;

  alternateIndex = requestedIndex % available.length;
  const [language, value] = available[alternateIndex];
  const metadata = ALTERNATE_LANGUAGES[language];
  block.className = `language-block alternate-block ${metadata.className}-block`;
  block.dir = language === "arabic" ? "rtl" : "ltr";
  text("#alternate-label", metadata.label);
  text("#alternate-term", value.term);
  optionalText("#alternate-reading", value.pronunciation || value.reading);
  optionalText("#alternate-meaning", value.meaning);
}

function startAlternateLoop(card) {
  window.clearInterval(alternateTimer);
  alternateIndex = 0;
  showAlternate(card, 0);
  const count = Object.values(card.extra_languages || {}).filter((value) => value?.term).length;
  if (card.mode !== "knowledge" || count < 2) return;
  alternateTimer = window.setInterval(() => {
    if (alternateIndex >= count - 1) {
      window.clearInterval(alternateTimer);
      return;
    }
    alternateIndex += 1;
    showAlternate(card, alternateIndex);
  }, INNER_SLIDE_DWELL_MS);
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
  const primaryTitle = ["answer", "question"].includes(card.mode)
    ? (card.english.term || card.title)
    : card.title;
  text("#card-title", primaryTitle);
  cardView.classList.toggle("long-title", primaryTitle.length > 80);
  cardView.classList.toggle("very-long-title", primaryTitle.length > 150);
  const translationLength = (card.japanese.term || "").length + (card.chinese.simplified || "").length;
  cardView.classList.toggle("dense-translations", translationLength > 130);
  optionalText("#card-subtitle", card.subtitle);
  text("#card-summary", card.summary_en);
  text("#origin-story", card.origin_story);
  text("#english-term", card.english.term || card.title);
  optionalText("#english-pronunciation", card.english.pronunciation);
  optionalText("#english-meaning", card.english.meaning);
  text("#word-card-term", card.english.term || card.title);
  optionalText("#word-card-phonetic", card.english.pronunciation);
  text("#word-card-definition", card.english.meaning || card.summary_en);
  optionalText("#japanese-meaning", card.japanese.meaning);
  const showTraditional = card.chinese.traditional
    && card.chinese.traditional !== card.chinese.simplified
    && card.chinese.simplified.length < 20;
  const chineseTokens = Array.isArray(card.chinese.ruby_tokens) ? card.chinese.ruby_tokens : [];
  renderRuby("#chinese-term", chineseTokens, card.chinese.simplified, card.chinese.pinyin);
  $("#chinese-term").title = showTraditional ? card.chinese.traditional : "";
  optionalText("#chinese-pinyin", chineseTokens.length ? "" : card.chinese.pinyin);
  optionalText("#chinese-meaning", card.chinese.meaning);
  text("#memory-hook", card.memory_hook);
  text(
    "#grounded-label",
    {
      word: "Book anchor + model context",
      knowledge: "Book-grounded · accepted atoms",
      answer: "Reviewed book translations",
      question: "Reviewed book translations",
      root: "Root book + affix book + model context",
      affix: "Affix book + root book + model context",
    }[card.mode] || "Book evidence attached",
  );
  renderOriginGraph(card);
  startAlternateLoop(card);
  renderSentenceCarousel(card);

  const tokens = Array.isArray(card.japanese.ruby_tokens) ? card.japanese.ruby_tokens : [];
  renderRuby("#japanese-ruby", tokens, card.japanese.term, card.japanese.reading);

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
  restartTransition(cardView, "card-switch-enter");
  updateCarouselChrome();
  if (refreshHistory) loadHistory();
}

function setMode(nextMode, preserveView = false) {
  const previousMode = mode;
  mode = MODE_COPY[nextMode] ? nextMode : "answer";
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
  if (ambientRouting) applyAmbientComposer();
  if (preserveView) return;
  if (mode === "chat") {
    show("chat");
  } else if (visibleView === "chat" || previousMode !== mode) {
    show("empty");
  }
}

async function submitChat(message) {
  $("#lab-starters")?.remove();
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
        thread_id: chatThreadId,
        parent_event_id: chatParentEventId,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    pending.remove();
    chatThreadId = payload.thread_id || chatThreadId;
    chatParentEventId = payload.event_id || chatParentEventId;
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
  ambientRouting = false;
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
    $("#lab-starters")?.remove();
    observations.reverse().forEach((item) => {
      renderChatMessage("user", item.prompt);
      renderChatMessage("assistant", item.response, { ...item.metrics, saved: true });
    });
  } catch (_error) {
    // Model Lab remains usable even if the optional ledger cannot be read.
  }
}

async function submitQuery(query, requestedMode = mode, context = {}) {
  query = String(query || "").trim();
  if (!query) return;
  setMode(requestedMode);
  if (mode === "chat") {
    await submitChat(query);
    return;
  }
  $("#query").value = query;
  $("#generate-button").disabled = true;
  text("#loading-kicker", "READING LOCALLY");
  text("#loading-title", "Composing the next card…");
  text("#loading-detail", "Qwen is working on this Raspberry Pi. Your text stays here.");
  show("loading");
  try {
    for (let poll = 0; poll < 300; poll += 1) {
      const response = await fetch("/api/cards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, mode, ...context }),
      });
      const payload = await response.json();
      if (response.status === 202 && payload.status === "preparing") {
        text("#loading-kicker", `LOCAL PIPELINE · ${payload.completed_jobs} / ${payload.total_jobs}`);
        text("#loading-title", payload.current_label || "Preparing accepted knowledge");
        text("#loading-detail", "Each finished step is saved. You may reconnect without losing progress.");
        await wait(Math.max(1000, Math.min(Number(payload.poll_after_ms) || 3000, 10000)));
        continue;
      }
      if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
      renderCard(payload);
      return;
    }
    throw new Error("Preparation is still running. Reopen this word to resume its saved progress.");
  } catch (error) {
    text("#error-message", error.message);
    show("error");
  } finally {
    $("#generate-button").disabled = false;
  }
}

function applyAmbientComposer() {
  text("#query-label", "Ask anything or enter one word");
  $("#query").placeholder = "inspection · origin: inspection · What should I learn?";
  $("#query").maxLength = 2000;
  $("#generate-button").textContent = "Go";
  const prompts = ["inspection", "origin: inspection", "What should I focus on?"];
  all(".examples button").forEach((button, index) => {
    button.dataset.query = prompts[index];
    button.dataset.mode = "ambient";
    button.textContent = prompts[index];
  });
}

async function submitIntent(query) {
  query = String(query || "").trim();
  if (!query) return;
  if (!ambientRouting) {
    await submitQuery(query, mode);
    return;
  }
  $("#generate-button").disabled = true;
  try {
    const response = await fetch("/api/intent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const route = await response.json();
    if (!response.ok) throw new Error(route.error || `Request failed (${response.status})`);
    ambientRouting = false;
    await submitQuery(route.query, route.mode);
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
    const morphologyItems = Object.values(health.morphology || {}).reduce((total, item) => total + (item.items || 0), 0);
    const lexiconItems = Object.values(health.lexicons || {}).reduce((total, item) => total + (item.entries || 0), 0);
    const sourceCount = (health.corpus.entries || 0) + bookItems + morphologyItems + lexiconItems;
    const deck = health.autonomous_deck || {};
    const lexical = health.autonomous_lexical || {};
    container.title = ready ? `${sourceCount.toLocaleString()} local source records loaded` : "";
    const bookProgress = deck.total
      ? `${(deck.accepted || 0).toLocaleString()} / ${deck.total.toLocaleString()} book cards`
      : "Local sources";
    const lexicalProgress = lexical.planned
      ? ` · ${lexical.planned.toLocaleString()} words planned`
      : "";
    const progress = bookProgress + lexicalProgress;
    text("#state-label", ready ? `${progress} · Qwen ready` : "Model or corpus is starting…");
  } catch (_error) {
    text("#state-label", "Terminal unavailable");
  }
}

async function loadHistory() {
  try {
    if (mode === "chat") return;
    const response = await fetch(`/api/cards?mode=${encodeURIComponent(mode)}&limit=1000`);
    const cards = await response.json();
    if (!response.ok || !Array.isArray(cards)) throw new Error("Card history unavailable");
    allSavedCards = cards;
    const shouldOpenLatest = mode !== "chat" && (!activeCard || activeCard.mode !== mode);
    rebuildModeCarousel(shouldOpenLatest);
  } catch (_error) {
    $("#history").replaceChildren(element("p", "quiet", "History unavailable."));
  }
}

function shuffledModeDeck(cards) {
  const deck = [...cards];
  // Keep the newest accepted card visible on refresh, then traverse every
  // other accepted card once in a shuffled order before the mode repeats.
  for (let index = deck.length - 1; index > 1; index -= 1) {
    const swapIndex = 1 + Math.floor(Math.random() * index);
    [deck[index], deck[swapIndex]] = [deck[swapIndex], deck[index]];
  }
  return deck;
}

function shuffledAmbientPass(cards, previousCardId = "") {
  const deck = [...cards];
  for (let index = deck.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(Math.random() * (index + 1));
    [deck[index], deck[swapIndex]] = [deck[swapIndex], deck[index]];
  }
  if (deck.length > 1 && deck[0].card_id === previousCardId) deck.push(deck.shift());
  return deck.map((card) => card.card_id);
}

function rememberAmbientCard(cardMode, cards, cardId) {
  if (!AMBIENT_MODE_ORDER.includes(cardMode) || !cardId || !cards.length) return;
  const acceptedIds = new Set(cards.map((card) => card.card_id));
  const previous = ambientModeDecks.get(cardMode);
  let remainingIds = previous
    ? previous.remainingIds.filter((id) => acceptedIds.has(id) && id !== cardId)
    : shuffledModeDeck(cards).map((card) => card.card_id).filter((id) => id !== cardId);
  if (previous) {
    const newIds = cards
      .filter((card) => !previous.acceptedIds.has(card.card_id) && card.card_id !== cardId)
      .map((card) => card.card_id);
    const newIdSet = new Set(newIds);
    remainingIds = [...newIds, ...remainingIds.filter((id) => !newIdSet.has(id))];
  }
  ambientModeDecks.set(cardMode, {
    acceptedIds,
    remainingIds,
    lastCardId: cardId,
  });
}

function takeAmbientCard(cardMode, cards) {
  if (!cards.length) return null;
  const byId = new Map(cards.map((card) => [card.card_id, card]));
  const acceptedIds = new Set(byId.keys());
  const previous = ambientModeDecks.get(cardMode);
  let remainingIds = previous
    ? previous.remainingIds.filter((id) => acceptedIds.has(id))
    : shuffledModeDeck(cards).map((card) => card.card_id);
  if (previous) {
    const newIds = cards
      .filter((card) => !previous.acceptedIds.has(card.card_id))
      .map((card) => card.card_id);
    const newIdSet = new Set(newIds);
    remainingIds = [
      ...newIds,
      ...remainingIds.filter((id) => !newIdSet.has(id)),
    ];
  }
  if (!remainingIds.length) {
    remainingIds = shuffledAmbientPass(cards, previous?.lastCardId || "");
  }
  const selectedId = remainingIds.shift();
  ambientModeDecks.set(cardMode, {
    acceptedIds,
    remainingIds,
    lastCardId: selectedId,
  });
  return byId.get(selectedId) || null;
}

async function acceptedCardsForMode(cardMode) {
  const response = await fetch(`/api/cards?mode=${encodeURIComponent(cardMode)}&limit=1000`);
  const cards = await response.json();
  if (!response.ok || !Array.isArray(cards)) throw new Error("Card history unavailable");
  return cards;
}

async function advanceAmbientMode(activityRevision) {
  for (let attempt = 0; attempt < AMBIENT_MODE_ORDER.length; attempt += 1) {
    const nextMode = AMBIENT_MODE_ORDER[ambientModeIndex % AMBIENT_MODE_ORDER.length];
    ambientModeIndex = (ambientModeIndex + 1) % AMBIENT_MODE_ORDER.length;
    let cards;
    try {
      cards = await acceptedCardsForMode(nextMode);
    } catch (_error) {
      // Skip one unavailable or empty accepted deck; the next mode remains usable.
      continue;
    }
    if (
      !ambientRouting
      || !autoplayEnabled
      || activityRevision !== userActivityRevision
    ) {
      scheduleCarousel();
      return;
    }
    const selected = takeAmbientCard(nextMode, cards);
    if (!selected) continue;
    setMode(nextMode, true);
    allSavedCards = cards;
    carouselCards = shuffledModeDeck(cards);
    carouselIndex = carouselCards.findIndex((card) => card.card_id === selected.card_id);
    renderModeDeckDots();
    renderCard(selected, false);
    scheduleCarousel();
    return;
  }
  scheduleCarousel();
}

function renderModeDeckDots() {
  const history = $("#history");
  history.replaceChildren();
  carouselCards.forEach((card, index) => {
    const button = element("button");
    button.title = card.title;
    button.classList.toggle("active", index === carouselIndex);
    button.addEventListener("click", () => {
      carouselIndex = index;
      renderCard(card, false);
      if (ambientRouting) rememberAmbientCard(mode, carouselCards, card.card_id);
      scheduleCarousel();
    });
    history.append(button);
  });
}

function rebuildModeCarousel(openLatest = false) {
  const history = $("#history");
  carouselCards = mode === "chat" ? [] : allSavedCards.filter((card) => card.mode === mode);
  carouselCards = shuffledModeDeck(carouselCards);
  history.replaceChildren();
  if (!carouselCards.length) {
    carouselIndex = -1;
    history.append(element("p", "quiet", mode === "chat" ? "Model Lab has its own ledger." : `No ${MODE_COPY[mode].card.toLowerCase()} cards yet.`));
    updateCarouselChrome();
    scheduleCarousel();
    return;
  }
  const found = carouselCards.findIndex((card) => card.card_id === activeCardId);
  carouselIndex = openLatest || found < 0 ? 0 : found;
  renderModeDeckDots();
  if (openLatest || found < 0) renderCard(carouselCards[carouselIndex], false);
  if (ambientRouting && carouselIndex >= 0) {
    rememberAmbientCard(mode, carouselCards, carouselCards[carouselIndex].card_id);
  }
  updateCarouselChrome();
  scheduleCarousel();
}

async function syncAcceptedDeck() {
  if (mode === "chat" || visibleView !== "card" || activeCard?.mode !== mode) return;
  const requestedMode = mode;
  try {
    const response = await fetch(
      "/api/cards?mode=" + encodeURIComponent(requestedMode) + "&limit=1000",
    );
    const cards = await response.json();
    if (!response.ok || !Array.isArray(cards) || mode !== requestedMode) return;
    const current = carouselCards[carouselIndex];
    const acceptedIds = new Set(cards.map((card) => card.card_id));
    const knownIds = new Set(carouselCards.map((card) => card.card_id));
    const newlyAccepted = cards.filter((card) => !knownIds.has(card.card_id));
    const removedCount = carouselCards.filter((card) => !acceptedIds.has(card.card_id)).length;
    allSavedCards = cards;
    if (!newlyAccepted.length && !removedCount) return;

    const newIds = new Set(newlyAccepted.map((card) => card.card_id));
    const existingPass = current
      ? [
        ...carouselCards.slice(carouselIndex + 1),
        ...carouselCards.slice(0, carouselIndex),
      ]
      : carouselCards;
    const remaining = existingPass.filter((card) => (
      acceptedIds.has(card.card_id)
      && card.card_id !== current?.card_id
      && !newIds.has(card.card_id)
    ));
    carouselCards = current
      ? [current, ...newlyAccepted, ...remaining]
      : shuffledModeDeck(cards);
    carouselIndex = current ? 0 : -1;
    renderModeDeckDots();
    updateCarouselChrome();
    if (!current && carouselCards.length) {
      carouselIndex = 0;
      renderCard(carouselCards[0], false);
      scheduleCarousel();
    }
  } catch (_error) {
    // Keep the current accepted deck untouched while the service is unavailable.
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
  if (ambientRouting) {
    rememberAmbientCard(mode, carouselCards, carouselCards[carouselIndex].card_id);
  }
  scheduleCarousel();
}

function activeInnerSlideCount() {
  if (["answer", "question"].includes(mode)) return Math.max(1, sentenceSlides.length);
  if (["word", "root", "affix"].includes(mode)) return Math.max(1, graphFocusAreas.length);
  if (mode === "knowledge") {
    return Math.max(
      1,
      Object.entries(activeCard?.extra_languages || {})
        .filter(([language, value]) => ALTERNATE_LANGUAGES[language] && value?.term)
        .length,
    );
  }
  return 1;
}

function scheduleCarousel() {
  if (autoplayTimer) clearTimeout(autoplayTimer);
  if (!autoplayEnabled) return;
  const dwell = Math.max(CARD_MIN_DWELL_MS, activeInnerSlideCount() * INNER_SLIDE_DWELL_MS);
  autoplayTimer = setTimeout(() => {
    if (mode === "chat") return;
    if (ambientRouting) {
      advanceAmbientMode(userActivityRevision);
      return;
    }
    if (carouselCards.length > 1) navigateCards(1);
  }, dwell);
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

all(".mode").forEach((button) => button.addEventListener("click", async () => {
  ambientRouting = false;
  const nextMode = button.dataset.mode;
  if (nextMode === "chat") {
    chatContextCardId = "";
    chatContextTitle = "";
    resetChat();
    setMode(nextMode);
    rebuildModeCarousel();
    return;
  }
  setMode(nextMode);
  await loadHistory();
}));
all(".examples button").forEach((button) => button.addEventListener("click", () => {
  if (ambientRouting || button.dataset.mode === "ambient") submitIntent(button.dataset.query);
  else submitQuery(button.dataset.query, button.dataset.mode || mode);
}));
$("#card-form").addEventListener("submit", (event) => { event.preventDefault(); submitIntent($("#query").value); });
$("#refresh-history").addEventListener("click", loadHistory);
$("#clear-chat").addEventListener("click", resetChat);
$("#discuss-card").addEventListener("click", discussCurrentCard);
$("#previous-card").addEventListener("click", () => navigateCards(-1));
$("#next-card").addEventListener("click", () => navigateCards(1));
$("#previous-sentence-slide").addEventListener("click", () => {
  window.clearInterval(sentenceSlideTimer);
  showSentenceSlide(sentenceSlideIndex - 1);
  scheduleCarousel();
});
$("#next-sentence-slide").addEventListener("click", () => {
  window.clearInterval(sentenceSlideTimer);
  showSentenceSlide(sentenceSlideIndex + 1);
  scheduleCarousel();
});
$("#previous-graph-focus").addEventListener("click", () => {
  window.clearInterval(graphFocusTimer);
  showGraphFocus(graphFocusIndex - 1);
  scheduleCarousel();
});
$("#next-graph-focus").addEventListener("click", () => {
  window.clearInterval(graphFocusTimer);
  showGraphFocus(graphFocusIndex + 1);
  scheduleCarousel();
});
$("#fit-graph").addEventListener("click", () => {
  window.clearInterval(graphFocusTimer);
  resetGraphAutofit();
  scheduleCarousel();
});
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
  scheduleGraphViewportFit(160);
});
document.addEventListener("pointermove", () => noteActivity(true), { passive: true });
document.addEventListener("pointerdown", () => noteActivity(true), { passive: true });
document.addEventListener("keydown", () => noteActivity(true));
document.addEventListener("focusin", () => noteActivity(true));
window.addEventListener("resize", () => scheduleGraphViewportFit(120), { passive: true });

const initialParameters = new URLSearchParams(location.search);
const initialMode = MODE_COPY[initialParameters.get("mode")] ? initialParameters.get("mode") : "answer";
ambientRouting = !initialParameters.has("mode");
setMode(initialMode);
if (initialParameters.has("display")) document.body.classList.add("display-mode");
loadHealth();
loadHistory();
renderLabStarters();
loadObservations();
setInterval(loadHealth, 30000);
setInterval(syncAcceptedDeck, ACCEPTED_DECK_SYNC_MS);
