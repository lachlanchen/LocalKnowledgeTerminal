"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
let mode = "answer";
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
let alternateTimer = null;
let alternateIndex = 0;
let originCy = null;
let overviewCy = null;
let graphFocusAreas = [];
let graphFocusIndex = 0;
let allSavedCards = [];
let sentenceSlides = [];
let sentenceSlideIndex = 0;
let sentenceSlideTimer = null;
let chromeTimer = null;
let ambientRouting = false;

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

function noteActivity() {
  document.body.classList.remove("chrome-collapsed");
  window.clearTimeout(chromeTimer);
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
    tokens.forEach((token) => {
      if (!token.r) {
        container.append(document.createTextNode(token.t || ""));
        return;
      }
      const ruby = document.createElement("ruby");
      ruby.append(document.createTextNode(token.t || ""), element("rt", "", token.r));
      container.append(ruby);
    });
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
    for (const marker of [". ", "? ", "! ", "; ", ", ", " "]) {
      boundary = Math.max(boundary, windowText.lastIndexOf(marker));
    }
    if (boundary < floor) boundary = maxCharacters;
    else boundary += 1;
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
      chunks.push(chunk);
      chunk = [];
      length = 0;
    }
    chunk.push(token);
    length += tokenLength;
    if (/[。！？?!]$/.test(String(token.t || "")) && length >= maxCharacters * 0.55) {
      chunks.push(chunk);
      chunk = [];
      length = 0;
    }
  });
  if (chunk.length) chunks.push(chunk);
  return chunks;
}

function buildSentenceSlides(card) {
  const slides = [];
  splitText(card.english.term || card.title, 165).forEach((term) => {
    slides.push({ language: "english", label: "ENGLISH", term, tokens: [] });
  });
  const japaneseTokens = Array.isArray(card.japanese.ruby_tokens) ? card.japanese.ruby_tokens : [];
  const japaneseChunks = splitRubyTokens(japaneseTokens, 64);
  if (japaneseChunks.length) {
    japaneseChunks.forEach((tokens) => slides.push({ language: "japanese", label: "日本語 · FURIGANA", term: "", tokens }));
  } else {
    splitText(card.japanese.term, 64).forEach((term) => {
      slides.push({ language: "japanese", label: "日本語 · FURIGANA", term, tokens: [], reading: card.japanese.reading });
    });
  }
  const chineseTokens = Array.isArray(card.chinese.ruby_tokens) ? card.chinese.ruby_tokens : [];
  const chineseChunks = splitRubyTokens(chineseTokens, 48);
  if (chineseChunks.length) {
    chineseChunks.forEach((tokens) => slides.push({ language: "chinese", label: "中文 · PINYIN", term: "", tokens }));
  } else {
    splitText(card.chinese.simplified, 48).forEach((term) => {
      slides.push({ language: "chinese", label: "中文 · PINYIN", term, tokens: [], reading: card.chinese.pinyin });
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

function showSentenceSlide(requestedIndex) {
  if (!sentenceSlides.length) return;
  sentenceSlideIndex = (requestedIndex + sentenceSlides.length) % sentenceSlides.length;
  const slide = sentenceSlides[sentenceSlideIndex];
  const stage = $("#sentence-stage");
  stage.className = `sentence-stage language-${slide.language}`;
  text("#sentence-language", slide.label);
  const content = element("p", "sentence-text");
  stage.replaceChildren(content);
  if (slide.language === "english") {
    content.textContent = slide.term;
  } else {
    renderRubyElement(content, slide.tokens, slide.term, slide.reading || "");
  }
  text("#sentence-position", `${sentenceSlideIndex + 1} / ${sentenceSlides.length}`);
  all("#sentence-dots button").forEach((button, index) => button.classList.toggle("active", index === sentenceSlideIndex));
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
    button.addEventListener("click", () => showSentenceSlide(index));
    return button;
  }));
  showSentenceSlide(0);
  if (sentenceSlides.length > 1) {
    sentenceSlideTimer = window.setInterval(() => showSentenceSlide(sentenceSlideIndex + 1), 9000);
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
        width: 146,
        height: 78,
        shape: "round-rectangle",
        "background-color": "#eaf2ff",
        "border-width": 3,
        "border-color": "#1769ff",
        label: "data(label)",
        color: "#17213d",
        "font-family": "Inter, Segoe UI, Noto Sans CJK SC, sans-serif",
        "font-size": 14,
        "font-weight": 700,
        "text-wrap": "wrap",
        "text-max-width": 126,
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
    { selector: "node.center", style: { width: 184, height: 106, shape: "ellipse", "background-color": "#ffcf3d", "border-color": "#17213d", "border-width": 4, "font-size": 17, "text-max-width": 158 } },
    { selector: ".dimmed", style: { opacity: 0 } },
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

function showGraphFocus(requestedIndex) {
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
  const target = focus.kind === "overview"
    ? originCy.elements()
    : focusNodes.union(focusEdges);
  originCy.animate({
    fit: { eles: target, padding: focus.kind === "overview" ? 28 : 70 },
    duration: 350,
  });
  text("#graph-focus-headline", focus.headline || focus.label);
  optionalText("#graph-focus-explanation", focus.explanation);
  text("#graph-focus-position", `${graphFocusIndex + 1} / ${graphFocusAreas.length}`);
  all("#graph-focus-dots button").forEach((button, index) => {
    button.classList.toggle("active", index === graphFocusIndex);
  });
}

function semanticGraphPositions(data) {
  const positions = new Map([[data.center_id, { x: 0, y: 0 }]]);
  const components = data.nodes.filter((node) => ["prefix", "root", "suffix"].includes(node.type));
  const histories = data.nodes.filter((node) => node.type === "historical");
  const related = data.nodes.filter((node) => (
    node.id !== data.center_id
    && !["prefix", "root", "suffix", "historical"].includes(node.type)
  ));
  const placeRows = (
    nodes, firstY, direction, perRow = 5, gapX = 170, rowGap = 100,
  ) => {
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
  placeRows(components, -90, -1, 5, 170);
  placeRows(histories, -205, -1, 5, 165);
  placeRows(related, 110, 1, 5, 170);
  data.nodes.forEach((node, index) => {
    if (!positions.has(node.id)) positions.set(node.id, { x: index * 170, y: 145 });
  });
  return positions;
}

function renderOriginGraph(card) {
  const graph = $("#origin-graph");
  const canvas = $("#origin-canvas");
  const overview = $("#graph-overview");
  originCy?.destroy();
  overviewCy?.destroy();
  originCy = null;
  overviewCy = null;
  canvas.replaceChildren();
  overview.replaceChildren();
  const data = unifiedGraph(card);
  const enabled = ["word", "root", "affix"].includes(card.mode)
    && Array.isArray(data?.nodes) && data.nodes.length > 1;
  graph.classList.toggle("hidden", !enabled);
  if (!enabled) return;
  text("#graph-kind", card.mode === "word" ? "WORD ORIGIN GRAPH" : `${card.mode.toUpperCase()} GRAPH`);
  if (typeof window.cytoscape !== "function") {
    canvas.append(element("p", "graph-error", "Graph renderer unavailable."));
    return;
  }
  const semanticPositions = semanticGraphPositions(data);
  const graphNodes = data.nodes.map((node) => ({
    data: {
      id: node.id,
      label: [node.form || "—", node.language, node.meaning].filter(Boolean).join("\n"),
      type: node.type || "related",
    },
    classes: [
      node.basis === "book" ? "book" : "model",
      node.type || "related",
      node.id === data.center_id ? "center" : "",
    ].filter(Boolean).join(" "),
    position: semanticPositions.get(node.id),
  }));
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
    minZoom: .25,
    maxZoom: 2.2,
    wheelSensitivity: .16,
    boxSelectionEnabled: false,
    autoungrabify: true,
    layout: { name: "preset", fit: true, padding: 24 },
    style: graphStyles(),
  });
  originCy.fit(originCy.elements(), 28);
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
    button.addEventListener("click", () => showGraphFocus(index));
    return button;
  }));
  $("#graph-focus-controls").classList.toggle("hidden", graphFocusAreas.length < 2);
  originCy.on("tap", "node", (event) => {
    const index = graphFocusAreas.findIndex((area) => (
      area.kind !== "overview" && area.node_ids?.includes(event.target.id())
    ));
    if (index >= 0) showGraphFocus(index);
  });
  showGraphFocus(0);
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
    alternateIndex += 1;
    showAlternate(card, alternateIndex);
  }, 9000);
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
  if (ambientRouting && mode === "answer") applyAmbientComposer();
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
    const sourceCount = (health.corpus.entries || 0) + bookItems + morphologyItems;
    text("#state-label", ready ? `${sourceCount.toLocaleString()} sources · model ready` : "Model or corpus is starting…");
  } catch (_error) {
    text("#state-label", "Terminal unavailable");
  }
}

async function loadHistory() {
  try {
    const response = await fetch("/api/cards?limit=30");
    const cards = await response.json();
    if (!response.ok || !Array.isArray(cards)) throw new Error("Card history unavailable");
    allSavedCards = cards;
    const shouldOpenLatest = mode !== "chat" && (!activeCard || activeCard.mode !== mode);
    rebuildModeCarousel(shouldOpenLatest);
  } catch (_error) {
    $("#history").replaceChildren(element("p", "quiet", "History unavailable."));
  }
}

function shuffledAnswerDeck(cards) {
  const deck = [...cards];
  // Keep the newly prepared answer visible on refresh, then traverse every
  // other answer once in a shuffled order before the carousel repeats.
  for (let index = deck.length - 1; index > 1; index -= 1) {
    const swapIndex = 1 + Math.floor(Math.random() * index);
    [deck[index], deck[swapIndex]] = [deck[swapIndex], deck[index]];
  }
  return deck;
}

function rebuildModeCarousel(openLatest = false) {
  const history = $("#history");
  carouselCards = mode === "chat" ? [] : allSavedCards.filter((card) => card.mode === mode);
  if (mode === "answer") carouselCards = shuffledAnswerDeck(carouselCards);
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
  carouselCards.forEach((card, index) => {
    const button = element("button");
    button.title = card.title;
    button.classList.toggle("active", index === carouselIndex);
    button.addEventListener("click", () => {
      carouselIndex = index;
      renderCard(card, false);
    });
    history.append(button);
  });
  if (openLatest || found < 0) renderCard(carouselCards[carouselIndex], false);
  updateCarouselChrome();
  scheduleCarousel();
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
  rebuildModeCarousel(true);
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
$("#previous-sentence-slide").addEventListener("click", () => showSentenceSlide(sentenceSlideIndex - 1));
$("#next-sentence-slide").addEventListener("click", () => showSentenceSlide(sentenceSlideIndex + 1));
$("#previous-graph-focus").addEventListener("click", () => showGraphFocus(graphFocusIndex - 1));
$("#next-graph-focus").addEventListener("click", () => showGraphFocus(graphFocusIndex + 1));
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
document.addEventListener("pointermove", noteActivity, { passive: true });
document.addEventListener("pointerdown", noteActivity, { passive: true });
document.addEventListener("keydown", noteActivity);
document.addEventListener("focusin", noteActivity);

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
