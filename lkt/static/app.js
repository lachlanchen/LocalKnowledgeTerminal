"use strict";

const $ = (selector) => document.querySelector(selector);
const all = (selector) => [...document.querySelectorAll(selector)];
const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
async function fetchJsonWithTimeout(resource, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abortFromCaller = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(resource, { ...options, signal: controller.signal });
    const payload = await response.json();
    return { response, payload };
  } finally {
    window.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}
let mode = "question";
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
let knowledgeTickerFrame = null;
let knowledgeTickerRevision = 0;
const graphDerivativePager = {
  items: [],
  pageIndex: 0,
  capacity: 0,
  revision: 0,
  timer: null,
  transition: null,
  frame: null,
};
let ambientRouting = false;
let ambientModeIndex = 0;
let userActivityRevision = 0;
let historyRequestRevision = 0;
let cardSubmissionRevision = 0;
let activeCardSubmissionController = null;
let cardHydrationRevision = 0;
let activeCardHydrationController = null;
let knowledgeGeneration = "";
const ambientModeDecks = new Map();
const modeDeckTotals = new Map();
const modeSummaryCache = new Map();
const modeSummaryRequests = new Map();
const hydratedCardCache = new Map();
let pageRouteRevision = 0;
let pageRouteController = null;
let restoringPageRoute = false;
let pageUrlSyncQueued = false;
let pinnedPageCardId = "";
let recenteredEntityId = "";
let recenteredGraphCard = null;
let ambientPrefetch = null;
let healthRequestInFlight = false;
let deckSyncInFlight = false;

const INNER_SLIDE_DWELL_MS = 18000;
const CARD_MIN_DWELL_MS = 30000;
const ACCEPTED_DECK_SYNC_MS = 30000;
const HISTORY_DOT_LIMIT = 18;
const HISTORY_PAGE_LIMIT = 64;
const HYDRATED_CARD_CACHE_LIMIT = 96;
const AMBIENT_MODE_ORDER = ["question", "answer", "knowledge", "word", "root", "affix"];
const DISPLAY_SETTINGS_STORAGE_KEY = "lkt-display-settings-v1";
const BOOK_LANGUAGE_CODES = ["en", "ja", "zh"];
const DEFAULT_LEXICAL_LANGUAGE_CODES = ["en", "ja", "zh", "fr", "ar"];
const LEGACY_LANGUAGE_CODES = {
  english: "en",
  japanese: "ja",
  chinese: "zh",
  french: "fr",
  arabic: "ar",
};
const BUILTIN_LANGUAGE_METADATA = {
  fr: { label: "FRANÇAIS · PRONONCIATION", className: "french" },
  ar: { label: "العربية · النطق", className: "arabic" },
};
const RTL_LANGUAGE_BASES = new Set(["ar", "fa", "he", "ps", "ur"]);
const DEFAULT_DISPLAY_SETTINGS = {
  randomCards: true,
  ambientModes: [...AMBIENT_MODE_ORDER],
  languages: {
    book: [...BOOK_LANGUAGE_CODES],
    lexical: [...DEFAULT_LEXICAL_LANGUAGE_CODES],
  },
  knownLanguages: [...DEFAULT_LEXICAL_LANGUAGE_CODES],
};

let displaySettings = loadDisplaySettings();

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

function defaultDisplaySettings() {
  return {
    randomCards: DEFAULT_DISPLAY_SETTINGS.randomCards,
    ambientModes: [...DEFAULT_DISPLAY_SETTINGS.ambientModes],
    languages: {
      book: [...DEFAULT_DISPLAY_SETTINGS.languages.book],
      lexical: [...DEFAULT_DISPLAY_SETTINGS.languages.lexical],
    },
    knownLanguages: [...DEFAULT_DISPLAY_SETTINGS.knownLanguages],
  };
}

function normalizeLanguageCode(value) {
  const candidate = String(value || "").trim().toLocaleLowerCase().replaceAll("_", "-");
  const legacyCode = LEGACY_LANGUAGE_CODES[candidate];
  if (legacyCode) return legacyCode;
  return /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(candidate) ? candidate : "";
}

function languageBase(code) {
  return normalizeLanguageCode(code).split("-")[0] || "";
}

function languageName(code) {
  const normalized = normalizeLanguageCode(code);
  if (!normalized) return "Language";
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(normalized) || normalized.toLocaleUpperCase();
  } catch (_error) {
    return normalized.toLocaleUpperCase();
  }
}

function languageDirection(code) {
  return RTL_LANGUAGE_BASES.has(languageBase(code)) ? "rtl" : "ltr";
}

function languageMetadata(code) {
  const normalized = normalizeLanguageCode(code);
  const builtin = BUILTIN_LANGUAGE_METADATA[normalized];
  if (builtin) return builtin;
  return {
    label: `${languageName(normalized).toLocaleUpperCase()} · PRONUNCIATION`,
    className: `language-${normalized.replace(/[^a-z0-9-]/g, "")}`,
  };
}

function extraLanguageEntries(card) {
  const entries = new Map();
  Object.entries(card?.extra_languages || {}).forEach(([key, rawValue]) => {
    const value = typeof rawValue === "string" ? { term: rawValue } : rawValue;
    if (!value || typeof value !== "object") return;
    const code = [value.language_code, value.code, key, value.language]
      .map(normalizeLanguageCode)
      .find(Boolean);
    if (!code) return;
    const current = entries.get(code) || {};
    entries.set(code, {
      ...current,
      ...value,
      term: value.term || current.term || "",
      meaning: value.meaning || current.meaning || "",
    });
  });
  return [...entries.entries()];
}

function registerDiscoveredLanguages(codes) {
  let changed = false;
  codes.map(normalizeLanguageCode).filter(Boolean).forEach((code) => {
    if (displaySettings.knownLanguages.includes(code)) return;
    displaySettings.knownLanguages.push(code);
    displaySettings.languages.lexical.push(code);
    changed = true;
  });
  if (changed) saveDisplaySettings();
}

function registerCardLanguages(card) {
  registerDiscoveredLanguages(extraLanguageEntries(card).map(([code]) => code));
}

function languageSettingCodes(profile) {
  if (profile === "book") return [...BOOK_LANGUAGE_CODES];
  const priority = new Map(DEFAULT_LEXICAL_LANGUAGE_CODES.map((code, index) => [code, index]));
  return [...new Set([...DEFAULT_LEXICAL_LANGUAGE_CODES, ...displaySettings.knownLanguages])]
    .map(normalizeLanguageCode)
    .filter(Boolean)
    .sort((left, right) => {
      const leftPriority = priority.has(left) ? priority.get(left) : Number.MAX_SAFE_INTEGER;
      const rightPriority = priority.has(right) ? priority.get(right) : Number.MAX_SAFE_INTEGER;
      return leftPriority - rightPriority || languageName(left).localeCompare(languageName(right));
    });
}

function renderLanguageSettingOptions(profile) {
  const container = $("#settings-language-options");
  container.replaceChildren(...languageSettingCodes(profile).map((code) => {
    const label = element("label");
    const input = element("input");
    input.type = "checkbox";
    input.value = code;
    input.dataset.settingLanguage = "";
    label.append(
      input,
      element("span", "", code.toLocaleUpperCase()),
      element("small", "", languageName(code)),
    );
    return label;
  }));
}

function loadDisplaySettings() {
  const defaults = defaultDisplaySettings();
  try {
    const saved = JSON.parse(localStorage.getItem(DISPLAY_SETTINGS_STORAGE_KEY) || "null");
    if (!saved || typeof saved !== "object") return defaults;
    const knownLanguages = Array.isArray(saved.knownLanguages)
      ? saved.knownLanguages.map(normalizeLanguageCode).filter(Boolean)
      : [];
    defaults.knownLanguages = [...new Set([...defaults.knownLanguages, ...knownLanguages])];
    ["book", "lexical"].forEach((profile) => {
      const selected = Array.isArray(saved.languages?.[profile])
        ? saved.languages[profile]
          .map(normalizeLanguageCode)
          .filter((code) => code && (profile !== "book" || BOOK_LANGUAGE_CODES.includes(code)))
        : [];
      if (selected.length) defaults.languages[profile] = [...new Set(selected)];
    });
    defaults.knownLanguages = [...new Set([
      ...defaults.knownLanguages,
      ...defaults.languages.lexical,
    ])];
    defaults.randomCards = saved.randomCards !== false;
    const ambientModes = Array.isArray(saved.ambientModes)
      ? AMBIENT_MODE_ORDER.filter((cardMode) => saved.ambientModes.includes(cardMode))
      : [];
    if (ambientModes.length) defaults.ambientModes = ambientModes;
  } catch (_error) {
    // Private browsing and locked-down kiosk profiles may deny local storage.
  }
  return defaults;
}

function saveDisplaySettings() {
  try {
    localStorage.setItem(DISPLAY_SETTINGS_STORAGE_KEY, JSON.stringify(displaySettings));
  } catch (_error) {
    // The active page still keeps the preferences for this session.
  }
}

function languageProfileForMode(cardMode = mode) {
  return ["answer", "question"].includes(cardMode) ? "book" : "lexical";
}

function activeAmbientModeOrder() {
  const visibleModes = new Set(all(".mode")
    .filter((button) => {
      const style = getComputedStyle(button);
      return !button.disabled && style.display !== "none" && style.visibility !== "hidden";
    })
    .map((button) => button.dataset.mode)
    .filter(Boolean));
  return AMBIENT_MODE_ORDER.filter((cardMode) => (
    displaySettings.ambientModes.includes(cardMode) && visibleModes.has(cardMode)
  ));
}

function ambientIndexAt(cardMode) {
  const enabledModes = activeAmbientModeOrder();
  const currentIndex = enabledModes.indexOf(cardMode);
  return currentIndex < 0 ? 0 : currentIndex;
}

function ambientIndexAfter(cardMode) {
  const enabledModes = activeAmbientModeOrder();
  const currentIndex = enabledModes.indexOf(cardMode);
  return currentIndex < 0 ? 0 : (currentIndex + 1) % enabledModes.length;
}

function visibleLanguages(cardMode = mode) {
  return new Set(displaySettings.languages[languageProfileForMode(cardMode)].map(normalizeLanguageCode));
}

function languageEnabled(code, cardMode = mode) {
  return visibleLanguages(cardMode).has(normalizeLanguageCode(code));
}

function availableAlternateLanguages(card) {
  const visible = visibleLanguages(card?.mode);
  return extraLanguageEntries(card)
    .filter(([code, value]) => (
      !BOOK_LANGUAGE_CODES.includes(code)
      && visible.has(code)
      && value.term
    ));
}

function syncSettingsControls() {
  const profile = languageProfileForMode();
  const selected = new Set(displaySettings.languages[profile]);
  renderLanguageSettingOptions(profile);
  text("#settings-language-scope", profile === "book" ? "QUESTION + ANSWER" : "WORD CARDS + GRAPHS");
  all("[data-setting-language]").forEach((input) => {
    input.checked = selected.has(input.value);
  });
  const ambientModes = new Set(activeAmbientModeOrder());
  all("[data-setting-mode]").forEach((input) => {
    input.checked = ambientModes.has(input.value);
  });
  const loopCount = ambientModes.size;
  text(
    "#settings-loop-scope",
    loopCount === 6 ? "ALL 6 MODES" : `${loopCount} MODE${loopCount === 1 ? "" : "S"}`,
  );
  $("#random-cards-setting").checked = displaySettings.randomCards;
}

function applySettingsControls() {
  const profile = languageProfileForMode();
  let selected = all("[data-setting-language]:checked:not(:disabled)").map((input) => input.value);
  if (!selected.length) {
    selected = ["en"];
    $("[data-setting-language][value='en']").checked = true;
  }
  displaySettings.languages[profile] = selected;
  let ambientModes = all("[data-setting-mode]:checked").map((input) => input.value);
  if (!ambientModes.length) {
    const fallbackMode = AMBIENT_MODE_ORDER.includes(mode) ? mode : AMBIENT_MODE_ORDER[0];
    ambientModes = [fallbackMode];
    $(`[data-setting-mode][value='${fallbackMode}']`).checked = true;
  }
  displaySettings.ambientModes = AMBIENT_MODE_ORDER.filter((cardMode) => (
    ambientModes.includes(cardMode)
  ));
  displaySettings.randomCards = $("#random-cards-setting").checked;
  saveDisplaySettings();
  ambientModeIndex = ambientIndexAfter(mode);
  ambientModeDecks.clear();
  if (activeCard?.mode === mode) renderCard(activeCard, false);
  if (mode !== "chat") rebuildModeCarousel(false);
  syncSettingsControls();
}

function resetDisplaySettings() {
  displaySettings = defaultDisplaySettings();
  saveDisplaySettings();
  syncSettingsControls();
  applySettingsControls();
}

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
  if (!["loading", "error"].includes(name)) syncPageUrl();
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
  if (languageEnabled("en", card.mode)) {
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
  }
  const japaneseTokens = Array.isArray(card.japanese.ruby_tokens) ? card.japanese.ruby_tokens : [];
  const japaneseGrammar = grammarAnalysis(card, "ja", card.japanese.term);
  const japaneseChunks = splitRubyTokens(
    annotateRubyGrammar(japaneseTokens, japaneseGrammar, card.japanese.term),
    64,
  );
  if (languageEnabled("ja", card.mode)) {
    if (japaneseChunks.length) {
      japaneseChunks.forEach((tokens) => slides.push({ language: "japanese", label: "日本語 · FURIGANA", term: "", tokens }));
    } else {
      splitText(card.japanese.term, 64).forEach((term) => {
        slides.push({ language: "japanese", label: "日本語 · FURIGANA", term, tokens: [], reading: card.japanese.reading });
      });
    }
  }
  const chineseTokens = Array.isArray(card.chinese.ruby_tokens) ? card.chinese.ruby_tokens : [];
  const chineseGrammar = grammarAnalysis(card, "zh", card.chinese.simplified);
  const chineseChunks = splitRubyTokens(
    annotateRubyGrammar(chineseTokens, chineseGrammar, card.chinese.simplified),
    48,
  );
  if (languageEnabled("zh", card.mode)) {
    if (chineseChunks.length) {
      chineseChunks.forEach((tokens) => slides.push({ language: "chinese", label: "中文 · PINYIN", term: "", tokens }));
    } else {
      splitText(card.chinese.simplified, 48).forEach((term) => {
        slides.push({ language: "chinese", label: "中文 · PINYIN", term, tokens: [], reading: card.chinese.pinyin });
      });
    }
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

const KNOWLEDGE_TICKER_SELECTORS = [
  "[data-overflow-role]:not(#evidence-list):not(#evidence-title)",
];
const LEXICAL_TICKER_MODES = new Set(["knowledge", "word", "root", "affix"]);
const NON_VISUAL_GRAPH_RELATIONS = new Set([
  "has-language",
  "has-period",
  "in-language",
  "language",
  "language-of",
  "period",
]);
const knowledgeTickerMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const EVIDENCE_AUTO_SCROLL_PAUSE = 8000;
const EVIDENCE_AUTO_SCROLL_RATE = 18;
let evidenceAutoScrollState = null;
const KNOWLEDGE_AUTOFIT_SELECTORS = [
  ".word-card-label",
  ".word-card-hero h1",
  ".word-card-phonetic",
  ".word-card-hero > p",
  ".mode-knowledge .language-block > span",
  ".mode-knowledge .language-block h2",
  ".mode-knowledge .language-block small",
  ".mode-knowledge .language-block p",
];

function stopEvidenceAutoScroll(lane = null) {
  const state = evidenceAutoScrollState;
  if (state && (!lane || state.lane === lane)) {
    if (state.frame !== null) window.cancelAnimationFrame(state.frame);
    evidenceAutoScrollState = null;
  }
  const target = lane || state?.lane;
  if (!target) return;
  target.classList.remove("evidence-auto-scroll");
  target.removeAttribute("tabindex");
  target.onwheel = null;
  target.ontouchstart = null;
  target.onpointerdown = null;
  target.onmouseenter = null;
  target.onmouseleave = null;
  target.onfocusin = null;
  target.onfocusout = null;
  target.onkeydown = null;
}

function pauseEvidenceAutoScroll(lane, delay = EVIDENCE_AUTO_SCROLL_PAUSE) {
  if (evidenceAutoScrollState?.lane !== lane) return;
  evidenceAutoScrollState.pauseUntil = performance.now() + delay;
}

function fitEvidenceAutoScrollLane(lane) {
  if (!lane.textContent.trim() || lane.clientHeight <= 1) return;
  const maxScroll = lane.scrollHeight - lane.clientHeight;
  if (maxScroll <= 1) return;

  lane.classList.add("evidence-auto-scroll");
  lane.tabIndex = 0;
  const state = {
    lane,
    frame: null,
    last: performance.now(),
    pauseUntil: performance.now() + 1400,
    hovered: false,
  };
  evidenceAutoScrollState = state;
  const pause = () => pauseEvidenceAutoScroll(lane);
  lane.onwheel = pause;
  lane.ontouchstart = pause;
  lane.onpointerdown = pause;
  lane.onmouseenter = () => {
    if (evidenceAutoScrollState === state) state.hovered = true;
  };
  lane.onmouseleave = () => {
    if (evidenceAutoScrollState !== state) return;
    state.hovered = false;
    pauseEvidenceAutoScroll(lane, 1800);
  };
  lane.onfocusin = pause;
  lane.onfocusout = () => pauseEvidenceAutoScroll(lane, 1800);
  lane.onkeydown = (event) => {
    if (!["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) return;
    event.stopPropagation();
    pause();
  };

  const advance = (now) => {
    if (evidenceAutoScrollState !== state) return;
    const elapsed = Math.min(80, Math.max(0, now - state.last));
    state.last = now;
    const maximum = lane.scrollHeight - lane.clientHeight;
    if (maximum <= 1) {
      stopEvidenceAutoScroll(lane);
      return;
    }
    if (!state.hovered && now >= state.pauseUntil) {
      lane.scrollTop = lane.scrollTop >= maximum - .5
        ? 0
        : Math.min(maximum, lane.scrollTop + (elapsed * EVIDENCE_AUTO_SCROLL_RATE / 1000));
    }
    state.frame = window.requestAnimationFrame(advance);
  };
  state.frame = window.requestAnimationFrame(advance);
}

function restoreKnowledgeTickerLane(lane) {
  if (lane.id === "evidence-list") stopEvidenceAutoScroll(lane);
  const primary = lane.querySelector(
    ":scope > .knowledge-ticker-track > .knowledge-ticker-copy:not([aria-hidden='true'])",
  );
  if (primary) lane.replaceChildren(...Array.from(primary.childNodes));
  lane.classList.remove("knowledge-ticker-lane", "is-overflowing");
  lane.classList.remove("knowledge-title-fit", "knowledge-static-fit");
  lane.removeAttribute("data-motion-axis");
  lane.style.removeProperty("--ticker-gap");
  lane.style.removeProperty("--ticker-shift");
  lane.style.removeProperty("--ticker-shift-y");
  lane.style.removeProperty("--ticker-duration");
  lane.removeAttribute("tabindex");
  lane.onkeydown = null;
  if (lane.dataset.knowledgeFitFont === "true") {
    lane.style.removeProperty("font-size");
    delete lane.dataset.knowledgeFitFont;
  }
  if (lane.dataset.knowledgeFitMaxHeight === "true") {
    lane.style.removeProperty("max-block-size");
    delete lane.dataset.knowledgeFitMaxHeight;
  }
}

function resetKnowledgeTickers() {
  if (knowledgeTickerFrame !== null) window.cancelAnimationFrame(knowledgeTickerFrame);
  knowledgeTickerFrame = null;
  knowledgeTickerRevision += 1;
  stopEvidenceAutoScroll($("#evidence-list"));
  all(".knowledge-ticker-lane").forEach(restoreKnowledgeTickerLane);
  all(".knowledge-title-fit, .knowledge-static-fit").forEach(restoreKnowledgeTickerLane);
}

function knowledgeWordCardTextKind(lane) {
  if (activeCard?.mode !== "knowledge" || !lane.closest(".card-view.mode-knowledge")) return "";
  if (lane.matches(".language-block h2")) return "translation-title";
  if (lane.matches(".language-block small, .language-block p")) return "translation-subtext";
  if (lane.matches("#word-card-term")) return "hero-title";
  if (lane.matches("#word-card-phonetic")) return "hero-reading";
  if (lane.matches("#word-card-definition")) return "hero-subtext";
  return "";
}

function knowledgeTranslationTitleMaxHeight(lane) {
  const block = lane.closest(".language-block");
  if (!block || block.clientHeight <= 1) return 0;
  const blockStyle = getComputedStyle(block);
  const titleStyle = getComputedStyle(lane);
  const innerHeight = block.clientHeight
    - (Number.parseFloat(blockStyle.paddingTop) || 0)
    - (Number.parseFloat(blockStyle.paddingBottom) || 0);
  const labelHeight = block.querySelector(":scope > span")?.getBoundingClientRect().height || 0;
  const reading = block.querySelector(":scope > small:not(.hidden)");
  const readingHeight = reading?.getClientRects().length
    ? reading.getBoundingClientRect().height
    : 0;
  const meaning = block.querySelector(":scope > p:not(.hidden)");
  const margins = (Number.parseFloat(titleStyle.marginTop) || 0)
    + (Number.parseFloat(titleStyle.marginBottom) || 0)
    + (meaning ? Number.parseFloat(getComputedStyle(meaning).marginTop) || 0 : 0);
  const compact = $("#card-view").clientWidth < 1200 || $("#card-view").clientHeight < 620;
  const meaningReserve = Math.max(compact ? 42 : 96, innerHeight * .32);
  const available = innerHeight - labelHeight - readingHeight - margins - meaningReserve;
  return Math.max(compact ? 36 : 88, available);
}

function fitKnowledgeWordCardFont(
  lane,
  { widthOnly = false, title = false, maxHeight = 0 } = {},
) {
  const cardView = $("#card-view");
  lane.classList.add(title ? "knowledge-title-fit" : "knowledge-static-fit");
  lane.style.removeProperty("font-size");
  delete lane.dataset.knowledgeFitFont;
  if (maxHeight > 1) {
    lane.style.setProperty("max-block-size", `${Math.floor(maxHeight)}px`);
    lane.dataset.knowledgeFitMaxHeight = "true";
  }
  if (!lane.textContent.trim() || lane.clientWidth <= 1 || lane.clientHeight <= 1) return true;

  const upper = Number.parseFloat(getComputedStyle(lane).fontSize);
  if (!Number.isFinite(upper) || upper <= 0) return true;
  const compact = cardView.clientWidth < 1200 || cardView.clientHeight < 620;
  const floor = title
    ? (compact ? 18 : 42)
    : (compact ? 12 : 18);
  const minimum = Math.min(upper, floor);
  const fits = () => (
    lane.scrollWidth <= lane.clientWidth
    && (widthOnly || lane.scrollHeight <= lane.clientHeight)
  );
  if (fits()) return true;

  lane.style.setProperty("font-size", `${minimum}px`, "important");
  lane.dataset.knowledgeFitFont = "true";
  if (!fits()) return false;

  let lower = minimum;
  let higher = upper;
  for (let pass = 0; pass < 12; pass += 1) {
    const candidate = (lower + higher) / 2;
    lane.style.setProperty("font-size", `${candidate}px`, "important");
    if (fits()) lower = candidate;
    else higher = candidate;
  }
  lane.style.setProperty("font-size", `${lower.toFixed(2)}px`, "important");
  return fits();
}

function fitKnowledgeTickerLane(lane) {
  restoreKnowledgeTickerLane(lane);
  if (lane.id === "evidence-list") {
    fitEvidenceAutoScrollLane(lane);
    return;
  }
  if (lane.matches(".graph-derivative-term")) {
    fitDerivativeRailTerm(lane);
  }
  if (!lane.textContent.trim() || lane.clientWidth <= 1 || lane.closest(".hidden")) return;

  const wordCardKind = knowledgeWordCardTextKind(lane);
  if (wordCardKind === "translation-subtext") {
    if (fitKnowledgeWordCardFont(lane)) return;
  }
  if (wordCardKind === "hero-subtext") {
    const hero = lane.closest(".word-card-hero");
    const availableHeight = hero
      ? hero.getBoundingClientRect().bottom - lane.getBoundingClientRect().top
      : 0;
    fitKnowledgeWordCardFont(lane, { maxHeight: availableHeight });
    return;
  }
  if (wordCardKind === "translation-title") {
    fitKnowledgeWordCardFont(lane, {
      title: true,
      maxHeight: knowledgeTranslationTitleMaxHeight(lane),
    });
  }
  const forceHorizontal = ["translation-title", "hero-title", "hero-reading"].includes(wordCardKind);
  const preferredAxis = forceHorizontal || lane.dataset.overflowRole === "term"
    ? "horizontal"
    : "vertical";
  let axis = preferredAxis;
  lane.classList.add("knowledge-ticker-lane");
  lane.dataset.motionAxis = axis;
  const blockLane = lane.id === "evidence-list"
    || lane.classList.contains("graph-derivative-rail");
  const track = document.createElement(blockLane ? "div" : "span");
  track.className = "knowledge-ticker-track";
  const copy = document.createElement(blockLane ? "div" : "span");
  copy.className = "knowledge-ticker-copy";
  copy.dir = getComputedStyle(lane).direction === "rtl" ? "rtl" : "ltr";
  while (lane.firstChild) copy.append(lane.firstChild);
  track.append(copy);
  lane.append(track);

  let copyWidth = Math.ceil(Math.max(copy.scrollWidth, copy.getBoundingClientRect().width));
  let copyHeight = Math.ceil(Math.max(copy.scrollHeight, copy.getBoundingClientRect().height));
  const fontSize = Number.parseFloat(getComputedStyle(lane).fontSize) || 0;
  const verticalTolerance = Math.max(2, Math.ceil(fontSize * .18));
  const horizontalOverflow = copyWidth > lane.clientWidth + 1;
  const verticalOverflow = copyHeight > lane.clientHeight + verticalTolerance;
  axis = forceHorizontal
    ? "horizontal"
    : verticalOverflow ? "vertical" : horizontalOverflow ? "horizontal" : preferredAxis;
  if (axis !== preferredAxis) {
    lane.dataset.motionAxis = axis;
    copyWidth = Math.ceil(Math.max(copy.scrollWidth, copy.getBoundingClientRect().width));
    copyHeight = Math.ceil(Math.max(copy.scrollHeight, copy.getBoundingClientRect().height));
  }
  const overflowing = axis === "horizontal"
    ? copyWidth > lane.clientWidth + 1
    : copyHeight > lane.clientHeight + verticalTolerance;
  if (!overflowing) return;

  const travel = axis === "horizontal" ? copyWidth : copyHeight;
  const gap = axis === "horizontal"
    ? Math.max(36, Math.min(96, Math.round(lane.clientWidth * .12)))
    : Math.max(18, Math.min(44, Math.round(lane.clientHeight * .16)));
  const duplicate = copy.cloneNode(true);
  duplicate.setAttribute("aria-hidden", "true");
  track.append(duplicate);
  lane.style.setProperty("--ticker-gap", `${gap}px`);
  lane.style.setProperty("--ticker-shift", `${-(copyWidth + gap)}px`);
  lane.style.setProperty("--ticker-shift-y", `${-(copyHeight + gap)}px`);
  lane.style.setProperty(
    "--ticker-duration",
    `${Math.max(10, axis === "horizontal" ? (copyWidth + gap) / 34 : (travel + gap) / 24).toFixed(2)}s`,
  );
  lane.classList.add("is-overflowing");

  if (knowledgeTickerMotion.matches) {
    lane.tabIndex = 0;
    lane.onkeydown = (event) => {
      const keys = axis === "horizontal"
        ? ["ArrowLeft", "ArrowRight", "Home", "End"]
        : ["ArrowUp", "ArrowDown", "Home", "End"];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Home") lane.scrollTo({ left: 0, top: 0 });
      if (event.key === "End") lane.scrollTo({ left: lane.scrollWidth, top: lane.scrollHeight });
      if (event.key === "ArrowLeft") lane.scrollBy({ left: -lane.clientWidth * .65 });
      if (event.key === "ArrowRight") lane.scrollBy({ left: lane.clientWidth * .65 });
      if (event.key === "ArrowUp") lane.scrollBy({ top: -lane.clientHeight * .65 });
      if (event.key === "ArrowDown") lane.scrollBy({ top: lane.clientHeight * .65 });
    };
  }
}

function fitDerivativeRailTerm(term) {
  const card = term.closest(".graph-derivative-card");
  if (!card || term.clientWidth <= 1) return;
  const labelNode = card.querySelector(".graph-derivative-label");
  const label = labelNode?.textContent?.trim() || "";
  const note = card.querySelector(".graph-derivative-note");
  const noteText = note?.textContent?.trim() || "";
  const termText = term.textContent.trim();
  card.dataset.derivativeNote = noteText;
  card.title = [termText, noteText].filter(Boolean).join(": ");
  card.setAttribute(
    "aria-label",
    [label, termText, noteText].filter(Boolean).join(": "),
  );

  term.style.removeProperty("font-size");
  term.style.removeProperty("block-size");
  const cardStyle = getComputedStyle(card);
  const innerHeight = card.clientHeight
    - Number.parseFloat(cardStyle.paddingTop || 0)
    - Number.parseFloat(cardStyle.paddingBottom || 0)
    - Number.parseFloat(cardStyle.borderTopWidth || 0)
    - Number.parseFloat(cardStyle.borderBottomWidth || 0);
  const gap = Number.parseFloat(cardStyle.rowGap || cardStyle.gap) || 0;
  const supportingParts = [labelNode, note].filter((part) => (
    part && getComputedStyle(part).display !== "none"
  ));
  const supportingHeight = supportingParts.reduce(
    (height, part) => height + part.getBoundingClientRect().height,
    0,
  );
  const termHeight = Math.max(
    16,
    Math.floor(
      innerHeight
      - supportingHeight
      - gap * Math.max(0, supportingParts.length),
    ),
  );
  term.style.setProperty("block-size", `${termHeight}px`);
  const upper = Number.parseFloat(getComputedStyle(term).fontSize);
  if (!Number.isFinite(upper) || upper <= 0) return;
  const compact = $("#card-view").classList.contains("compact-graph-evidence");
  const minimum = Math.min(upper, compact ? 11 : 13);
  const fits = () => (
    term.scrollWidth <= term.clientWidth + 1
    && term.scrollHeight <= term.clientHeight + 1
  );
  if (fits()) return;

  term.style.setProperty("font-size", `${minimum}px`, "important");
  if (!fits()) return;
  let lower = minimum;
  let higher = upper;
  for (let pass = 0; pass < 11; pass += 1) {
    const candidate = (lower + higher) / 2;
    term.style.setProperty("font-size", `${candidate}px`, "important");
    if (fits()) lower = candidate;
    else higher = candidate;
  }
  term.style.setProperty("font-size", `${lower.toFixed(2)}px`, "important");
}

function refreshKnowledgeTickers() {
  knowledgeTickerFrame = null;
  const cardView = $("#card-view");
  const visibleLexicalMode = [...LEXICAL_TICKER_MODES].some((mode) => (
    cardView.classList.contains(`mode-${mode}`)
  ));
  if (!visibleLexicalMode && !LEXICAL_TICKER_MODES.has(activeCard?.mode)) return;
  const lanes = [
    ...KNOWLEDGE_TICKER_SELECTORS.flatMap((selector) => all(selector)),
    $("#evidence-list"),
  ];
  lanes
    .filter((lane) => lane && !lane.closest(".hidden") && lane.getClientRects().length)
    .forEach(fitKnowledgeTickerLane);
}

function scheduleKnowledgeTickers() {
  if (knowledgeTickerFrame !== null) return;
  knowledgeTickerFrame = window.requestAnimationFrame(refreshKnowledgeTickers);
}

function startKnowledgeTickers() {
  const revision = knowledgeTickerRevision;
  scheduleKnowledgeTickers();
  window.requestAnimationFrame(() => {
    if (revision !== knowledgeTickerRevision) return;
    scheduleKnowledgeTickers();
  });
  if (document.fonts?.ready) {
    document.fonts.ready.then(() => {
      if (revision !== knowledgeTickerRevision) return;
      scheduleKnowledgeTickers();
      scheduleGraphDerivativePagerMeasurement();
    });
  }
}

function fitKnowledgeDefinitionHeight() {
  const cardView = $("#card-view");
  const definition = $("#word-card-definition");
  if (
    !cardView?.classList.contains("mode-knowledge")
    || !definition
    || definition.getClientRects().length === 0
    || definition.clientHeight <= 0
  ) return;

  const fits = () => definition.scrollHeight <= definition.clientHeight + 1;
  if (fits()) return;

  const ceiling = Number.parseFloat(getComputedStyle(definition).fontSize) || 16;
  let lower = Math.min(8, ceiling);
  let upper = ceiling;
  definition.style.fontSize = `${lower}px`;
  if (!fits()) return;

  for (let attempt = 0; attempt < 9; attempt += 1) {
    const candidate = (lower + upper) / 2;
    definition.style.fontSize = `${candidate}px`;
    if (fits()) lower = candidate;
    else upper = candidate;
  }
  definition.style.fontSize = `${Math.floor(lower * 10) / 10}px`;
}

function fitKnowledgeCanvas() {
  window.requestAnimationFrame(fitKnowledgeDefinitionHeight);
  const cardView = $("#card-view");
  const main = cardView.querySelector(".card-main");
  const targets = KNOWLEDGE_AUTOFIT_SELECTORS.flatMap((selector) => all(selector));
  targets.forEach((target) => target.style.removeProperty("font-size"));
  cardView.classList.remove(
    "knowledge-compact-fit",
    "knowledge-space-efficient",
    "knowledge-desktop-height-fit",
  );
  cardView.style.removeProperty("--knowledge-fit-scale");
  cardView.style.removeProperty("--knowledge-fit-width");
  cardView.style.removeProperty("--knowledge-fit-height");
  if (activeCard?.mode !== "knowledge" || !main) return;

  const viewportWidth = cardView.clientWidth;
  const viewportHeight = cardView.clientHeight;
  if (viewportWidth <= 1 || viewportHeight <= 1) return;
  if (document.fullscreenElement || document.body.classList.contains("display-mode")) return;

  const mainRect = main.getBoundingClientRect();
  const outerRegionsOverflow = [
    cardView.querySelector(".word-card-hero"),
    cardView.querySelector(".language-grid"),
  ].filter((region) => region && !region.classList.contains("hidden")).some((region) => {
    const rect = region.getBoundingClientRect();
    return rect.left < mainRect.left - 1
      || rect.right > mainRect.right + 1
      || rect.top < mainRect.top - 1
      || rect.bottom > mainRect.bottom + 1;
  });
  const genuinelyConstrained = viewportWidth < 1200 || viewportHeight < 620;
  if (genuinelyConstrained || outerRegionsOverflow) {
    cardView.classList.add("knowledge-space-efficient");
  }
  if (viewportWidth >= 1200 && viewportHeight < 900) {
    cardView.classList.add("knowledge-desktop-height-fit");
  }
}

let knowledgeCanvasResizeFrame = null;
const knowledgeCanvasResizeObserver = new ResizeObserver(() => {
  if (knowledgeCanvasResizeFrame !== null) {
    window.cancelAnimationFrame(knowledgeCanvasResizeFrame);
  }
  knowledgeCanvasResizeFrame = window.requestAnimationFrame(() => {
    knowledgeCanvasResizeFrame = null;
    if (activeCard?.mode !== "knowledge") return;
    resetKnowledgeTickers();
    fitKnowledgeCanvas();
    startKnowledgeTickers();
  });
});
knowledgeCanvasResizeObserver.observe($("#card-view").parentElement);
if (document.fonts?.ready) {
  document.fonts.ready.then(() => {
    if (activeCard?.mode !== "knowledge") return;
    resetKnowledgeTickers();
    fitKnowledgeCanvas();
    startKnowledgeTickers();
  });
}

function setMorphologyEvidenceExpanded(expanded, userInitiated = false) {
  const cardView = $("#card-view");
  const morphologyMode = ["root", "affix"].includes(activeCard?.mode);
  const graphMode = ["word", "root", "affix"].includes(activeCard?.mode);
  const compactGraphMode = graphMode
    && window.matchMedia("(min-width: 621px) and (max-width: 1100px) and (max-height: 700px)").matches
    && !document.fullscreenElement
    && !document.body.classList.contains("display-mode");
  const toggle = $("#evidence-toggle");
  if (userInitiated && compactGraphMode) {
    cardView.dataset.compactEvidenceChoice = expanded ? "expanded" : "collapsed";
  }
  const compactExpanded = cardView.dataset.compactEvidenceChoice === "expanded";
  const effectiveExpanded = compactGraphMode ? compactExpanded : morphologyMode && expanded;
  cardView.classList.toggle("compact-graph-evidence", compactGraphMode);
  cardView.classList.toggle("evidence-expanded", effectiveExpanded);
  toggle.hidden = !(morphologyMode || compactGraphMode);
  toggle.setAttribute("aria-expanded", String(effectiveExpanded));
  const action = effectiveExpanded ? "Collapse evidence sidebar" : "Expand evidence sidebar";
  toggle.setAttribute("aria-label", action);
  toggle.title = action;
  toggle.textContent = "";
}

function toggleMorphologyEvidencePanel() {
  const cardView = $("#card-view");
  if (
    !["root", "affix"].includes(activeCard?.mode)
    && !cardView.classList.contains("compact-graph-evidence")
  ) return;
  resetKnowledgeTickers();
  setMorphologyEvidenceExpanded(!cardView.classList.contains("evidence-expanded"), true);
  refreshKnowledgeTickers();
  window.setTimeout(() => {
    resetKnowledgeTickers();
    refreshKnowledgeTickers();
  }, 80);
  scheduleGraphViewportFit(120);
  scheduleGraphDerivativePagerMeasurement();
}

function syncGraphEvidenceViewport() {
  if (!activeCard) return;
  setMorphologyEvidenceExpanded(["root", "affix"].includes(activeCard.mode));
  syncGraphTranslationDisclosure();
  scheduleGraphViewportFit(120);
}

window.addEventListener("resize", syncGraphEvidenceViewport);
document.addEventListener("fullscreenchange", syncGraphEvidenceViewport);

window.addEventListener("resize", () => {
  resetKnowledgeTickers();
  fitKnowledgeCanvas();
  scheduleKnowledgeTickers();
  scheduleGraphDerivativePagerMeasurement();
});
knowledgeTickerMotion.addEventListener?.("change", scheduleKnowledgeTickers);

function restartTransition(node, className) {
  node.classList.remove(className);
  void node.offsetWidth;
  node.classList.add(className);
}

function showSentenceSlide(requestedIndex) {
  if (!sentenceSlides.length) return;
  sentenceSlideIndex = (requestedIndex + sentenceSlides.length) % sentenceSlides.length;
  syncPageUrl();
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
  if (sentenceSlides.length > 1 && autoplayEnabled && !ambientRouting) {
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

function evidenceMetadata(item) {
  const metadata = [];
  if (item.pages && item.pages.length) metadata.push(pagesLabel(item.pages));
  if (item.section) metadata.push(`Section · ${item.section}`);
  if (item.locator) metadata.push(`Locator · ${item.locator}`);
  return metadata;
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

function normalizeLexicalView(card, view) {
  if (!view || !Array.isArray(view.nodes) || !view.nodes.length) return null;
  const rawNodes = view.nodes;
  const rawEdges = Array.isArray(view.edges) ? view.edges : [];
  const rawNodesById = new Map(rawNodes.map((node) => [
    String(node?.id || node?.entity_id || ""),
    node,
  ]));
  const propertyKinds = new Map();
  rawNodesById.forEach((node, id) => {
    const payload = node?.payload && typeof node.payload === "object" ? node.payload : {};
    const kind = String(node?.kind || payload.kind || node?.type || "")
      .trim()
      .toLocaleLowerCase();
    if (["language", "period"].includes(kind)) propertyKinds.set(id, kind);
  });
  const propertyMetadata = new Map();
  rawEdges.forEach((edge) => {
    const source = String(edge?.source || edge?.source_entity_id || "");
    const target = String(edge?.target || edge?.target_entity_id || "");
    const propertyId = propertyKinds.has(target)
      ? target
      : (propertyKinds.has(source) ? source : "");
    if (!propertyId) return;
    const subjectId = propertyId === target ? source : target;
    if (!subjectId || propertyKinds.has(subjectId)) return;
    const propertyNode = rawNodesById.get(propertyId) || {};
    const payload = propertyNode?.payload && typeof propertyNode.payload === "object"
      ? propertyNode.payload
      : {};
    const value = String(
      propertyNode?.form
      || propertyNode?.text
      || propertyNode?.label
      || payload.form
      || payload.text
      || payload.label
      || payload.value
      || "",
    ).trim();
    if (!value) return;
    const metadata = propertyMetadata.get(subjectId) || {};
    const relationship = String(edge?.relation || edge?.relationship || "")
      .trim()
      .toLocaleLowerCase();
    const propertyKind = relationship.includes("period") ? "period" : propertyKinds.get(propertyId);
    if (!metadata[propertyKind]) metadata[propertyKind] = value;
    propertyMetadata.set(subjectId, metadata);
  });
  const requestedSubject = String(view.subject_entity_id || card.extensions?.knowledge_subject || "");
  const subjectId = rawNodesById.has(requestedSubject)
    ? requestedSubject
    : requestedSubject.replace(/^(term|morpheme|historical-form):/, "");
  const protectedNodeIds = new Set([
    subjectId,
    ...(Array.isArray(view.focus_entity_ids) ? view.focus_entity_ids.map(String) : []),
  ]);
  const derivativeNodeIds = new Set();
  const keepSharedWords = Boolean(view.include_shared_words)
    || ["root", "affix"].includes(card.mode);
  const displayEdges = rawEdges.filter((edge) => {
    const relationship = String(
      edge?.relation || edge?.relationship || "related",
    ).trim().toLocaleLowerCase();
    if (NON_VISUAL_GRAPH_RELATIONS.has(relationship)) return false;
    if (relationship !== "shares-component" || keepSharedWords) return true;
    const endpoints = [
      String(edge?.source || edge?.source_entity_id || ""),
      String(edge?.target || edge?.target_entity_id || ""),
    ];
    let hiddenDerivative = false;
    endpoints.forEach((id) => {
      const node = rawNodesById.get(id);
      const kind = String(node?.kind || node?.payload?.kind || node?.type || "")
        .trim().toLocaleLowerCase();
      // Component-sharing also connects the current word to its own parts.
      // Hide other words, never the subject, explicit focus, or a morpheme.
      if (!protectedNodeIds.has(id) && ["word", "term"].includes(kind)) {
        derivativeNodeIds.add(id);
        hiddenDerivative = true;
      }
    });
    return !hiddenDerivative;
  });
  const componentKinds = new Map();
  displayEdges.forEach((edge) => {
    const properties = edge?.properties && typeof edge.properties === "object"
      ? edge.properties
      : {};
    const target = String(edge?.target || edge?.target_entity_id || "");
    if (target && properties.component_kind) {
      componentKinds.set(target, String(properties.component_kind));
    }
  });
  const nodes = rawNodes.map((node) => {
    const payload = node?.payload && typeof node.payload === "object" ? node.payload : {};
    const id = String(node?.id || node?.entity_id || "");
    const metadata = propertyMetadata.get(id) || {};
    let type = String(node?.kind || payload.kind || node?.type || "related");
    if (type === "term") type = "word";
    if (type === "historical-form") type = "historical";
    if (type === "morpheme") type = componentKinds.get(id) || "related";
    const evidenceIds = Array.isArray(node?.evidence_ids)
      ? node.evidence_ids.map(String)
      : (Array.isArray(payload.evidence_ids) ? payload.evidence_ids.map(String) : []);
    return {
      ...node,
      id,
      type,
      form: String(node?.form || node?.text || payload.form || payload.text || node?.label || ""),
      meaning: String(node?.meaning || node?.definition || payload.meaning || payload.definition || ""),
      language: String(node?.language || payload.language || metadata.language || ""),
      period: String(node?.period || payload.period_label || payload.period || metadata.period || ""),
      basis: String(node?.basis || payload.basis || (evidenceIds.length ? "book" : "model")),
      evidence_ids: evidenceIds,
    };
  }).filter((node) => (
    node.id
    && !derivativeNodeIds.has(node.id)
    && !propertyKinds.has(node.id)
  ));
  if (!nodes.length) return null;
  const ids = new Set(nodes.map((node) => node.id));
  const edges = displayEdges.map((edge, index) => ({
    ...edge,
    id: String(edge?.id || edge?.assertion_id || `lexical-edge-${index}`),
    source: String(edge?.source || edge?.source_entity_id || ""),
    target: String(edge?.target || edge?.target_entity_id || ""),
    relationship: String(edge?.relation || edge?.relationship || "related"),
    basis: String(edge?.basis || "model"),
    evidence_ids: Array.isArray(edge?.evidence_ids) ? edge.evidence_ids.map(String) : [],
  })).filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const requestedFocus = Array.isArray(view.focus_entity_ids)
    ? view.focus_entity_ids.map(String).filter((id) => ids.has(id))
    : [];
  const focusIds = requestedFocus.length
    ? requestedFocus
    : [subjectId].filter((id) => ids.has(id));
  const centerId = focusIds[0] || (ids.has(subjectId) ? subjectId : nodes[0].id);
  const overview = {
    id: "canonical-overview",
    label: "Whole lexical graph",
    kind: "overview",
    node_ids: nodes.map((node) => node.id),
    headline: card.title,
    explanation: "Canonical accepted knowledge at this saved graph revision.",
  };
  const focused = focusIds.map((focusId, index) => {
    const focusNode = nodes.find((node) => node.id === focusId) || nodes[0];
    const neighborIds = edges.flatMap((edge) => {
      if (edge.source === focusId) return [edge.target];
      if (edge.target === focusId) return [edge.source];
      return [];
    });
    return {
      id: `canonical-focus-${index + 1}`,
      label: focusNode.form || focusNode.label || card.title,
      kind: ["root", "prefix", "suffix"].includes(focusNode.type)
        ? focusNode.type
        : (card.mode === "root" ? "root" : card.mode === "affix" ? "prefix" : "overview"),
      node_ids: [...new Set([focusId, ...neighborIds])],
      headline: focusNode.form || focusNode.label || card.title,
      explanation: focusNode.meaning || "Accepted canonical relationships.",
    };
  });
  return {
    ...view,
    center_id: centerId,
    nodes,
    edges,
    focus_areas: ["root", "affix"].includes(card.mode)
      ? [...focused, overview]
      : [overview, ...focused.filter((area) => area.kind !== "overview")],
  };
}

function unifiedGraph(card) {
  const lexical = normalizeLexicalView(card, card.extensions?.lexical_view);
  if (lexical) return lexical;
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
    { selector: "node.book", style: { "background-color": "#ff5d45", "border-style": "solid" } },
    { selector: "node.model", style: { "border-style": "dashed", opacity: .72 } },
    { selector: "node.center", style: { width: 23, height: 23, "background-color": "#ffcf3d", "border-color": "#17213d", "border-width": 2 } },
    { selector: "node.focus-node", style: { width: 23, height: 23, "border-color": "#17213d", "border-width": 3 } },
    { selector: "edge", style: { width: 1.5, "curve-style": "straight", "line-color": "#9aa7bf", "target-arrow-color": "#9aa7bf", "target-arrow-shape": "triangle", "arrow-scale": .55 } },
    { selector: "edge.parallel", style: { "curve-style": "unbundled-bezier", "control-point-distances": "data(routeOffset)", "control-point-weights": .5 } },
    { selector: "edge.self-edge", style: { "curve-style": "bezier", "loop-direction": "-45deg", "loop-sweep": "70deg" } },
    { selector: "edge.book", style: { "line-style": "solid", opacity: .9 } },
    { selector: "edge.model", style: { "line-style": "dashed", opacity: .42 } },
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
        "font-size": 15,
        "font-weight": 700,
        "text-wrap": "wrap",
        "text-overflow-wrap": "anywhere",
        "text-max-width": 128,
        "text-valign": "center",
        "text-halign": "center",
        "transition-property": "opacity, border-width, width, height",
        "transition-duration": "250ms",
      },
    },
    { selector: "node[fontSize]", style: { "font-size": "data(fontSize)" } },
    { selector: "node[labelWidth]", style: { "text-max-width": "data(labelWidth)" } },
    { selector: "node.book", style: { "background-color": "#fff0e8", "border-color": "#ff5d45", "border-style": "solid" } },
    { selector: "node.model", style: { "border-style": "dashed", "border-color": "#7f8ba3", "background-opacity": .86 } },
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
        "curve-style": "straight",
        "line-color": "#71809e",
        "target-arrow-color": "#71809e",
        "target-arrow-shape": "triangle",
        "arrow-scale": .82,
      },
    },
    {
      selector: "edge.parallel",
      style: {
        "curve-style": "unbundled-bezier",
        "control-point-distances": "data(routeOffset)",
        "control-point-weights": .5,
      },
    },
    { selector: "edge.self-edge", style: { "curve-style": "bezier", "loop-direction": "-45deg", "loop-sweep": "70deg" } },
    { selector: "edge.book", style: { "line-style": "solid", opacity: .9 } },
    { selector: "edge.model", style: { "line-style": "dashed", opacity: .5, "target-arrow-fill": "hollow" } },
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

function graphPeriodLabel(value) {
  const period = String(value || "").trim().replace(/\s+/g, " ");
  if (!period) return "";
  return period.length <= 18
    ? period.toLocaleUpperCase()
    : `${period.slice(0, 17).trimEnd()}...`;
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
  const termFontSize = isCenter ? 24 : 20;
  const meaningFontSize = isCenter ? 17 : 15;
  const minimumWidth = isCenter ? 300 : 240;
  const maximumWidth = isCenter ? 480 : 420;
  const contentLength = [...`${term} ${meaning}`].length;
  const naturalTermWidth = graphTextWidth(term, termFontSize, 950) + (isCenter ? 64 : 42);
  const densityWidth = minimumWidth + Math.sqrt(Math.max(0, contentLength - 24)) * 12;
  const nodeWidth = Math.round(Math.max(
    minimumWidth,
    Math.min(maximumWidth, Math.max(naturalTermWidth, densityWidth)),
  ));
  const contentWidth = nodeWidth - (isCenter ? 52 : 36);
  const termLines = graphTextLines(term, contentWidth, termFontSize, 950);
  const meaningLines = graphTextLines(meaning, contentWidth, meaningFontSize, 650);
  const copyHeight = termLines * termFontSize * 1.08
    + (meaningLines ? 7 + meaningLines * meaningFontSize * 1.22 : 0);
  const nodeHeight = Math.ceil(Math.min(
    isCenter ? 290 : 240,
    Math.max(isCenter ? 164 : 132, 42 + copyHeight + 18),
  ));
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
  $("#graph-overview")?.classList.remove("overlaps-graph");
}

function syncGraphOverviewClearance(badges) {
  const overview = $("#graph-overview");
  if (!overview) return;
  // Visibility preserves the minimap's bounds, so a subsequent pan or slide
  // can restore it without moving the graph or causing a hide/show feedback loop.
  const bounds = overview.getBoundingClientRect();
  const clearance = 8;
  const overlaps = bounds.width > 1 && bounds.height > 1 && badges.some((badge) => {
    const box = badge.getBoundingClientRect();
    return box.width > 1 && box.height > 1
      && box.left < bounds.right + clearance
      && box.right > bounds.left - clearance
      && box.top < bounds.bottom + clearance
      && box.bottom > bounds.top - clearance;
  });
  overview.classList.toggle("overlaps-graph", overlaps);
}

function graphNodeCopyFits(copy) {
  if (
    copy.scrollWidth > copy.clientWidth + 1
    || copy.scrollHeight > copy.clientHeight + 1
  ) return false;
  return Array.from(
    copy.querySelectorAll(".graph-node-term, .graph-node-meaning"),
  ).every((part) => (
    part.scrollWidth <= part.clientWidth + 1
    && part.scrollHeight <= part.clientHeight + 1
  ));
}

function fitGraphNodeCopy(box) {
  const copy = box.querySelector(".graph-node-copy");
  const term = box.querySelector(".graph-node-term");
  const meaning = box.querySelector(".graph-node-meaning");
  if (!copy || !term || copy.clientWidth <= 1 || copy.clientHeight <= 1) return;

  const fitPart = (part, property, upperLimit) => {
    if (!part || part.clientWidth <= 1 || part.clientHeight <= 1) return 0;
    let lower = 1;
    let upper = upperLimit;
    for (let pass = 0; pass < 11; pass += 1) {
      const candidate = (lower + upper) / 2;
      box.style.setProperty(property, `${candidate}px`);
      const fits = part.scrollWidth <= part.clientWidth + 1
        && part.scrollHeight <= part.clientHeight + 1;
      if (fits) lower = candidate;
      else upper = candidate;
    }
    box.style.setProperty(property, `${lower}px`);
    return lower;
  };

  const termSize = fitPart(term, "--graph-term-size", 160);
  const nodeScale = Number.parseFloat(box.style.getPropertyValue("--graph-node-scale")) || 1;
  const minimumGap = Math.max(1.5, 2.5 * nodeScale);
  const meaningLimit = meaning
    ? Math.max(1, Math.min(120, termSize / 1.4, termSize - minimumGap))
    : 0;
  const meaningSize = meaning
    ? fitPart(meaning, "--graph-meaning-size", meaningLimit)
    : 0;
  if (graphNodeCopyFits(copy)) return;

  let lower = .08;
  let upper = 1;
  for (let pass = 0; pass < 12; pass += 1) {
    const candidate = (lower + upper) / 2;
    box.style.setProperty("--graph-term-size", `${termSize * candidate}px`);
    if (meaning) {
      box.style.setProperty("--graph-meaning-size", `${meaningSize * candidate}px`);
    }
    if (graphNodeCopyFits(copy)) lower = candidate;
    else upper = candidate;
  }
  box.style.setProperty("--graph-term-size", `${termSize * lower}px`);
  if (meaning) box.style.setProperty("--graph-meaning-size", `${meaningSize * lower}px`);
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
  const graphRect = layer.getBoundingClientRect();
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
    const hasMeaning = Boolean(String(node.data("meaning") || "").trim());
    const meaningPending = type === "word" && !hasMeaning;
    const languageValue = String(node.data("language") || "").trim();
    const periodValue = String(node.data("period") || "").trim();
    const language = graphLanguageCode(languageValue);
    const period = graphPeriodLabel(periodValue);
    const metadataBadge = [...new Set([language, period].filter(Boolean))].join(" · ");
    const safeLanguage = language.toLocaleLowerCase().replace(/[^a-z0-9-]/g, "");
    const box = element(
      "span",
      `graph-node-badge-box type-${type}${safeLanguage ? ` lang-${safeLanguage}` : ""}${node.hasClass("center") ? " center" : ""}${meaningPending ? " meaning-pending" : ""}`,
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
    box.dataset.graphTermSize = String(node.data("termFontSize") * scale);
    box.dataset.graphMeaningSize = String(node.data("meaningFontSize") * scale);
    box.style.setProperty("--graph-copy-top", `${32 * scale}px`);
    box.style.setProperty("--graph-copy-side", `${10 * scale}px`);
    box.style.setProperty("--graph-copy-bottom", `${(meaningPending ? 22 : 10) * scale}px`);
    box.style.setProperty("--graph-copy-gap", `${7 * scale}px`);
    box.style.setProperty("--graph-badge-top", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-side", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-space", `${9 * scale}px`);
    box.style.setProperty("--graph-badge-radius", `${8 * scale}px`);
    box.style.setProperty("--graph-badge-padding-y", `${3 * scale}px`);
    box.style.setProperty("--graph-badge-padding-x", `${6 * scale}px`);
    box.style.setProperty("--graph-badge-size", `${10 * scale}px`);
    box.style.setProperty("--graph-pending-size", `${8 * scale}px`);
    box.append(element("i", "graph-node-type", type.toLocaleUpperCase()));
    if (metadataBadge) {
      const metadata = element("i", "graph-node-language", metadataBadge);
      metadata.title = [languageValue, periodValue].filter(Boolean).join(" · ");
      box.append(metadata);
    }
    const copy = element(
      "span",
      `graph-node-copy${hasMeaning ? " has-meaning" : ""}`,
    );
    const term = element("strong", "graph-node-term", node.data("form") || "—");
    term.dir = "auto";
    copy.append(term);
    if (hasMeaning) {
      const meaning = element("span", "graph-node-meaning", node.data("meaning"));
      meaning.dir = "auto";
      copy.append(meaning);
    }
    box.append(copy);
    if (meaningPending) {
      const pending = element("small", "graph-node-meaning-pending", "meaning pending");
      pending.title = "Grounded meaning has not been accepted yet";
      box.append(pending);
    }
    badges.push(box);
  });
  layer.replaceChildren(...badges);
  badges.forEach(fitGraphNodeCopy);
  syncGraphOverviewClearance(badges);
  if (performance.now() < (requestGraphOverlayFit.settleUntil || 0)) {
    requestGraphOverlayFit.requested = true;
    scheduleGraphOverlayMeasurement();
  }
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

function syncGraphTranslationDisclosure() {
  const container = $("#graph-focus-annotations");
  if (!container) return;
  let disclosure = $("#graph-translations-disclosure");
  if (!disclosure) {
    disclosure = element("details", "graph-translations-disclosure");
    disclosure.id = "graph-translations-disclosure";
    const summary = element("summary", "", "Translations");
    summary.setAttribute("aria-controls", container.id);
    container.before(disclosure);
    disclosure.append(summary, container);
    disclosure.addEventListener("toggle", () => {
      if (window.innerWidth <= 620) {
        disclosure.dataset.mobileOpen = String(disclosure.open);
      }
      scheduleGraphViewportFit(0);
    });
  }
  const cardKey = String(activeCard?.card_id || activeCardId || activeCard?.query || "");
  if (disclosure.dataset.cardKey !== cardKey) {
    disclosure.dataset.cardKey = cardKey;
    disclosure.dataset.mobileOpen = "false";
  }
  disclosure.classList.toggle("hidden", container.classList.contains("hidden"));
  disclosure.open = window.innerWidth > 620 || disclosure.dataset.mobileOpen === "true";
}

function renderGraphFocusAnnotations(focus) {
  const container = $("#graph-focus-annotations");
  const explicit = focus?.annotations && typeof focus.annotations === "object"
    ? focus.annotations
    : null;
  const annotations = new Map();
  const addAnnotation = (code, term, meaning) => {
    const normalized = normalizeLanguageCode(code);
    if (!normalized || !term) return;
    const current = annotations.get(normalized) || {};
    annotations.set(normalized, {
      term: term || current.term || "",
      meaning: meaning || current.meaning || "",
    });
  };
  addAnnotation("ja", activeCard?.japanese?.term, activeCard?.japanese?.meaning);
  addAnnotation("zh", activeCard?.chinese?.simplified, activeCard?.chinese?.meaning);
  extraLanguageEntries(activeCard).forEach(([code, value]) => {
    addAnnotation(code, value.term, value.meaning);
  });
  Object.entries(explicit || {}).forEach(([key, rawValue]) => {
    const value = typeof rawValue === "string" ? { term: rawValue } : rawValue;
    if (!value || typeof value !== "object") return;
    const code = [value.language_code, value.code, key, value.language]
      .map(normalizeLanguageCode)
      .find(Boolean);
    if (code) addAnnotation(code, value.term, value.meaning);
  });
  registerDiscoveredLanguages([...annotations.keys()]);
  const values = [...annotations.entries()].filter(([code, value]) => (
    code !== "en" && value.term && languageEnabled(code, activeCard?.mode)
  ));
  container.replaceChildren(...values.map(([code, value]) => {
    const safeCode = code.replace(/[^a-z0-9-]/g, "");
    const item = element("span", `graph-focus-annotation language-${safeCode}`);
    item.append(element("small", "", code.toLocaleUpperCase()));
    const term = element("b", "", value.term);
    term.dir = "auto";
    if (languageBase(code) === "ar") {
      term.replaceChildren();
      term.dir = "rtl";
      appendArabicLetters(term, value.term);
    }
    item.append(term);
    if (value.meaning) {
      const explanation = element("em", "", value.meaning);
      explanation.dir = "auto";
      item.append(explanation);
    }
    return item;
  }));
  container.classList.toggle("hidden", values.length === 0);
  syncGraphTranslationDisclosure();
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

function scheduleGraphOverlayMeasurement() {
  window.cancelAnimationFrame(requestGraphOverlayFit.frame || 0);
  requestGraphOverlayFit.frame = window.requestAnimationFrame(() => {
    requestGraphOverlayFit.frame = window.requestAnimationFrame(fitGraphOverlayBounds);
  });
}

function requestGraphOverlayFit() {
  requestGraphOverlayFit.requested = true;
  requestGraphOverlayFit.pass = 0;
  requestGraphOverlayFit.settleUntil = performance.now() + 2500;
  scheduleGraphNodeBadges();
  scheduleGraphOverlayMeasurement();
}

function fitGraphOverlayBounds() {
  requestGraphOverlayFit.frame = null;
  if (
    !requestGraphOverlayFit.requested
    || !originCy
    || $("#origin-graph").classList.contains("hidden")
  ) return;
  const canvas = $("#origin-canvas");
  const canvasRect = canvas.getBoundingClientRect();
  const visibleBoxes = [
    ...document.querySelectorAll("#graph-node-badges .graph-node-badge-box"),
    ...document.querySelectorAll("#graph-node-badges .graph-node-copy"),
  ].filter((node) => {
    const rect = node.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1 && getComputedStyle(node).display !== "none";
  });
  if (!visibleBoxes.length || canvasRect.width <= 1 || canvasRect.height <= 1) {
    requestGraphOverlayFit.requested = false;
    return;
  }

  const bounds = visibleBoxes.reduce((box, node) => {
    const rect = node.getBoundingClientRect();
    box.left = Math.min(box.left, rect.left);
    box.right = Math.max(box.right, rect.right);
    box.top = Math.min(box.top, rect.top);
    box.bottom = Math.max(box.bottom, rect.bottom);
    return box;
  }, { left: Infinity, right: -Infinity, top: Infinity, bottom: -Infinity });
  const padding = Math.max(
    10,
    Math.min(18, Math.round(Math.min(canvasRect.width, canvasRect.height) * .045)),
  );
  const safe = {
    left: canvasRect.left + padding,
    right: canvasRect.right - padding,
    top: canvasRect.top + padding,
    bottom: canvasRect.bottom - padding,
  };
  const overflow = {
    left: Math.max(0, safe.left - bounds.left),
    right: Math.max(0, bounds.right - safe.right),
    top: Math.max(0, safe.top - bounds.top),
    bottom: Math.max(0, bounds.bottom - safe.bottom),
  };
  canvas.dataset.overlayOverflow = [
    overflow.left,
    overflow.right,
    overflow.top,
    overflow.bottom,
  ].map((value) => Math.round(value * 10) / 10).join(",");
  if (Object.values(overflow).every((value) => value <= 1)) {
    requestGraphOverlayFit.requested = false;
    return;
  }
  if ((requestGraphOverlayFit.pass || 0) >= 6) {
    requestGraphOverlayFit.requested = false;
    return;
  }

  const boundsWidth = Math.max(1, bounds.right - bounds.left);
  const boundsHeight = Math.max(1, bounds.bottom - bounds.top);
  const safeWidth = Math.max(1, safe.right - safe.left);
  const safeHeight = Math.max(1, safe.bottom - safe.top);
  const currentZoom = originCy.zoom();
  const scale = Math.min(1, safeWidth / boundsWidth, safeHeight / boundsHeight);
  const readableFloor = Math.max(originCy.minZoom(), Math.min(currentZoom, .28));
  const nextZoom = Math.max(
    readableFloor,
    currentZoom * (scale < 1 ? scale * .985 : 1),
  );
  const currentPan = originCy.pan();
  const boundsCenter = {
    x: (bounds.left + bounds.right) / 2 - canvasRect.left,
    y: (bounds.top + bounds.bottom) / 2 - canvasRect.top,
  };
  const modelCenter = {
    x: (boundsCenter.x - currentPan.x) / currentZoom,
    y: (boundsCenter.y - currentPan.y) / currentZoom,
  };
  const safeCenter = {
    x: (safe.left + safe.right) / 2 - canvasRect.left,
    y: (safe.top + safe.bottom) / 2 - canvasRect.top,
  };
  requestGraphOverlayFit.pass = (requestGraphOverlayFit.pass || 0) + 1;
  canvas.dataset.overlayFitPasses = String(requestGraphOverlayFit.pass);
  originCy.stop();
  originCy.viewport({
    zoom: nextZoom,
    pan: {
      x: safeCenter.x - modelCenter.x * nextZoom,
      y: safeCenter.y - modelCenter.y * nextZoom,
    },
  });
  scheduleGraphNodeBadges();
  scheduleGraphOverlayMeasurement();
}

if (document.fonts?.ready) {
  document.fonts.ready.then(requestGraphOverlayFit);
}

function fitGraphView({ wholeGraph = false, animate = false } = {}) {
  if (!originCy || $("#origin-graph").classList.contains("hidden")) return;
  originCy.resize();
  const target = wholeGraph ? originCy.elements() : visibleGraphElements();
  if (!target?.length) return;
  const canvas = $("#origin-canvas");
  const shortestSide = Math.min(canvas.clientWidth, canvas.clientHeight);
  const compactMorphology = ["root", "affix"].includes(activeCard?.mode)
    && window.innerWidth <= 620;
  const padding = compactMorphology
    ? Math.max(12, Math.min(24, Math.round(shortestSide * .04)))
    : Math.max(24, Math.min(72, Math.round(shortestSide * (wholeGraph ? .055 : .09))));
  const targetNodes = target.nodes();
  const bounds = targetNodes.reduce((box, node) => {
    const position = node.position();
    const width = Number(node.data("nodeWidth")) || node.width();
    const height = Number(node.data("nodeHeight")) || node.height();
    box.left = Math.min(box.left, position.x - width / 2);
    box.right = Math.max(box.right, position.x + width / 2);
    box.top = Math.min(box.top, position.y - height / 2);
    box.bottom = Math.max(box.bottom, position.y + height / 2);
    return box;
  }, { left: Infinity, right: -Infinity, top: Infinity, bottom: -Infinity });
  const boundsWidth = Math.max(1, bounds.right - bounds.left);
  const boundsHeight = Math.max(1, bounds.bottom - bounds.top);
  const availableWidth = Math.max(1, canvas.clientWidth - padding * 2);
  const availableHeight = Math.max(1, canvas.clientHeight - padding * 2);
  const maximumZoom = wholeGraph ? 1.28 : 1.9;
  const zoom = Math.max(originCy.minZoom(), Math.min(
    maximumZoom,
    availableWidth / boundsWidth,
    availableHeight / boundsHeight,
  ));
  const pan = {
    x: canvas.clientWidth / 2 - ((bounds.left + bounds.right) / 2) * zoom,
    y: canvas.clientHeight / 2 - ((bounds.top + bounds.bottom) / 2) * zoom,
  };
  originCy.stop();
  if (animate) {
    originCy.animate({ zoom, pan, duration: 350 });
  } else {
    originCy.viewport({ zoom, pan });
  }
  overviewCy?.resize();
  overviewCy?.fit(overviewCy.elements(), 9);
  requestGraphOverlayFit();
}

function scheduleGraphViewportFit(delay = 90) {
  window.clearTimeout(graphViewportFitTimer);
  graphViewportFitTimer = window.setTimeout(() => {
    const focus = graphFocusAreas[graphFocusIndex];
    layoutGraphForCurrentFocus(focus);
    fitGraphView({ wholeGraph: focus?.kind === "overview" });
  }, delay);
}

function resetGraphAutofit() {
  if (!originCy || !graphFocusAreas.length) return;
  layoutGraphForCanvas();
  const overviewIndex = Math.max(0, graphFocusAreas.findIndex((area) => area.kind === "overview"));
  showGraphFocus(overviewIndex, false);
  fitGraphView({ wholeGraph: true, animate: true });
}

function morphologyGraphSlideLabel(focus) {
  const fallback = String(
    focus.headline
    || focus.label
    || activeCard?.english?.term
    || activeCard?.title
    || (activeCard?.mode === "root" ? "Root" : "Affix"),
  ).trim();
  if (!originCy || !["root", "affix"].includes(activeCard?.mode)) return fallback;

  const expectedTypes = activeCard.mode === "root"
    ? new Set(["root"])
    : new Set(["prefix", "suffix", "affix"]);
  const candidates = [];
  const centerNode = originCy.nodes(".center").first();
  if (centerNode.length) candidates.push(centerNode);
  const focusNode = originCy.$id(String(focus.focus_node_id || "")).first();
  if (focusNode.length && !candidates.some((node) => node.id() === focusNode.id())) {
    candidates.push(focusNode);
  }
  originCy.nodes().forEach((node) => {
    if (
      expectedTypes.has(String(node.data("type") || "").toLowerCase())
      && !candidates.some((candidate) => candidate.id() === node.id())
    ) candidates.push(node);
  });
  const preferred = candidates.find((node) => (
    expectedTypes.has(String(node.data("type") || "").toLowerCase())
    && String(node.data("form") || node.data("label") || "").trim()
  )) || candidates.find((node) => (
    String(node.data("form") || node.data("label") || "").trim()
  ));
  return String(preferred?.data("form") || preferred?.data("label") || fallback).trim() || fallback;
}

const COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT = 900;
let compactGraphTypographyTimer = null;

function fitCompactGraphFocusHeadline() {
  const headline = $("#graph-focus-headline");
  if (!headline) return;
  headline.style.removeProperty("font-size");
  if (window.innerWidth > COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT || !headline.clientWidth) return;

  const maximum = Number.parseFloat(getComputedStyle(headline).fontSize) || 44;
  let lower = Math.min(12, maximum);
  let upper = maximum;
  headline.style.fontSize = `${upper}px`;
  if (headline.scrollWidth <= headline.clientWidth + 1) return;

  for (let attempt = 0; attempt < 9; attempt += 1) {
    const candidate = (lower + upper) / 2;
    headline.style.fontSize = `${candidate}px`;
    if (headline.scrollWidth <= headline.clientWidth + 1) lower = candidate;
    else upper = candidate;
  }
  headline.style.fontSize = `${Math.max(10, lower - .35)}px`;
}

function restoreGraphNodeTypography(nodes) {
  nodes.forEach((node) => {
    const baseline = node.scratch("_lktCompactTypographyBaseline");
    if (baseline) node.style(baseline);
  });
}

function graphNodeTypographyIssues(nodes, graph) {
  const boxes = nodes.map((node) => node.renderedBoundingBox({
    includeLabels: true,
    includeOverlays: false,
  }));
  let overlaps = 0;
  for (let left = 0; left < boxes.length; left += 1) {
    for (let right = left + 1; right < boxes.length; right += 1) {
      const a = boxes[left];
      const b = boxes[right];
      if (a.x1 < b.x2 - 2 && a.x2 > b.x1 + 2 && a.y1 < b.y2 - 2 && a.y2 > b.y1 + 2) {
        overlaps += 1;
      }
    }
  }
  const clipped = boxes.filter((box) => (
    box.x1 < -1 || box.y1 < -1 || box.x2 > graph.width() + 1 || box.y2 > graph.height() + 1
  )).length;
  return { overlaps, clipped };
}

function applyCompactGraphNodeTypography() {
  const graph = originCy;
  if (!graph) return;
  const allNodes = graph.nodes();
  allNodes.forEach((node) => {
    if (!node.scratch("_lktCompactTypographyBaseline")) {
      node.scratch("_lktCompactTypographyBaseline", {
        "font-size": node.style("font-size"),
        width: node.style("width"),
        height: node.style("height"),
        "text-max-width": node.style("text-max-width"),
      });
    }
  });
  restoreGraphNodeTypography(allNodes);
  if (window.innerWidth > COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT) return;

  const visibleNodes = allNodes.filter((node) => node.visible() && !node.hasClass("dimmed"));
  if (!visibleNodes.length) return;
  const baselineIssues = graphNodeTypographyIssues(visibleNodes, graph);
  const factors = [1.32, 1.24, 1.16, 1.08];
  for (const factor of factors) {
    visibleNodes.forEach((node) => {
      const baseline = node.scratch("_lktCompactTypographyBaseline");
      const fontSize = Number.parseFloat(baseline["font-size"]);
      const width = Number.parseFloat(baseline.width);
      const height = Number.parseFloat(baseline.height);
      const textWidth = Number.parseFloat(baseline["text-max-width"]);
      const style = {};
      if (Number.isFinite(fontSize)) style["font-size"] = `${fontSize * factor}px`;
      if (Number.isFinite(width)) style.width = `${width * Math.min(factor, 1.2)}px`;
      if (Number.isFinite(height)) style.height = `${height * Math.min(factor, 1.2)}px`;
      if (Number.isFinite(textWidth)) style["text-max-width"] = `${textWidth * Math.min(factor, 1.16)}px`;
      node.style(style);
    });
    const issues = graphNodeTypographyIssues(visibleNodes, graph);
    if (issues.overlaps <= baselineIssues.overlaps && issues.clipped <= baselineIssues.clipped) return;
    restoreGraphNodeTypography(visibleNodes);
  }
}

function scheduleCompactGraphTypography() {
  fitCompactGraphFocusHeadline();
  window.clearTimeout(compactGraphTypographyTimer);
  compactGraphTypographyTimer = window.setTimeout(() => {
    fitCompactGraphFocusHeadline();
    applyCompactGraphNodeTypography();
  }, 420);
}

window.addEventListener("resize", scheduleCompactGraphTypography, { passive: true });

function showGraphFocus(requestedIndex, animate = true) {
  if (!originCy || !graphFocusAreas.length) return;
  graphFocusIndex = (requestedIndex + graphFocusAreas.length) % graphFocusAreas.length;
  const focus = graphFocusAreas[graphFocusIndex];
  const ids = new Set(focus.node_ids || []);
  const requestedEdgeIds = new Set(focus.edge_ids || []);
  originCy.elements().removeClass("dimmed focus-node");
  originCy.elements().style("display", "element");
  overviewCy?.nodes().removeClass("focus-node");
  const focusNodes = originCy.nodes().filter((node) => ids.has(node.id()));
  const focusEdges = originCy.edges().filter((edge) => (
    ids.has(edge.source().id())
    && ids.has(edge.target().id())
    && (!requestedEdgeIds.size || requestedEdgeIds.has(edge.id()))
  ));
  if (focus.kind !== "overview") {
    originCy.nodes().not(focusNodes).addClass("dimmed");
    originCy.edges().not(focusEdges).addClass("dimmed");
    if (focus.branch_slide) {
      originCy.nodes().not(focusNodes).style("display", "none");
      originCy.edges().not(focusEdges).style("display", "none");
    }
    focusNodes.addClass("focus-node");
    overviewCy?.nodes().filter((node) => ids.has(node.id())).addClass("focus-node");
  }
  const morphologyMode = ["root", "affix"].includes(activeCard?.mode);
  const morphologyLabel = morphologyGraphSlideLabel(focus);
  const displayedHeadline = morphologyMode
    ? `${morphologyLabel} · ${graphFocusIndex + 1}/${graphFocusAreas.length}`
    : (focus.headline || focus.label);
  const headline = $("#graph-focus-headline");
  queueMicrotask(scheduleCompactGraphTypography);
  headline.textContent = displayedHeadline;
  headline.title = displayedHeadline;
  headline.classList.toggle("morphology-slide-heading", morphologyMode);
  const morphologySubject = morphologyMode && focus.kind === "overview"
    ? originCy.nodes().filter((node) => (
      node.data("form") === morphologyLabel
      && ["root", "prefix", "suffix", "affix"].includes(node.data("type"))
    )).first()
    : null;
  optionalText("#graph-focus-explanation", morphologySubject?.length
    ? morphologySubject.data("meaning")
    : focus.explanation);
  renderGraphFocusAnnotations(focus);
  layoutGraphForCurrentFocus(focus);
  fitGraphView({ wholeGraph: focus.kind === "overview", animate });
  scheduleCompactWordOriginReadability();
  const positionPrefix = focus.branch_slide ? "BRANCH" : focus.kind === "overview" ? "OVERVIEW" : "FOCUS";
  text("#graph-focus-position", `${positionPrefix} ${graphFocusIndex + 1} / ${graphFocusAreas.length}`);
  all("#graph-focus-dots button").forEach((button, index) => {
    button.classList.toggle("active", index === graphFocusIndex);
  });
  scheduleGraphNodeBadges();
  scheduleGraphDerivativePagerMeasurement();
  syncPageUrl();
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

function wordOriginGraphPositions(data, canvasWidth = 1280) {
  const nodes = Array.isArray(data.nodes) ? data.nodes : [];
  const stableIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const metrics = new Map(nodes.map((node) => [
    node.id,
    graphNodeMetrics(node, node.id === data.center_id),
  ]));
  const chainNodes = nodes.filter((node) => (
    node.id === data.center_id || node.type === "historical"
  ));
  const chainIds = new Set(chainNodes.map((node) => node.id));
  const indegree = new Map(chainNodes.map((node) => [node.id, 0]));
  const outgoing = new Map(chainNodes.map((node) => [node.id, []]));
  (data.edges || []).forEach((edge) => {
    if (!chainIds.has(edge.source) || !chainIds.has(edge.target) || edge.source === edge.target) return;
    if (outgoing.get(edge.source).includes(edge.target)) return;
    outgoing.get(edge.source).push(edge.target);
    indegree.set(edge.target, indegree.get(edge.target) + 1);
  });
  const ready = chainNodes
    .filter((node) => indegree.get(node.id) === 0)
    .sort((left, right) => stableIndex.get(left.id) - stableIndex.get(right.id));
  const orderedChain = [];
  while (ready.length) {
    const node = ready.shift();
    orderedChain.push(node);
    outgoing.get(node.id).forEach((targetId) => {
      indegree.set(targetId, indegree.get(targetId) - 1);
      if (indegree.get(targetId) !== 0) return;
      ready.push(nodes.find((candidate) => candidate.id === targetId));
      ready.sort((left, right) => stableIndex.get(left.id) - stableIndex.get(right.id));
    });
  }
  chainNodes.forEach((node) => {
    if (!orderedChain.includes(node)) orderedChain.push(node);
  });
  if (!orderedChain.length && nodes.length) orderedChain.push(nodes[0]);

  const columns = Math.max(
    1,
    Math.min(5, orderedChain.length, Math.max(2, Math.floor(canvasWidth / 330))),
  );
  const rows = [];
  for (let start = 0; start < orderedChain.length; start += columns) {
    rows.push(orderedChain.slice(start, start + columns));
  }
  const columnGap = Math.max(64, Math.min(110, canvasWidth * .075));
  const rowGap = 120;
  const rowHeights = rows.map((row) => Math.max(...row.map((node) => metrics.get(node.id).nodeHeight)));
  const totalHeight = rowHeights.reduce((sum, height) => sum + height, 0)
    + Math.max(0, rows.length - 1) * rowGap;
  const positions = new Map();
  let rowTop = -totalHeight / 2;
  rows.forEach((sourceRow, rowIndex) => {
    const row = rowIndex % 2 ? [...sourceRow].reverse() : sourceRow;
    const rowWidth = row.reduce((sum, node) => sum + metrics.get(node.id).nodeWidth, 0)
      + Math.max(0, row.length - 1) * columnGap;
    let left = -rowWidth / 2;
    row.forEach((node) => {
      const nodeMetrics = metrics.get(node.id);
      positions.set(node.id, {
        x: left + nodeMetrics.nodeWidth / 2,
        y: rowTop + rowHeights[rowIndex] / 2,
      });
      left += nodeMetrics.nodeWidth + columnGap;
    });
    rowTop += rowHeights[rowIndex] + rowGap;
  });

  const chainOrder = new Map(orderedChain.map((node, index) => [node.id, index]));
  const branchCounts = new Map();
  nodes.filter((node) => !chainIds.has(node.id)).forEach((node, index) => {
    const connectedChainIds = (data.edges || []).flatMap((edge) => {
      if (edge.source === node.id && chainIds.has(edge.target)) return [edge.target];
      if (edge.target === node.id && chainIds.has(edge.source)) return [edge.source];
      return [];
    });
    const anchorId = connectedChainIds.sort((left, right) => (
      chainOrder.get(left) - chainOrder.get(right)
    ))[0] || orderedChain[index % orderedChain.length]?.id;
    const anchorPosition = positions.get(anchorId) || { x: 0, y: 0 };
    const anchorMetrics = metrics.get(anchorId) || { nodeHeight: 164 };
    const component = ["prefix", "root", "suffix"].includes(node.type);
    const side = component ? -1 : 1;
    const countKey = `${anchorId}:${side}`;
    const branchIndex = branchCounts.get(countKey) || 0;
    branchCounts.set(countKey, branchIndex + 1);
    const nodeMetrics = metrics.get(node.id);
    const tier = Math.floor(branchIndex / 3);
    const lane = branchIndex % 3;
    positions.set(node.id, {
      x: anchorPosition.x + (lane - 1) * (nodeMetrics.nodeWidth * .72 + 58),
      y: anchorPosition.y + side * (
        (anchorMetrics.nodeHeight + nodeMetrics.nodeHeight) / 2 + 92 + tier * (nodeMetrics.nodeHeight + 64)
      ),
    });
  });
  return positions;
}

function layoutGraphForCanvas() {
  if (!originCy || !originGraphData) return;
  const graphNodes = originCy.nodes().map((node) => node);
  const canvas = $("#origin-canvas");
  if (
    activeCard?.mode === "word"
    && $("#card-view").classList.contains("compact-graph-evidence")
    && graphNodes.length >= 4
    && graphNodes.length <= 6
  ) {
    const center = graphNodes.find((node) => node.id() === originGraphData.center_id);
    const satellites = clockwiseGraphOrder(graphNodes, originGraphData.center_id);
    const slots = satellites.length === 3
      ? [{ x: -300, y: -180 }, { x: 300, y: -180 }, { x: 0, y: 180 }]
      : satellites.length === 4
        ? [{ x: -300, y: -180 }, { x: 300, y: -180 }, { x: -300, y: 180 }, { x: 300, y: 180 }]
        : [
            { x: -320, y: -180 }, { x: 0, y: -180 }, { x: 320, y: -180 },
            { x: -210, y: 180 }, { x: 210, y: 180 },
          ];
    center?.position({ x: 0, y: 0 });
    satellites.forEach((node, index) => node.position(slots[index] || { x: 0, y: 180 }));
  } else if (activeCard?.mode === "word") {
    const positions = wordOriginGraphPositions(
      originGraphData,
      canvas.clientWidth,
    );
    originCy.nodes().positions((node) => positions.get(node.id()));
  } else if (["root", "affix"].includes(activeCard?.mode) && graphNodes.length >= 3) {
    layoutFocusedGraphForCanvas({
      node_ids: graphNodes.map((node) => node.id()),
      focus_node_id: originGraphData.center_id,
    });
  } else if (graphNodes.length >= 7) {
    layoutClockwiseGraphNodes(graphNodes, originGraphData.center_id);
  } else {
    const positions = semanticGraphPositions(originGraphData, $("#origin-canvas").clientWidth);
    originCy.nodes().positions((node) => positions.get(node.id()));
  }
  repelGraphNodes(originCy, originGraphData.center_id);
  if (overviewCy) {
    overviewCy.nodes().positions((node) => originCy.$id(node.id()).position());
    overviewCy.fit(overviewCy.elements(), 9);
  }
}

function layoutFocusedGraphForCanvas(focus) {
  if (!originCy || !focus) return;
  const requestedIds = Array.isArray(focus.node_ids) ? focus.node_ids : [];
  const nodes = requestedIds
    .map((id) => originCy.$id(id))
    .filter((node) => node.length && !node.hasClass("dimmed"));
  if (!nodes.length) return;
  if (nodes.length === 1) {
    nodes[0].position({ x: 0, y: 0 });
    return;
  }

  if (["root", "affix"].includes(activeCard?.mode) && nodes.length >= 3) {
    const focusId = nodes.some((node) => node.id() === String(focus.focus_node_id || ""))
      ? String(focus.focus_node_id)
      : (nodes.some((node) => node.id() === originGraphData?.center_id)
          ? originGraphData.center_id
          : nodes[0].id());
    const nodeIds = new Set(nodes.map((node) => node.id()));
    const visibleEdges = originCy.edges().filter((edge) => (
      !edge.hasClass("dimmed")
      && edge.style("display") !== "none"
      && nodeIds.has(edge.source().id())
      && nodeIds.has(edge.target().id())
    ));
    const neighbors = new Map(nodes.map((node) => [node.id(), []]));
    visibleEdges.forEach((edge) => {
      neighbors.get(edge.source().id()).push(edge.target().id());
      neighbors.get(edge.target().id()).push(edge.source().id());
    });
    const stableSort = (items) => [...items].sort((left, right) => (
      String(left.data("form") || left.id()).localeCompare(
        String(right.data("form") || right.id()),
        undefined,
        { sensitivity: "base" },
      ) || left.id().localeCompare(right.id())
    ));
    const histories = stableSort(nodes.filter((node) => node.hasClass("historical")));
    const components = stableSort(nodes.filter((node) => (
      node.hasClass("prefix") || node.hasClass("root") || node.hasClass("suffix")
    )));
    const words = stableSort(nodes.filter((node) => node.hasClass("word")));
    const assignedIds = new Set([...histories, ...components, ...words].map((node) => node.id()));
    const related = stableSort(nodes.filter((node) => !assignedIds.has(node.id())));
    const centeredComponents = components.filter((node) => node.id() !== focusId);
    const focusNode = components.find((node) => node.id() === focusId);
    if (focusNode) centeredComponents.splice(Math.ceil(centeredComponents.length / 2), 0, focusNode);
    const componentOrder = new Map(centeredComponents.map((node, index) => [node.id(), index]));
    words.sort((left, right) => {
      const barycenter = (node) => {
        const indexes = (neighbors.get(node.id()) || [])
          .map((id) => componentOrder.get(id))
          .filter(Number.isFinite);
        return indexes.length
          ? indexes.reduce((sum, value) => sum + value, 0) / indexes.length
          : Number.MAX_SAFE_INTEGER;
      };
      return barycenter(left) - barycenter(right) || left.id().localeCompare(right.id());
    });
    const semanticRows = [histories, centeredComponents, words, related].filter((row) => row.length);
    const canvas = $("#origin-canvas");
    const portrait = window.innerWidth <= 620
      || (canvas.clientWidth < 520 && canvas.clientHeight > canvas.clientWidth);
    const rows = portrait
      ? semanticRows.flatMap((row) => row.map((node) => [node]))
      : semanticRows;
    const gapX = 48;
    const gapY = portrait ? 24 : 40;
    const rowHeights = rows.map((row) => Math.max(...row.map((node) => node.data("nodeHeight"))));
    const totalHeight = rowHeights.reduce((sum, height) => sum + height, 0)
      + gapY * Math.max(0, rows.length - 1);
    let rowTop = -totalHeight / 2;
    rows.forEach((row, rowIndex) => {
      const rowWidth = row.reduce((sum, node) => sum + node.data("nodeWidth"), 0)
        + gapX * Math.max(0, row.length - 1);
      let left = -rowWidth / 2;
      row.forEach((node) => {
        const width = node.data("nodeWidth");
        node.position({ x: left + width / 2, y: rowTop + rowHeights[rowIndex] / 2 });
        left += width + gapX;
      });
      rowTop += rowHeights[rowIndex] + gapY;
    });
    repelGraphNodes(originCy, focusId, 80, nodes);
    return;
  }

  const visibleCenterId = nodes.some((node) => node.id() === originGraphData?.center_id)
    ? originGraphData.center_id
    : null;
  if (nodes.length >= 4 && visibleCenterId) {
    layoutClockwiseGraphNodes(nodes, visibleCenterId);
    repelGraphNodes(originCy, visibleCenterId, 160, nodes);
    return;
  }

  const canvas = $("#origin-canvas");
  const aspect = Math.max(.8, canvas.clientWidth / Math.max(1, canvas.clientHeight));
  const portrait = ["root", "affix"].includes(activeCard?.mode)
    && (window.innerWidth <= 620
      || (canvas.clientWidth < 520 && canvas.clientHeight > canvas.clientWidth));
  const columns = portrait ? 1 : nodes.length <= 2
    ? nodes.length
    : nodes.length === 3
      ? 2
    : Math.min(nodes.length, Math.max(2, Math.ceil(Math.sqrt(nodes.length * aspect))));
  const gapX = 96;
  const gapY = portrait ? 24 : 82;
  const rows = [];
  for (let start = 0; start < nodes.length; start += columns) {
    rows.push(nodes.slice(start, start + columns));
  }
  const rowHeights = rows.map((row) => Math.max(...row.map((node) => node.data("nodeHeight"))));
  const totalHeight = rowHeights.reduce((sum, height) => sum + height, 0)
    + gapY * Math.max(0, rows.length - 1);
  let rowTop = -totalHeight / 2;
  rows.forEach((row, rowIndex) => {
    const rowWidth = row.reduce((sum, node) => sum + node.data("nodeWidth"), 0)
      + gapX * Math.max(0, row.length - 1);
    let left = -rowWidth / 2;
    row.forEach((node) => {
      const width = node.data("nodeWidth");
      node.position({
        x: left + width / 2,
        y: rowTop + rowHeights[rowIndex] / 2,
      });
      left += width + gapX;
    });
    rowTop += rowHeights[rowIndex] + gapY;
  });
}

function clockwiseGraphOrder(nodes, centerId) {
  const nodeById = new Map(nodes.map((node) => [node.id(), node]));
  const stableIndex = new Map(nodes.map((node, index) => [node.id(), index]));
  const satellites = nodes.filter((node) => node.id() !== centerId);
  const satelliteIds = new Set(satellites.map((node) => node.id()));
  const indegree = new Map(satellites.map((node) => [node.id(), 0]));
  const outgoing = new Map(satellites.map((node) => [node.id(), []]));
  originCy.edges().forEach((edge) => {
    const source = edge.source().id();
    const target = edge.target().id();
    if (!satelliteIds.has(source) || !satelliteIds.has(target)) return;
    outgoing.get(source).push(target);
    indegree.set(target, indegree.get(target) + 1);
  });
  const ready = satellites
    .filter((node) => indegree.get(node.id()) === 0)
    .map((node) => node.id());
  const orderedIds = [];
  while (ready.length) {
    ready.sort((left, right) => stableIndex.get(left) - stableIndex.get(right));
    const id = ready.shift();
    orderedIds.push(id);
    outgoing.get(id).forEach((target) => {
      indegree.set(target, indegree.get(target) - 1);
      if (indegree.get(target) === 0) ready.push(target);
    });
  }
  satellites.forEach((node) => {
    if (!orderedIds.includes(node.id())) orderedIds.push(node.id());
  });
  return orderedIds.map((id) => nodeById.get(id));
}

function layoutClockwiseGraphNodes(nodes, centerId) {
  const center = nodes.find((node) => node.id() === centerId);
  if (!center) return;
  center.position({ x: 0, y: 0 });
  const satellites = clockwiseGraphOrder(nodes, centerId);
  if (!satellites.length) return;
  const canvas = $("#origin-canvas");
  const aspect = Math.max(1.25, Math.min(2.05, canvas.clientWidth / Math.max(1, canvas.clientHeight)));
  const widest = Math.max(center.data("nodeWidth"), ...satellites.map((node) => node.data("nodeWidth")));
  const tallest = Math.max(center.data("nodeHeight"), ...satellites.map((node) => node.data("nodeHeight")));
  const baseHeight = Math.max(650, tallest * 3.7, satellites.length * 92);
  const radiusY = Math.max(230, (baseHeight - tallest) / 2);
  const radiusX = Math.max(390, (baseHeight * aspect - widest) / 2);
  const startAngle = -Math.PI * .75;
  satellites.forEach((node, index) => {
    const angle = startAngle + (Math.PI * 2 * index) / satellites.length;
    node.position({
      x: Math.cos(angle) * radiusX,
      y: Math.sin(angle) * radiusY,
    });
  });
}

function layoutGraphForCurrentFocus(focus = graphFocusAreas[graphFocusIndex]) {
  if (!focus || focus.kind === "overview") {
    layoutGraphForCanvas();
    schedulePostLayoutGraphFit(focus);
    return;
  }
  layoutFocusedGraphForCanvas(focus);
  schedulePostLayoutGraphFit(focus);
}

let postLayoutGraphFitFrame = null;

function schedulePostLayoutGraphFit(focus) {
  if (postLayoutGraphFitFrame !== null) {
    window.cancelAnimationFrame(postLayoutGraphFitFrame);
  }
  const graph = originCy;
  let attempts = 0;
  const fitWhenReady = () => {
    postLayoutGraphFitFrame = null;
    if (!graph || originCy !== graph || $("#origin-graph").classList.contains("hidden")) return;
    const canvas = $("#origin-canvas");
    if ((canvas.clientWidth <= 1 || canvas.clientHeight <= 1) && attempts < 3) {
      attempts += 1;
      postLayoutGraphFitFrame = window.requestAnimationFrame(fitWhenReady);
      return;
    }
    fitGraphView({ wholeGraph: !focus || focus.kind === "overview" });
  };
  postLayoutGraphFitFrame = window.requestAnimationFrame(fitWhenReady);
}

function repelGraphNodes(cy, centerId, iterations = 160, selectedNodes = null) {
  const nodes = (selectedNodes || cy.nodes()).map((node) => node);
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

const GRAPH_DERIVATIVE_LIMIT = 24;

const GRAPH_DERIVATIVE_MIN_WIDTH = 180;
const GRAPH_DERIVATIVE_MIN_HEIGHT = 58;
const GRAPH_DERIVATIVE_PAGE_GAP = 8;
const GRAPH_DERIVATIVE_PAGE_INTERVAL = 5200;
const GRAPH_DERIVATIVE_PAGE_FADE = 160;

function clearGraphDerivativePagerMotion() {
  if (graphDerivativePager.timer !== null) window.clearTimeout(graphDerivativePager.timer);
  if (graphDerivativePager.transition !== null) window.clearTimeout(graphDerivativePager.transition);
  if (graphDerivativePager.frame !== null) window.cancelAnimationFrame(graphDerivativePager.frame);
  graphDerivativePager.timer = null;
  graphDerivativePager.transition = null;
  graphDerivativePager.frame = null;
  graphDerivativePager.revision += 1;
}

function graphDerivativePageCount() {
  if (!graphDerivativePager.capacity) return 0;
  return Math.ceil(graphDerivativePager.items.length / graphDerivativePager.capacity);
}

function fitGraphDerivativePageTerms(page) {
  window.requestAnimationFrame(() => {
    page.querySelectorAll("[data-overflow-role='term']").forEach(fitKnowledgeTickerLane);
  });
}

function scheduleGraphDerivativePageAdvance() {
  if (graphDerivativePager.timer !== null) window.clearTimeout(graphDerivativePager.timer);
  graphDerivativePager.timer = null;
  const pageCount = graphDerivativePageCount();
  if (pageCount <= 1) return;
  const revision = graphDerivativePager.revision;
  graphDerivativePager.timer = window.setTimeout(() => {
    graphDerivativePager.timer = null;
    if (revision !== graphDerivativePager.revision) return;
    const rail = $("#graph-derivatives-left");
    const current = rail.querySelector(":scope > .graph-derivative-page");
    const commit = () => {
      if (revision !== graphDerivativePager.revision) return;
      graphDerivativePager.pageIndex = (graphDerivativePager.pageIndex + 1) % pageCount;
      renderGraphDerivativePage(!knowledgeTickerMotion.matches);
    };
    if (!current || knowledgeTickerMotion.matches) {
      commit();
      return;
    }
    current.classList.add("is-leaving");
    graphDerivativePager.transition = window.setTimeout(() => {
      graphDerivativePager.transition = null;
      commit();
    }, GRAPH_DERIVATIVE_PAGE_FADE);
  }, GRAPH_DERIVATIVE_PAGE_INTERVAL);
}

function renderGraphDerivativePage(animateIn = false) {
  const rail = $("#graph-derivatives-left");
  const capacity = graphDerivativePager.capacity;
  const start = graphDerivativePager.pageIndex * capacity;
  const items = graphDerivativePager.items.slice(start, start + capacity);
  const page = element("div", `graph-derivative-page${animateIn ? " is-entering" : ""}`);
  page.dataset.page = String(graphDerivativePager.pageIndex + 1);
  page.replaceChildren(...items.map((item, index) => graphDerivativeCard(item, index)));
  rail.replaceChildren(page);
  fitGraphDerivativePageTerms(page);
  if (animateIn) window.requestAnimationFrame(() => page.classList.remove("is-entering"));
  scheduleGraphDerivativePageAdvance();
}

function collapseGraphDerivativePager() {
  const layout = $("#graph-layout");
  const rails = $("#graph-derivative-rails");
  const left = $("#graph-derivatives-left");
  if (graphDerivativePager.timer !== null) window.clearTimeout(graphDerivativePager.timer);
  if (graphDerivativePager.transition !== null) window.clearTimeout(graphDerivativePager.transition);
  graphDerivativePager.timer = null;
  graphDerivativePager.transition = null;
  graphDerivativePager.capacity = 0;
  left.hidden = true;
  rails.hidden = true;
  layout.classList.remove("has-derivatives", "has-left-derivatives", "has-right-derivatives");
}

function measureGraphDerivativePager() {
  graphDerivativePager.frame = null;
  if (!graphDerivativePager.items.length) return;
  const rail = $("#graph-derivatives-left");
  const sample = rail.querySelector(".graph-derivative-card");
  if (!sample) return;
  const railStyle = getComputedStyle(rail);
  const usableWidth = rail.clientWidth
    - Number.parseFloat(railStyle.paddingLeft || 0)
    - Number.parseFloat(railStyle.paddingRight || 0);
  const usableHeight = rail.clientHeight
    - Number.parseFloat(railStyle.paddingTop || 0)
    - Number.parseFloat(railStyle.paddingBottom || 0);
  const cardHeight = Math.max(
    GRAPH_DERIVATIVE_MIN_HEIGHT,
    Math.ceil(sample.getBoundingClientRect().height),
  );
  const gap = Number.parseFloat(railStyle.rowGap) || GRAPH_DERIVATIVE_PAGE_GAP;
  const capacity = Math.floor((usableHeight + gap) / (cardHeight + gap));
  if (usableWidth < GRAPH_DERIVATIVE_MIN_WIDTH - .5 || capacity < 1) {
    collapseGraphDerivativePager();
    scheduleGraphViewportFit(80);
    return;
  }
  graphDerivativePager.capacity = capacity;
  graphDerivativePager.pageIndex = Math.min(
    graphDerivativePager.pageIndex,
    Math.max(0, graphDerivativePageCount() - 1),
  );
  if (graphDerivativePager.transition !== null) {
    window.clearTimeout(graphDerivativePager.transition);
    graphDerivativePager.transition = null;
  }
  renderGraphDerivativePage(false);
  scheduleGraphViewportFit(80);
}

function scheduleGraphDerivativePagerMeasurement() {
  if (!graphDerivativePager.items.length) return;
  if (graphDerivativePager.frame !== null) window.cancelAnimationFrame(graphDerivativePager.frame);
  const layout = $("#graph-layout");
  const rails = $("#graph-derivative-rails");
  const left = $("#graph-derivatives-left");
  rails.hidden = false;
  left.hidden = false;
  layout.classList.add("has-derivatives", "has-left-derivatives");
  layout.classList.remove("has-right-derivatives");
  graphDerivativePager.frame = window.requestAnimationFrame(() => {
    graphDerivativePager.frame = window.requestAnimationFrame(measureGraphDerivativePager);
  });
}

function normalizedGraphDerivatives(card, data) {
  if (card.mode !== "word") return [];
  const center = (data?.nodes || []).find((node) => node.id === data.center_id);
  const excluded = new Set(
    [center?.form, card.english?.term, card.title, card.query]
      .map((value) => String(value || "").trim().normalize("NFKC").toLocaleLowerCase())
      .filter(Boolean),
  );
  const graphMeanings = new Map();
  for (const node of data?.nodes || []) {
    const term = String(node?.form || node?.label || "").trim();
    const meaning = String(
      node?.meaning
      || node?.definition
      || node?.payload?.meaning
      || node?.payload?.definition
      || "",
    ).trim().replace(/\s+/g, " ");
    const key = term.normalize("NFKC").toLocaleLowerCase();
    if (key && meaning && !graphMeanings.has(key)) graphMeanings.set(key, meaning);
  }
  const seen = new Set(excluded);
  const derivatives = [];
  for (const item of card.related_terms || []) {
    const term = String(typeof item === "string" ? item : item?.term || "").trim();
    const key = term.normalize("NFKC").toLocaleLowerCase();
    if (!term || seen.has(key)) continue;
    seen.add(key);
    derivatives.push({
      term,
      note: String(
        (typeof item === "object" && item ? item.note || "" : "")
        || graphMeanings.get(key)
        || "",
      ).trim().replace(/\s+/g, " "),
    });
    if (derivatives.length >= GRAPH_DERIVATIVE_LIMIT) break;
  }
  return derivatives;
}

function graphDerivativeCard(item, index) {
  const card = element("article", "graph-derivative-card");
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  const accessibleDescription = [item.term, item.note].filter(Boolean).join(": ");
  card.title = accessibleDescription;
  card.setAttribute(
    "aria-label",
    [accessibleDescription, "Open Word Origin"].filter(Boolean).join(": "),
  );
  const term = element("strong", "graph-derivative-term", item.term);
  term.dataset.overflowRole = "term";
  term.dir = "auto";
  card.append(term);
  if (item.note) {
    const note = element("small", "graph-derivative-note", item.note);
    note.dir = "auto";
    card.append(note);
  }
  const open = () => submitQuery(item.term, "word");
  card.addEventListener("click", open);
  card.addEventListener("keydown", (event) => {
    if (!['Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    open();
  });
  return card;
}

let graphRecenterRevision = 0;

async function recenterLexicalGraph(entityId) {
  if (!activeCard || !["word", "root", "affix"].includes(activeCard.mode)) return;
  const revision = ++graphRecenterRevision;
  const expectedCardId = activeCardId;
  const graphMode = activeCard.mode;
  const response = await fetch(
    `/api/graph?entity_id=${encodeURIComponent(entityId)}`
      + `&mode=${encodeURIComponent(graphMode)}&nodes=48&edges=96&depth=4`,
    { cache: "no-store" },
  );
  if (!response.ok || revision !== graphRecenterRevision) return;
  const view = await response.json();
  if (
    revision !== graphRecenterRevision
    || activeCardId !== expectedCardId
    || mode !== graphMode
    || !Array.isArray(view.nodes)
    || !view.nodes.length
  ) return;
  const center = view.nodes.find((node) => String(node.id) === entityId);
  const centeredCard = {
    ...activeCard,
    title: String(center?.label || activeCard.title),
    summary_en: String(
      center?.payload?.meaning
      || center?.payload?.definition
      || activeCard.summary_en,
    ),
    related_terms: [],
    extensions: {
      ...activeCard.extensions,
      lexical_view: {
        ...view,
        subject_entity_id: entityId,
        focus_entity_ids: [entityId],
        include_shared_words: true,
      },
    },
  };
  window.clearInterval(graphFocusTimer);
  recenteredEntityId = entityId;
  recenteredGraphCard = centeredCard;
  renderOriginGraph(centeredCard);
  syncPageUrl();
  scheduleCarousel();
}

function renderGraphDerivativeRails(card, data) {
  const layout = $("#graph-layout");
  const rails = $("#graph-derivative-rails");
  const left = $("#graph-derivatives-left");
  const right = $("#graph-derivatives-right");
  const derivatives = normalizedGraphDerivatives(card, data);
  clearGraphDerivativePagerMotion();
  graphDerivativePager.items = derivatives;
  graphDerivativePager.pageIndex = 0;
  graphDerivativePager.capacity = derivatives.length ? 1 : 0;
  left.replaceChildren(...derivatives.slice(0, 1).map((item) => graphDerivativeCard(item, 0)));
  right.replaceChildren();
  left.hidden = derivatives.length === 0;
  right.hidden = true;
  rails.hidden = derivatives.length === 0;
  layout.classList.toggle("has-derivatives", derivatives.length > 0);
  layout.classList.toggle("has-left-derivatives", derivatives.length > 0);
  layout.classList.remove("has-right-derivatives");
  left.onmouseenter = () => {
    if (graphDerivativePager.timer !== null) window.clearTimeout(graphDerivativePager.timer);
    graphDerivativePager.timer = null;
  };
  left.onmouseleave = scheduleGraphDerivativePageAdvance;
  left.onfocusin = left.onmouseenter;
  left.onfocusout = (event) => {
    if (!left.contains(event.relatedTarget)) scheduleGraphDerivativePageAdvance();
  };
  if (derivatives.length) scheduleGraphDerivativePagerMeasurement();
}

const GRAPH_BRANCH_NODE_LIMIT = 9;
const GRAPH_BRANCH_HISTORY_LIMIT = 3;
const GRAPH_BRANCH_COMPONENT_LIMIT = 3;

function graphEdgeRelation(edge) {
  return String(edge?.relationship || edge?.relation || "related").trim().toLocaleLowerCase();
}

function graphNodeType(node) {
  return String(node?.type || "related").trim().toLocaleLowerCase();
}

function isGraphHistoryNode(node) {
  const type = graphNodeType(node);
  return type.includes("histor") || ["etymon", "origin", "language-form"].includes(type);
}

function isGraphComponentNode(node) {
  const type = graphNodeType(node);
  return type.includes("morpheme") || ["root", "prefix", "suffix", "affix"].includes(type);
}

function isGraphWordNode(node) {
  return ["word", "term", "related", "derivative", "lexeme", "entry"].includes(graphNodeType(node));
}

function morphologyBranchFocusAreas(card, data, validIds) {
  if (!["root", "affix"].includes(card.mode)) return [];
  const nodes = Array.isArray(data?.nodes) ? data.nodes : [];
  const nodeById = new Map(nodes.map((node) => [String(node.id), node]));
  const centerId = validIds.has(String(data.center_id)) ? String(data.center_id) : String(nodes[0]?.id || "");
  const edges = (Array.isArray(data?.edges) ? data.edges : []).map((edge, index) => ({
    ...edge,
    _display_id: String(edge.id || `edge-${index}`),
    _source: String(edge.source || edge.source_entity_id || ""),
    _target: String(edge.target || edge.target_entity_id || ""),
  })).filter((edge) => validIds.has(edge._source) && validIds.has(edge._target));
  const edgeIds = new Set(edges.map((edge) => edge._display_id));
  const adjacency = new Map();
  edges.forEach((edge) => {
    if (!adjacency.has(edge._source)) adjacency.set(edge._source, []);
    if (!adjacency.has(edge._target)) adjacency.set(edge._target, []);
    adjacency.get(edge._source).push({ edge, otherId: edge._target });
    adjacency.get(edge._target).push({ edge, otherId: edge._source });
  });

  const boundedIds = (values) => [...new Set(values.map(String))]
    .filter((id) => validIds.has(id))
    .slice(0, GRAPH_BRANCH_NODE_LIMIT);
  const makeSlide = (raw, index) => {
    const requestedFocusId = String(raw?.focus_node_id || "");
    const requestedNodeIds = Array.isArray(raw?.node_ids) ? raw.node_ids : [];
    const focusId = validIds.has(requestedFocusId)
      ? requestedFocusId
      : String(requestedNodeIds.find((id) => validIds.has(String(id)) && String(id) !== centerId) || centerId);
    const derivativeEdge = (edge, candidateFocusId) => {
      if (!["has-component", "shares-component"].includes(graphEdgeRelation(edge))) {
        return "";
      }
      if (edge._source === candidateFocusId && isGraphWordNode(nodeById.get(edge._target))) {
        return edge._target;
      }
      if (edge._target === candidateFocusId && isGraphWordNode(nodeById.get(edge._source))) {
        return edge._source;
      }
      return "";
    };
    let fullDerivativeIds = [...new Set(
      edges.map((edge) => derivativeEdge(edge, focusId)).filter(Boolean),
    )];
    if (!fullDerivativeIds.length && focusId !== centerId) {
      fullDerivativeIds = [...new Set(
        edges.map((edge) => derivativeEdge(edge, centerId)).filter(Boolean),
      )];
    }
    const requestedNodeSet = new Set(requestedNodeIds.map(String));
    let slideDerivativeIds = fullDerivativeIds.filter((id) => requestedNodeSet.has(id));
    if (!slideDerivativeIds.length && fullDerivativeIds.length) {
      slideDerivativeIds = [fullDerivativeIds[0]];
    }
    const secondaryComponentIds = slideDerivativeIds.flatMap((derivativeId) => (
      (adjacency.get(derivativeId) || [])
        .map(({ otherId }) => otherId)
        .filter((otherId) => otherId !== focusId && isGraphComponentNode(nodeById.get(otherId)))
        .slice(0, 2)
    ));
    let nodeIds = boundedIds([
      centerId,
      focusId,
      ...slideDerivativeIds,
      ...secondaryComponentIds,
      ...requestedNodeIds,
    ]);
    if (!nodeIds.length) return null;
    const nodeIdSet = new Set(nodeIds);
    const inducedEdges = edges.filter((edge) => (
      nodeIdSet.has(edge._source)
      && nodeIdSet.has(edge._target)
    ));
    const reachableIds = new Set([nodeIdSet.has(focusId) ? focusId : nodeIds[0]]);
    let changed = true;
    while (changed) {
      changed = false;
      inducedEdges.forEach((edge) => {
        if (!reachableIds.has(edge._source) && !reachableIds.has(edge._target)) return;
        const previousSize = reachableIds.size;
        reachableIds.add(edge._source);
        reachableIds.add(edge._target);
        changed = changed || reachableIds.size !== previousSize;
      });
    }
    nodeIds = nodeIds.filter((id) => reachableIds.has(id));
    const slideEdgeIds = inducedEdges
      .filter((edge) => reachableIds.has(edge._source) && reachableIds.has(edge._target))
      .map((edge) => edge._display_id);
    const focusNode = nodeById.get(focusId) || nodeById.get(centerId);
    return {
      ...raw,
      id: String(raw?.id || `branch-${index + 1}`),
      label: String(raw?.label || focusNode?.form || `Branch ${index + 1}`),
      kind: "branch",
      branch_slide: true,
      focus_node_id: focusId,
      node_ids: nodeIds,
      edge_ids: [...new Set(slideEdgeIds)],
      headline: String(raw?.headline || raw?.label || focusNode?.form || card.title),
      explanation: String(raw?.explanation || focusNode?.meaning || card.summary_en || "Connected lexical branch."),
    };
  };
  const uniqueSlides = (slides) => {
    const seen = new Set();
    return slides.filter(Boolean).filter((slide) => {
      const signature = `${slide.focus_node_id}:${slide.node_ids.join("|")}`;
      if (seen.has(signature)) return false;
      seen.add(signature);
      return true;
    });
  };

  if (Array.isArray(data.branch_slides) && data.branch_slides.length) {
    return uniqueSlides(data.branch_slides.map(makeSlide));
  }

  const centerNeighbors = adjacency.get(centerId) || [];
  const centerHistoryIds = centerNeighbors
    .filter(({ edge, otherId }) => (
      isGraphHistoryNode(nodeById.get(otherId))
      || /(history|origin|etym|develop|descend|borrow|attest)/.test(graphEdgeRelation(edge))
    ))
    .map(({ otherId }) => otherId)
    .slice(0, GRAPH_BRANCH_HISTORY_LIMIT);
  let derivativeIds = centerNeighbors
    .filter(({ otherId }) => otherId !== centerId && isGraphWordNode(nodeById.get(otherId)))
    .map(({ otherId }) => otherId);
  if (!derivativeIds.length) {
    derivativeIds = nodes
      .filter((node) => String(node.id) !== centerId && isGraphWordNode(node))
      .map((node) => String(node.id));
  }
  const derivedSlides = [...new Set(derivativeIds)].map((derivativeId, index) => {
    const derivativeNeighbors = adjacency.get(derivativeId) || [];
    const componentIds = derivativeNeighbors
      .map(({ otherId }) => otherId)
      .filter((id) => id !== centerId && isGraphComponentNode(nodeById.get(id)))
      .slice(0, GRAPH_BRANCH_COMPONENT_LIMIT);
    const derivativeHistoryIds = derivativeNeighbors
      .map(({ otherId }) => otherId)
      .filter((id) => isGraphHistoryNode(nodeById.get(id)))
      .slice(0, 1);
    return makeSlide({
      id: `derived-branch-${index + 1}`,
      label: nodeById.get(derivativeId)?.form || `Branch ${index + 1}`,
      focus_node_id: derivativeId,
      node_ids: [derivativeId, ...centerHistoryIds, ...componentIds, ...derivativeHistoryIds],
    }, index);
  });
  if (derivedSlides.length) return uniqueSlides(derivedSlides);

  const connectedIds = centerNeighbors.map(({ otherId }) => otherId);
  if (connectedIds.length) {
    return uniqueSlides([makeSlide({
      id: "history-branch",
      label: "History",
      focus_node_id: centerId,
      node_ids: [...centerHistoryIds, ...connectedIds],
    }, 0)]);
  }

  const legacyAreas = (Array.isArray(data.focus_areas) ? data.focus_areas : [])
    .filter((area) => area?.kind !== "overview" && Array.isArray(area?.node_ids));
  if (legacyAreas.length) return uniqueSlides(legacyAreas.map(makeSlide));
  return uniqueSlides([makeSlide({
    id: "current-form",
    label: nodeById.get(centerId)?.form || card.title,
    focus_node_id: centerId,
    node_ids: [centerId],
  }, 0)]);
}

function compactMorphologyPageCapacity() {
  return window.innerHeight < 800 ? 2 : 3;
}

function compactMorphologyBranchSlides(slides, data, maxNodes = 3) {
  const capacity = Math.max(2, Math.min(9, Math.floor(Number(maxNodes) || 3)));
  const nodeById = new Map((data.nodes || []).map((node) => [String(node.id), node]));
  const edges = (data.edges || []).map((edge, index) => ({
    id: String(edge.id || `edge-${index}`),
    source: String(edge.source || edge.source_entity_id || ""),
    target: String(edge.target || edge.target_entity_id || ""),
  }));
  const role = (id) => String(nodeById.get(id)?.type || "").toLowerCase();
  const isComponent = (id) => ["root", "prefix", "suffix", "affix"].includes(role(id));
  const pages = [];
  const signatures = new Set();
  for (const slide of slides) {
    const nodeIds = [...new Set((slide.node_ids || []).map(String))]
      .filter((id) => nodeById.has(id));
    const nodeSet = new Set(nodeIds);
    const requestedEdges = new Set((slide.edge_ids || []).map(String));
    const slideEdges = edges.filter((edge) => nodeSet.has(edge.source)
      && nodeSet.has(edge.target) && (!requestedEdges.size || requestedEdges.has(edge.id)));
    const anchor = [String(data.center_id || ""), String(slide.focus_node_id || ""), ...nodeIds]
      .find((id) => nodeSet.has(id) && isComponent(id))
      || (nodeSet.has(String(slide.focus_node_id)) ? String(slide.focus_node_id) : nodeIds[0]);
    const adjacency = new Map(nodeIds.map((id) => [id, new Set()]));
    slideEdges.forEach((edge) => {
      adjacency.get(edge.source).add(edge.target);
      adjacency.get(edge.target).add(edge.source);
    });
    const coveredNodes = new Set();
    const addPage = (selected) => {
      const ids = [...selected];
      ids.forEach((id) => coveredNodes.add(id));
      const includedEdges = slideEdges.filter((edge) => selected.has(edge.source) && selected.has(edge.target));
      const signature = JSON.stringify([
        [...ids].sort(), includedEdges.map((edge) => edge.id).sort(),
      ]);
      if (!signatures.has(signature)) {
        signatures.add(signature);
        pages.push({
          ...slide,
          id: `compact-morphology-${pages.length + 1}`,
          kind: "branch",
          branch_slide: true,
          compact_morphology_slide: true,
          focus_node_id: selected.has(anchor) ? anchor : ids[0],
          node_ids: ids,
          edge_ids: includedEdges.map((edge) => edge.id),
        });
      }
      return new Set(includedEdges.map((edge) => edge.id));
    };
    const edgeRank = (edge) => {
      if (edge.source !== anchor && edge.target !== anchor) return 2;
      return role(edge.source) === "word" || role(edge.target) === "word" ? 0 : 1;
    };
    let pending = [...slideEdges].sort((left, right) => edgeRank(left) - edgeRank(right));
    while (pending.length) {
      const seed = pending[0];
      const selected = new Set([seed.source, seed.target]);
      while (selected.size < capacity) {
        const candidates = [...new Set([...selected].flatMap((id) => [...adjacency.get(id)]))]
          .filter((id) => !selected.has(id));
        if (!candidates.length) break;
        const hasWord = [...selected].some((id) => role(id) === "word");
        const rank = (id) => id === anchor ? 0
          : !hasWord && role(id) === "word" ? 1
          : role(id).startsWith("histor") ? 2 : 3;
        candidates.sort((left, right) => rank(left) - rank(right)
          || nodeIds.indexOf(left) - nodeIds.indexOf(right));
        selected.add(candidates[0]);
      }
      const coveredEdges = addPage(selected);
      pending = pending.filter((edge) => !coveredEdges.has(edge.id));
    }
    // Preserve genuinely isolated saved nodes without inventing a connecting
    // edge. Connected branches above never acquire a dangling context node.
    nodeIds.filter((id) => !coveredNodes.has(id)).forEach((id) => addPage(new Set([id])));
  }
  return pages;
}

function compactWordOriginFocusAreas(card, overviewArea) {
  if (
    card.mode !== "word"
    || window.innerWidth > COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT
    || !originCy
  ) return [];

  const center = originCy.nodes(".center").first();
  const centerId = String(overviewArea?.focus_node_id || center.id() || "");
  const remaining = originCy.nodes().filter((node) => node.id() !== centerId);
  if (!centerId || !remaining.length) return [];

  // A shallow compact canvas cannot make a whole origin tree readable. Build
  // small topology-preserving pages instead: connected historical stages stay
  // together as edge pairs, while center-connected morphology nodes share a
  // page. Every page has at most three visible nodes including the word.
  const remainingIds = new Set(remaining.map((node) => node.id()));
  const adjacency = new Map([...remainingIds].map((id) => [id, new Set()]));
  originCy.edges().forEach((edge) => {
    const source = edge.source().id();
    const target = edge.target().id();
    if (!remainingIds.has(source) || !remainingIds.has(target)) return;
    adjacency.get(source).add(target);
    adjacency.get(target).add(source);
  });

  const pairedBranches = [];
  const singletonBranches = [];
  const visited = new Set();
  for (const node of remaining) {
    const start = node.id();
    if (visited.has(start)) continue;
    visited.add(start);
    const queue = [start];
    let componentSize = 0;
    const treePairs = [];
    while (queue.length) {
      const current = queue.shift();
      componentSize += 1;
      for (const neighbor of adjacency.get(current) || []) {
        if (visited.has(neighbor)) continue;
        visited.add(neighbor);
        queue.push(neighbor);
        treePairs.push([current, neighbor]);
      }
    }
    if (componentSize === 1) singletonBranches.push(start);
    else pairedBranches.push(...treePairs);
  }

  for (let index = 0; index < singletonBranches.length; index += 2) {
    pairedBranches.push(singletonBranches.slice(index, index + 2));
  }

  const headline = String(card.title || center.data("form") || center.data("label") || "Origin");
  return pairedBranches.map((branchIds, index) => {
    const nodeIds = [centerId, ...new Set(branchIds)].filter(Boolean);
    const nodeIdSet = new Set(nodeIds);
    const edgeIds = originCy.edges()
      .filter((edge) => nodeIdSet.has(edge.source().id()) && nodeIdSet.has(edge.target().id()))
      .map((edge) => edge.id());
    return {
      id: `compact-origin-${index + 1}`,
      label: headline,
      headline,
      kind: "branch",
      branch_slide: true,
      compact_origin_slide: true,
      focus_node_id: branchIds[0] || centerId,
      node_ids: nodeIds,
      edge_ids: edgeIds,
      explanation: card.summary_en || "",
    };
  });
}

let compactWordOriginReadabilityTimer = null;

function enforceCompactWordOriginReadability() {
  const shell = $("#origin-canvas")?.closest(".origin-graph");
  if (
    activeCard?.mode !== "word"
    || window.innerWidth > COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT
    || !shell?.classList.contains("compact-origin-paged")
    || !originCy
  ) return;

  const nodes = originCy.nodes().filter((node) => node.style("display") !== "none");
  if (!nodes.length || nodes.length > 3) return;
  const renderedSizes = nodes.map((node) => Number.parseFloat(node.renderedStyle("font-size")) || 0);
  const minimumRenderedSize = Math.min(...renderedSizes);
  const targetSize = 8.15;
  if (minimumRenderedSize >= targetSize) {
    $("#origin-canvas").dataset.compactOriginMinFont = minimumRenderedSize.toFixed(2);
    return;
  }

  const canvas = $("#origin-canvas");
  const bounds = nodes.boundingBox({ includeLabels: true, includeOverlays: false });
  const padding = 10;
  const safeZoom = Math.min(
    originCy.maxZoom(),
    (canvas.clientWidth - padding * 2) / Math.max(1, bounds.w),
    (canvas.clientHeight - padding * 2) / Math.max(1, bounds.h),
  );
  const desiredZoom = Math.min(
    safeZoom,
    originCy.zoom() * (targetSize / Math.max(.1, minimumRenderedSize)),
  );
  if (desiredZoom > originCy.zoom() + .001) {
    const centerX = (bounds.x1 + bounds.x2) / 2;
    const centerY = (bounds.y1 + bounds.y2) / 2;
    originCy.zoom(desiredZoom);
    originCy.pan({
      x: canvas.clientWidth / 2 - centerX * desiredZoom,
      y: canvas.clientHeight / 2 - centerY * desiredZoom,
    });
    scheduleGraphNodeBadges();
  }
  let achieved = Math.min(
    ...nodes.map((node) => Number.parseFloat(node.renderedStyle("font-size")) || 0),
  );
  // The page split removes the geometric pressure first. If a very long label
  // still forces the safe camera zoom below the floor, raise only the model
  // label size needed to meet it; node boxes and positions remain unchanged,
  // so this cannot introduce body overlap or canvas clipping.
  if (achieved < 8) {
    const minimumModelSize = targetSize / Math.max(.08, originCy.zoom());
    nodes.forEach((node) => {
      const currentModelSize = Number.parseFloat(node.style("font-size")) || 15;
      if (currentModelSize < minimumModelSize) {
        node.style("font-size", `${minimumModelSize}px`);
      }
    });
    scheduleGraphNodeBadges();
    achieved = Math.min(
      ...nodes.map((node) => Number.parseFloat(node.renderedStyle("font-size")) || 0),
    );
  }
  canvas.dataset.compactOriginMinFont = achieved.toFixed(2);
}

function scheduleCompactWordOriginReadability() {
  clearTimeout(compactWordOriginReadabilityTimer);
  compactWordOriginReadabilityTimer = setTimeout(enforceCompactWordOriginReadability, 520);
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
  renderGraphDerivativeRails(card, data);
  const enabled = ["word", "root", "affix"].includes(card.mode)
    && Array.isArray(data?.nodes) && data.nodes.length >= 1;
  graph.classList.toggle("hidden", !enabled);
  if (!enabled) return;
  originGraphData = data;
  text("#graph-kind", card.mode === "word" ? "WORD ORIGIN GRAPH" : `${card.mode.toUpperCase()} GRAPH`);
  if (typeof window.cytoscape !== "function") {
    canvas.append(element("p", "graph-error", "Graph renderer unavailable."));
    return;
  }
  const semanticPositions = card.mode === "word"
    ? wordOriginGraphPositions(data, canvas.clientWidth)
    : semanticGraphPositions(data, canvas.clientWidth);
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
        period: node.period || "",
        ...metrics,
      },
      classes: [
        (node.basis === "book" || node.evidence_ids?.length) ? "book" : "model",
        node.type || "related",
        isCenter ? "center" : "",
      ].filter(Boolean).join(" "),
      position: semanticPositions.get(node.id),
    };
  });
  const ids = new Set(data.nodes.map((node) => node.id));
  const visibleGraphEdges = (data.edges || [])
    .filter((edge) => (
      ids.has(edge.source)
      && ids.has(edge.target)
      && !NON_VISUAL_GRAPH_RELATIONS.has(
        String(edge.relationship || edge.relation || "").trim().toLocaleLowerCase(),
      )
    ));
  const endpointCounts = new Map();
  visibleGraphEdges.forEach((edge) => {
    const key = [String(edge.source), String(edge.target)].sort().join("\u0000");
    endpointCounts.set(key, (endpointCounts.get(key) || 0) + 1);
  });
  const endpointIndexes = new Map();
  const graphEdges = visibleGraphEdges.map((edge, index) => {
      const evidenceIds = Array.isArray(edge.evidence_ids) ? edge.evidence_ids : [];
      const grounded = edge.basis === "book" || evidenceIds.length > 0;
      const endpointKey = [String(edge.source), String(edge.target)].sort().join("\u0000");
      const routeCount = endpointCounts.get(endpointKey) || 1;
      const routeIndex = endpointIndexes.get(endpointKey) || 0;
      endpointIndexes.set(endpointKey, routeIndex + 1);
      const parallel = routeCount > 1;
      const selfEdge = String(edge.source) === String(edge.target);
      return {
        data: {
          id: edge.id || `edge-${index}`,
          source: edge.source,
          target: edge.target,
          relationship: edge.relationship || "related",
          basis: grounded ? "book" : "model",
          routeOffset: parallel ? (routeIndex - (routeCount - 1) / 2) * 42 : 0,
        },
        classes: [grounded ? "book" : "model", parallel ? "parallel" : "", selfEdge ? "self-edge" : ""]
          .filter(Boolean)
          .join(" "),
      };
    });
  originCy = window.cytoscape({
    container: canvas,
    elements: [...graphNodes, ...graphEdges],
    minZoom: .08,
    maxZoom: 3,
    wheelSensitivity: .16,
    boxSelectionEnabled: false,
    autoungrabify: true,
    layout: { name: "preset", fit: true, padding: 24 },
    style: graphStyles(),
  });
  repelGraphNodes(originCy, data.center_id);
  fitGraphView({ wholeGraph: true });
  originCy.on("render", scheduleGraphNodeBadges);
  if (document.fonts?.ready) document.fonts.ready.then(scheduleGraphNodeBadges);
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
  const allIds = data.nodes.map((node) => String(node.id));
  const overviewArea = {
    id: "overview",
    label: "Overview",
    kind: "overview",
    node_ids: allIds,
    headline: card.title,
    explanation: card.summary_en,
  };
  const originalBranchSlides = morphologyBranchFocusAreas(card, data, ids);
  const compactMorphology = window.innerWidth <= 620
    && ["root", "affix"].includes(card.mode) && originalBranchSlides.length > 0;
  const branchSlides = compactMorphology
    ? compactMorphologyBranchSlides(originalBranchSlides, data, compactMorphologyPageCapacity())
    : originalBranchSlides;
  graph.classList.toggle("compact-morphology-paged", Boolean(compactMorphology));
  graph.dataset.compactMorphologyCapacity = compactMorphology
    ? String(compactMorphologyPageCapacity()) : "0";
  const morphologyDense = branchSlides.length > 0 && (
    data.nodes.length > GRAPH_BRANCH_NODE_LIMIT
    || graphEdges.length > GRAPH_BRANCH_NODE_LIMIT + 4
    || canvas.clientWidth < 820
  );
  if (["root", "affix"].includes(card.mode) && branchSlides.length) {
    graphFocusAreas = morphologyDense ? branchSlides : [overviewArea, ...branchSlides];
  } else {
    graphFocusAreas = Array.isArray(data.focus_areas) && data.focus_areas.length
      ? data.focus_areas.map((area, index) => ({
        ...area,
        id: area.id || `focus-${index}`,
        node_ids: (area.node_ids || []).map(String).filter((id) => ids.has(id)),
      }))
      : [overviewArea];
    if (!graphFocusAreas.some((area) => area.kind === "overview")) {
      graphFocusAreas.unshift(overviewArea);
    }
  }
  const compactOriginSlides = compactWordOriginFocusAreas(card, overviewArea);
  if (compactOriginSlides.length) graphFocusAreas = compactOriginSlides;
  const hasBranchSlides = branchSlides.length > 0 || compactOriginSlides.length > 0;
  graph.classList.toggle("compact-origin-paged", compactOriginSlides.length > 0);
  graph.classList.toggle("branch-slides-active", hasBranchSlides);
  const dots = $("#graph-focus-dots");
  dots.replaceChildren(...graphFocusAreas.map((area, index) => {
    const button = element("button");
    button.type = "button";
    button.title = area.label || `Area ${index + 1}`;
    button.setAttribute("aria-label", `Show ${area.branch_slide ? "branch" : "graph focus"}: ${button.title}`);
    button.addEventListener("click", () => {
      window.clearInterval(graphFocusTimer);
      showGraphFocus(index);
      scheduleCarousel();
    });
    return button;
  }));
  $("#previous-graph-focus").disabled = graphFocusAreas.length < 2;
  $("#next-graph-focus").disabled = graphFocusAreas.length < 2;
  $("#previous-graph-focus").title = hasBranchSlides ? "Previous graph branch" : "Previous graph focus";
  $("#next-graph-focus").title = hasBranchSlides ? "Next graph branch" : "Next graph focus";
  $("#graph-focus-dots").classList.toggle("hidden", graphFocusAreas.length < 2);
  originCy.on("tap", "node", (event) => {
    const index = graphFocusAreas.findIndex((area) => (
      area.kind !== "overview" && area.node_ids?.includes(event.target.id())
    ));
    if (index >= 0) showGraphFocus(index);
    void recenterLexicalGraph(event.target.id());
  });
  const ambientFocusIndexes = graphFocusAreas
    .map((area, index) => ({ area, index }))
    .filter(({ area }) => area.kind !== "overview")
    .map(({ index }) => index);
  const initialFocusIndex = ambientRouting && autoplayEnabled && ambientFocusIndexes.length
    ? ambientFocusIndexes[Math.floor(Math.random() * ambientFocusIndexes.length)]
    : 0;
  showGraphFocus(initialFocusIndex, false);
  if (graphFocusAreas.length > 1 && autoplayEnabled && !ambientRouting) {
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

function updateKnowledgeLanguageGrid(card, alternateEnabled = false) {
  const grid = $("#language-grid");
  const count = [
    languageEnabled("ja", card.mode),
    languageEnabled("zh", card.mode),
    alternateEnabled,
  ].filter(Boolean).length;
  [1, 2, 3].forEach((value) => grid.classList.toggle(`visible-languages-${value}`, count === value));
}

function applyCardLanguageVisibility(card) {
  $("#word-card-hero").classList.toggle("hidden", card.mode === "knowledge" && !languageEnabled("en", card.mode));
  $(".japanese-block").classList.toggle("hidden", !languageEnabled("ja", card.mode));
  $(".chinese-block").classList.toggle("hidden", !languageEnabled("zh", card.mode));
}

function showAlternate(card, requestedIndex = alternateIndex) {
  const block = $("#alternate-block");
  const available = availableAlternateLanguages(card);
  const enabled = card.mode === "knowledge" && available.length > 0;
  block.classList.toggle("hidden", !enabled);
  $("#language-grid").classList.toggle("has-alternate", enabled);
  updateKnowledgeLanguageGrid(card, enabled);
  if (!enabled) return;

  alternateIndex = requestedIndex % available.length;
  const [language, value] = available[alternateIndex];
  const metadata = languageMetadata(language);
  block.className = `language-block alternate-block ${metadata.className}-block`;
  block.dir = languageDirection(language);
  block.lang = language;
  text("#alternate-label", metadata.label);
  text("#alternate-term", value.term);
  optionalText("#alternate-reading", value.pronunciation || value.reading);
  optionalText("#alternate-meaning", value.meaning);
  syncPageUrl();
}

function startAlternateLoop(card) {
  window.clearInterval(alternateTimer);
  alternateIndex = 0;
  showAlternate(card, 0);
  const count = availableAlternateLanguages(card).length;
  if (card.mode !== "knowledge" || count < 2 || !autoplayEnabled) return;
  alternateTimer = window.setInterval(() => {
    if (alternateIndex >= count - 1) {
      window.clearInterval(alternateTimer);
      return;
    }
    alternateIndex += 1;
    showAlternate(card, alternateIndex);
    scheduleKnowledgeTickers();
  }, INNER_SLIDE_DWELL_MS);
}

function renderCard(card, refreshHistory = true) {
  graphRecenterRevision += 1;
  recenteredEntityId = "";
  recenteredGraphCard = null;
  if (pinnedPageCardId !== card.card_id) pinnedPageCardId = "";
  resetKnowledgeTickers();
  registerCardLanguages(card);
  setMode(card.mode, true);
  activeCardId = card.card_id;
  activeCard = card;
  const copy = MODE_COPY[card.mode] || MODE_COPY.word;
  const cardView = $("#card-view");
  cardView.className = `card-view mode-${card.mode}`;
  delete cardView.dataset.compactEvidenceChoice;
  setMorphologyEvidenceExpanded(["root", "affix"].includes(card.mode));
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
  applyCardLanguageVisibility(card);
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
  evidence.scrollTop = 0;
  const evidenceTitle = $("#evidence-title");
  text(
    "#evidence-title",
    card.extensions?.source_title
      || card.evidence?.[0]?.source_title
      || SOURCE_TITLES[card.mode]
      || "Local library",
  );
  evidenceTitle.dataset.overflowRole = "term";
  text("#narrative-label", copy.narrative);
  const evidenceItems = Array.isArray(card.evidence) ? card.evidence : [];
  evidence.classList.toggle("evidence-list-multiple", evidenceItems.length > 1);
  evidence.classList.toggle("evidence-list-many", evidenceItems.length > 2);
  evidenceItems.forEach((item) => {
    const section = element("section", "evidence");
    section.append(element("h4", "", item.headword));
    const metadata = evidenceMetadata(item);
    if (metadata.length) {
      const metadataLabel = metadata.join(" · ");
      const metadataElement = element("span", "evidence-meta", metadataLabel);
      metadataElement.title = metadataLabel;
      section.append(metadataElement);
    }
    const excerpt = item.excerpt || "";
    const boundedExcerpt = excerpt.length > 320 ? `${excerpt.slice(0, 317)}…` : excerpt;
    if (boundedExcerpt) section.append(element("blockquote", "", `“${boundedExcerpt}”`));
    evidence.append(section);
  });
  show("card");
  fitKnowledgeCanvas();
  refreshKnowledgeTickers();
  startKnowledgeTickers();
  window.setTimeout(() => {
    stopEvidenceAutoScroll(evidence);
    fitEvidenceAutoScrollLane(evidence);
  }, 160);
  if (["root", "affix"].includes(card.mode)) scheduleGraphViewportFit(120);
  restartTransition(cardView, "card-switch-enter");
  updateCarouselChrome();
  if (refreshHistory) loadHistory();
}

function setMode(nextMode, preserveView = false) {
  const previousMode = mode;
  mode = MODE_COPY[nextMode] ? nextMode : "answer";
  if (previousMode !== mode) historyRequestRevision += 1;
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
  syncSettingsControls();
  syncPageUrl();
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

function cancelCardSubmission() {
  cardSubmissionRevision += 1;
  activeCardSubmissionController?.abort();
  activeCardSubmissionController = null;
}

async function submitQuery(query, requestedMode = mode, context = {}) {
  const submittedQuery = String(query || "").trim();
  if (!submittedQuery) return;
  cancelPageRestore();
  cancelCardSubmission();
  setMode(requestedMode);
  const submittedMode = mode;
  if (submittedMode === "chat") {
    await submitChat(submittedQuery);
    return;
  }
  const submittedContext = { ...context };
  const requestRevision = cardSubmissionRevision;
  const requestController = new AbortController();
  activeCardSubmissionController = requestController;
  $("#query").value = submittedQuery;
  $("#generate-button").disabled = true;
  text("#loading-kicker", "READING LOCALLY");
  text("#loading-title", "Composing the next card…");
  text("#loading-detail", "Qwen is working on this Raspberry Pi. Your text stays here.");
  show("loading");
  try {
    for (let poll = 0; poll < 300; poll += 1) {
      if (requestRevision !== cardSubmissionRevision) return;
      const response = await fetch("/api/cards", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...submittedContext,
          query: submittedQuery,
          mode: submittedMode,
          retry_failed: poll === 0,
        }),
        signal: requestController.signal,
      });
      const payload = await response.json();
      if (requestRevision !== cardSubmissionRevision) return;
      if (response.status === 202 && payload.status === "preparing") {
        text("#loading-kicker", `LOCAL PIPELINE · ${payload.completed_jobs} / ${payload.total_jobs}`);
        text("#loading-title", payload.current_label || "Preparing accepted knowledge");
        text(
          "#loading-detail",
          payload.generation_blocker
            || "Each finished step is saved. You may reconnect without losing progress.",
        );
        await wait(Math.max(1000, Math.min(Number(payload.poll_after_ms) || 3000, 10000)));
        continue;
      }
      if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
      renderCard(payload);
      return;
    }
    throw new Error("Preparation is still running. Reopen this word to resume its saved progress.");
  } catch (error) {
    if (error.name === "AbortError" || requestRevision !== cardSubmissionRevision) return;
    text("#error-message", error.message);
    show("error");
  } finally {
    if (requestRevision === cardSubmissionRevision) {
      activeCardSubmissionController = null;
      $("#generate-button").disabled = false;
    }
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
  if (healthRequestInFlight) return;
  healthRequestInFlight = true;
  const container = $("#system-state");
  try {
    const { payload: health } = await fetchJsonWithTimeout("/api/health", {}, 12000);
    const generation = health.generation || {};
    const ready = generation.ready === true;
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
    text(
      "#state-label",
      ready
        ? `${progress} · Qwen ready`
        : generation.blocker || "Model, corpus, or worker is starting…",
    );
  } catch (_error) {
    text("#state-label", "Terminal unavailable");
  } finally {
    healthRequestInFlight = false;
  }
}

function readPageRoute(href) {
  const parameters = new URL(href).searchParams;
  const requestedMode = parameters.get("mode");
  return {
    mode: Object.hasOwn(MODE_COPY, requestedMode) ? requestedMode : activeAmbientModeOrder()[0],
    hasMode: parameters.has("mode"),
    cardId: (parameters.get("card") || "").trim().slice(0, 100),
    slide: Number(parameters.get("slide")) || 1,
    focusId: (parameters.get("focus") || "").slice(0, 200),
    entityId: (parameters.get("entity") || "").trim().slice(0, 100),
    language: (parameters.get("language") || "").slice(0, 32),
    display: parameters.has("display"),
    loop: parameters.has("loop"),
  };
}

function pageSlideIndex(slide, count) {
  const index = Number.isFinite(slide) ? Math.trunc(slide) - 1 : 0;
  return Math.max(0, Math.min(Math.max(0, count - 1), index));
}

function pageViewUrl(href, state) {
  const url = new URL(href);
  ["mode", "card", "slide", "focus", "entity", "language", "loop"].forEach((key) => {
    url.searchParams.delete(key);
  });
  url.searchParams.set("mode", state.mode);
  if (state.loop) url.searchParams.set("loop", "1");
  if (state.cardId) {
    url.searchParams.set("card", state.cardId);
    if (state.slide) url.searchParams.set("slide", String(state.slide));
    if (state.focusId) url.searchParams.set("focus", state.focusId);
    if (state.entityId) url.searchParams.set("entity", state.entityId);
    if (state.language) url.searchParams.set("language", state.language);
  }
  return url;
}

function syncPageUrl() {
  if (restoringPageRoute || pageUrlSyncQueued) return;
  pageUrlSyncQueued = true;
  queueMicrotask(() => {
    pageUrlSyncQueued = false;
    if (restoringPageRoute) return;
    const card = visibleView === "card" && activeCard?.mode === mode ? activeCard : null;
    const graph = card && ["word", "root", "affix"].includes(mode);
    const sentence = card && ["question", "answer"].includes(mode);
    const alternate = card?.mode === "knowledge"
      ? availableAlternateLanguages(card)[alternateIndex]?.[0]
      : "";
    const url = pageViewUrl(window.location.href, {
      mode,
      cardId: card?.card_id || "",
      slide: graph && graphFocusAreas.length ? graphFocusIndex + 1
        : sentence && sentenceSlides.length ? sentenceSlideIndex + 1 : 0,
      focusId: graph ? graphFocusAreas[graphFocusIndex]?.id : "",
      entityId: graph ? recenteredEntityId : "",
      language: alternate,
      loop: ambientRouting && autoplayEnabled,
    });
    // Autoplay must not create hundreds of browser history entries or reload.
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url);
    }
  });
}

function cancelPageRestore() {
  pageRouteRevision += 1;
  pageRouteController?.abort();
  pageRouteController = null;
  restoringPageRoute = false;
  pinnedPageCardId = "";
  cardHydrationRevision += 1;
  activeCardHydrationController?.abort();
  activeCardHydrationController = null;
  graphRecenterRevision += 1;
  userActivityRevision += 1;
}

async function restorePageFromUrl() {
  const route = readPageRoute(window.location.href);
  cancelPageRestore();
  cancelCardSubmission();
  const revision = pageRouteRevision;
  const controller = new AbortController();
  pageRouteController = controller;
  restoringPageRoute = true;
  historyRequestRevision += 1;
  window.clearTimeout(autoplayTimer);
  [graphFocusTimer, sentenceSlideTimer, alternateTimer].forEach((timer) => {
    window.clearInterval(timer);
  });
  // Shared views stay still; an explicit kiosk URL retains its display loop.
  autoplayEnabled = !route.cardId || route.display || route.loop;
  ambientRouting = autoplayEnabled && (!route.hasMode || route.display || route.loop);
  ambientModeIndex = ambientIndexAfter(route.mode);
  document.body.classList.toggle("display-mode", route.display || Boolean(document.fullscreenElement));
  $("#generate-button").disabled = false;
  setMode(route.mode);
  updateCarouselChrome();
  let opened = false;
  try {
    if (!route.cardId) {
      activeCard = null;
      activeCardId = null;
      show(mode === "chat" ? "chat" : "empty");
      opened = true;
      await loadHistory();
      return;
    }
    text("#loading-kicker", "SAVED VIEW");
    text("#loading-title", "Opening the linked card");
    text("#loading-detail", "Reading saved knowledge; no model generation is requested.");
    show("loading");
    const { response, payload: card } = await fetchJsonWithTimeout(
      "/api/cards/" + encodeURIComponent(route.cardId) + "?revision=1",
      { cache: "no-store", signal: controller.signal },
      20000,
    );
    if (revision !== pageRouteRevision || controller.signal.aborted) return;
    if (!response.ok) throw new Error(card.error || "This saved card is unavailable.");
    if (card.card_id !== route.cardId || !Object.hasOwn(MODE_COPY, card.mode)) {
      throw new Error("The saved link did not return a valid card.");
    }
    updateDeckResponseMetadata(response, card.mode);
    pinnedPageCardId = route.cardId;
    renderCard(rememberHydratedCard(card), false);
    if (route.entityId && ["word", "root", "affix"].includes(mode)) {
      await recenterLexicalGraph(route.entityId);
      if (revision !== pageRouteRevision || controller.signal.aborted) return;
    }
    if (["word", "root", "affix"].includes(mode) && graphFocusAreas.length) {
      const focusIndex = graphFocusAreas.findIndex((area) => area.id === route.focusId);
      showGraphFocus(focusIndex >= 0 ? focusIndex : pageSlideIndex(route.slide, graphFocusAreas.length), false);
    } else if (["question", "answer"].includes(mode) && sentenceSlides.length) {
      showSentenceSlide(pageSlideIndex(route.slide, sentenceSlides.length));
    }
    if (mode === "knowledge" && route.language) {
      const index = availableAlternateLanguages(card).findIndex(([code]) => code === route.language);
      if (index >= 0) {
        showAlternate(card, index);
        scheduleKnowledgeTickers();
      }
    }
    opened = true;
    if (route.display) pinnedPageCardId = "";
    await loadHistory();
    if (revision === pageRouteRevision) scheduleCarousel();
  } catch (error) {
    if (revision !== pageRouteRevision || error.name === "AbortError") return;
    pinnedPageCardId = "";
    text("#error-message", error.message);
    show("error");
  } finally {
    if (revision === pageRouteRevision) {
      restoringPageRoute = false;
      pageRouteController = null;
      // Keep failed URLs intact so they can still be reported.
      if (opened) syncPageUrl();
    }
  }
}

async function loadHistory() {
  const requestedMode = mode;
  const requestRevision = ++historyRequestRevision;
  try {
    if (requestedMode === "chat") return;
    const { cards, generationChanged } = await fetchModeSummaries(requestedMode);
    if (requestRevision !== historyRequestRevision || mode !== requestedMode) return;
    allSavedCards = pinnedPageCardId && activeCard?.card_id === pinnedPageCardId
      && activeCard.mode === requestedMode
      ? [activeCard, ...cards.filter((card) => card.card_id !== pinnedPageCardId)]
      : cards;
    const shouldOpenLatest = !activeCard || activeCard.mode !== requestedMode;
    rebuildModeCarousel(shouldOpenLatest);
  } catch (_error) {
    if (requestRevision !== historyRequestRevision || mode !== requestedMode) return;
    $("#history").replaceChildren(element("p", "quiet", "History unavailable."));
  }
}

function updateDeckResponseMetadata(response, requestedMode) {
  const total = Number.parseInt(response.headers.get("X-Total-Count") || "", 10);
  if (Number.isFinite(total)) modeDeckTotals.set(requestedMode, Math.max(0, total));
  const nextGeneration = response.headers.get("X-Knowledge-Generation") || "";
  const changed = Boolean(
    knowledgeGeneration && nextGeneration && knowledgeGeneration !== nextGeneration,
  );
  if (nextGeneration && nextGeneration !== knowledgeGeneration) {
    knowledgeGeneration = nextGeneration;
    hydratedCardCache.clear();
  }
  return changed;
}

async function fetchModeSummaries(cardMode) {
  const pending = modeSummaryRequests.get(cardMode);
  if (pending) return pending;
  const request = (async () => {
    const parameters = new URLSearchParams({
      mode: cardMode,
      limit: String(HISTORY_PAGE_LIMIT),
      offset: "0",
    });
    const { response, payload: cards } = await fetchJsonWithTimeout(
      `/api/cards?${parameters}`,
      {},
      15000,
    );
    const generationChanged = updateDeckResponseMetadata(response, cardMode);
    if (!response.ok || !Array.isArray(cards)) {
      throw new Error("Card history unavailable");
    }
    const result = { cards, generationChanged };
    modeSummaryCache.set(cardMode, result);
    return result;
  })();
  modeSummaryRequests.set(cardMode, request);
  try {
    return await request;
  } finally {
    if (modeSummaryRequests.get(cardMode) === request) {
      modeSummaryRequests.delete(cardMode);
    }
  }
}

function rememberHydratedCard(card, generation = knowledgeGeneration) {
  if (!card?.card_id) return card;
  card.__lktHydratedGeneration = generation;
  const key = `${card.card_id}:${generation}`;
  hydratedCardCache.delete(key);
  hydratedCardCache.set(key, card);
  while (hydratedCardCache.size > HYDRATED_CARD_CACHE_LIMIT) {
    hydratedCardCache.delete(hydratedCardCache.keys().next().value);
  }
  return card;
}

async function renderSavedCard(card, refreshHistory = false) {
  if (!card?.card_id) return;
  cancelPageRestore();
  if (
    !card.summary_only
    && card.__lktHydratedGeneration
    && card.__lktHydratedGeneration === knowledgeGeneration
  ) {
    renderCard(card, refreshHistory);
    return;
  }
  const expectedMode = card.mode;
  const requestRevision = ++cardHydrationRevision;
  activeCardHydrationController?.abort();
  const controller = new AbortController();
  activeCardHydrationController = controller;
  const cacheKey = `${card.card_id}:${knowledgeGeneration}`;
  let hydrated = hydratedCardCache.get(cacheKey);
  try {
    if (!hydrated) {
      const { response, payload } = await fetchJsonWithTimeout(
        `/api/cards/${encodeURIComponent(card.card_id)}`,
        { cache: "no-store", signal: controller.signal },
        20000,
      );
      updateDeckResponseMetadata(response, expectedMode);
      if (!response.ok) throw new Error(payload.error || "Card unavailable");
      hydrated = rememberHydratedCard(payload);
    }
    if (
      requestRevision !== cardHydrationRevision
      || controller.signal.aborted
      || mode !== expectedMode
    ) return;
    renderCard(hydrated, refreshHistory);
  } catch (error) {
    if (error.name !== "AbortError") console.warn("Saved card hydration failed", error);
  } finally {
    if (requestRevision === cardHydrationRevision) activeCardHydrationController = null;
  }
}

function shuffledModeDeck(cards) {
  const deck = [...cards];
  if (!displaySettings.randomCards) return deck;
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
  if (displaySettings.randomCards) {
    for (let index = deck.length - 1; index > 0; index -= 1) {
      const swapIndex = Math.floor(Math.random() * (index + 1));
      [deck[index], deck[swapIndex]] = [deck[swapIndex], deck[index]];
    }
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
    recentIds: [
      ...(previous?.recentIds || []).filter((id) => id !== cardId),
      cardId,
    ].slice(-12),
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
    recentIds: [
      ...(previous?.recentIds || []).filter((id) => id !== selectedId),
      selectedId,
    ].slice(-12),
  });
  return byId.get(selectedId) || null;
}

async function randomAcceptedCard(cardMode) {
  const parameters = new URLSearchParams({ mode: cardMode });
  const recentIds = ambientModeDecks.get(cardMode)?.recentIds || [];
  if (recentIds.length) parameters.set("exclude", recentIds.join(","));
  const { response, payload: card } = await fetchJsonWithTimeout(
    `/api/cards/random?${parameters}`,
    { cache: "no-store" },
    20000,
  );
  updateDeckResponseMetadata(response, cardMode);
  if (!response.ok || !card?.card_id) throw new Error(card.error || "Card history unavailable");
  return rememberHydratedCard(card);
}

async function hydrateAcceptedSummary(card, cardMode) {
  const cached = hydratedCardCache.get(`${card.card_id}:${knowledgeGeneration}`);
  if (cached) return cached;
  const { response, payload } = await fetchJsonWithTimeout(
    `/api/cards/${encodeURIComponent(card.card_id)}?rotation=1`,
    { cache: "no-store" },
    20000,
  );
  updateDeckResponseMetadata(response, cardMode);
  if (!response.ok || !payload?.card_id) {
    throw new Error(payload.error || "Card unavailable");
  }
  return rememberHydratedCard(payload);
}

async function fetchAmbientMode(cardMode) {
  try {
    const cachedCards = modeSummaryCache.get(cardMode)?.cards || [];
    const queued = takeAmbientCard(cardMode, cachedCards);
    let selected = null;
    if (queued) {
      try {
        selected = await hydrateAcceptedSummary(queued, cardMode);
      } catch (_error) {
        selected = await randomAcceptedCard(cardMode);
      }
    } else {
      selected = await randomAcceptedCard(cardMode);
    }
    const cards = [
      selected,
      ...cachedCards.filter((card) => card.card_id !== selected.card_id),
    ];
    // Refresh summaries after hydration. This preserves new-card priority on
    // the next visit without making a lightweight list delay the current card.
    void fetchModeSummaries(cardMode).catch(() => null);
    return { selected, cards };
  } catch (_error) {
    return null;
  }
}

function prefetchAmbientMode() {
  if (!ambientRouting || !autoplayEnabled) return;
  const enabledModes = activeAmbientModeOrder();
  if (!enabledModes.length) return;
  const nextMode = enabledModes[ambientModeIndex % enabledModes.length];
  if (
    ambientPrefetch?.mode === nextMode
  ) return;
  ambientPrefetch = {
    mode: nextMode,
    promise: fetchAmbientMode(nextMode),
  };
}

async function advanceAmbientMode(activityRevision) {
  const enabledModes = activeAmbientModeOrder();
  if (!enabledModes.length) return;
  // One mode gets one bounded attempt per dwell. Chaining six network
  // timeouts made an unavailable deck look like a frozen kiosk for minutes.
  const nextMode = enabledModes[ambientModeIndex % enabledModes.length];
  ambientModeIndex = (ambientModeIndex + 1) % enabledModes.length;
  const prefetched = (
    ambientPrefetch?.mode === nextMode
  ) ? ambientPrefetch.promise : fetchAmbientMode(nextMode);
  ambientPrefetch = null;
  const prepared = await prefetched;
  if (!prepared) return;
  const { cards, selected } = prepared;
  if (
    !ambientRouting
    || !autoplayEnabled
    || activityRevision !== userActivityRevision
    || !selected
  ) return;
  setMode(nextMode, true);
  allSavedCards = cards;
  carouselCards = shuffledModeDeck(cards);
  carouselIndex = carouselCards.findIndex((card) => card.card_id === selected.card_id);
  if (carouselIndex < 0) {
    carouselCards.unshift(selected);
    carouselIndex = 0;
  } else {
    carouselCards[carouselIndex] = selected;
  }
  renderModeDeckDots();
  rememberAmbientCard(nextMode, carouselCards, selected.card_id);
  renderCard(selected, false);
}

function runAmbientAdvance(activityRevision) {
  void advanceAmbientMode(activityRevision)
    .catch((error) => console.warn("Ambient advance failed", error))
    .finally(() => {
      // The loop owns its watchdog: a failed fetch or render must never leave
      // the kiosk without a future timer.
      if (ambientRouting && autoplayEnabled) scheduleCarousel();
    });
}

function renderModeDeckDots() {
  const history = $("#history");
  history.replaceChildren();
  const halfWindow = Math.floor(HISTORY_DOT_LIMIT / 2);
  const maxStart = Math.max(0, carouselCards.length - HISTORY_DOT_LIMIT);
  const start = Math.min(maxStart, Math.max(0, carouselIndex - halfWindow));
  const visibleCards = carouselCards.slice(start, start + HISTORY_DOT_LIMIT);
  visibleCards.forEach((card, offset) => {
    const index = start + offset;
    const button = element("button");
    button.dataset.carouselIndex = String(index);
    button.title = card.title;
    button.classList.toggle("active", index === carouselIndex);
    button.addEventListener("click", () => {
      carouselIndex = index;
      void renderSavedCard(card, false);
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
  if (openLatest || found < 0) void renderSavedCard(carouselCards[carouselIndex], false);
  if (ambientRouting && carouselIndex >= 0) {
    rememberAmbientCard(mode, carouselCards, carouselCards[carouselIndex].card_id);
  }
  updateCarouselChrome();
  scheduleCarousel();
}

async function syncAcceptedDeck() {
  if (deckSyncInFlight) return;
  if (restoringPageRoute || pinnedPageCardId) return;
  if (mode === "chat" || !["card", "empty"].includes(visibleView)) return;
  if (visibleView === "card" && activeCard?.mode !== mode) return;
  const requestedMode = mode;
  const requestRevision = pageRouteRevision;
  deckSyncInFlight = true;
  try {
    const { cards, generationChanged } = await fetchModeSummaries(requestedMode);
    if (mode !== requestedMode || requestRevision !== pageRouteRevision
      || restoringPageRoute || pinnedPageCardId) return;
    const current = activeCard?.mode === requestedMode
      && cards.some((card) => card.card_id === activeCard.card_id)
      ? activeCard
      : null;
    const knownIds = new Set(carouselCards.map((card) => card.card_id));
    const newlyAccepted = cards.filter((card) => !knownIds.has(card.card_id));
    const previousSummaries = new Map(allSavedCards.map((card) => [
      card.card_id,
      JSON.stringify(card),
    ]));
    const summaryChanged = cards.length !== allSavedCards.length
      || cards.some((card) => previousSummaries.get(card.card_id) !== JSON.stringify(card));
    allSavedCards = cards;
    if (!newlyAccepted.length && !summaryChanged && !generationChanged) return;

    const refreshedDeck = shuffledModeDeck(cards);
    carouselCards = current
      ? [current, ...refreshedDeck.filter((card) => card.card_id !== current.card_id)]
      : refreshedDeck;
    carouselIndex = current ? 0 : -1;
    renderModeDeckDots();
    updateCarouselChrome();
    if (!current && carouselCards.length) {
      carouselIndex = 0;
      void renderSavedCard(carouselCards[0], false);
      scheduleCarousel();
    }
  } catch (_error) {
    // Keep the current accepted deck untouched while the service is unavailable.
  } finally {
    deckSyncInFlight = false;
  }
}

function updateCarouselChrome() {
  const found = carouselCards.findIndex((card) => card.card_id === activeCardId);
  if (found >= 0) carouselIndex = found;
  const total = modeDeckTotals.get(mode) || carouselCards.length;
  text("#carousel-position", carouselCards.length ? `${carouselIndex + 1} / ${total}` : "0 / 0");
  let historyButtons = all("#history button");
  if (
    carouselCards.length
    && !historyButtons.some((button) => Number(button.dataset.carouselIndex) === carouselIndex)
  ) {
    renderModeDeckDots();
    historyButtons = all("#history button");
  }
  historyButtons.forEach((button) => button.classList.toggle(
    "active",
    Number(button.dataset.carouselIndex) === carouselIndex,
  ));
  $("#toggle-autoplay").textContent = autoplayEnabled ? "Ⅱ" : "▶";
  $("#toggle-autoplay").title = autoplayEnabled ? "Pause carousel" : "Play carousel";
}

function navigateCards(step) {
  if (!carouselCards.length || visibleView === "loading") return;
  carouselIndex = (carouselIndex + step + carouselCards.length) % carouselCards.length;
  void renderSavedCard(carouselCards[carouselIndex], false);
  if (ambientRouting) {
    rememberAmbientCard(mode, carouselCards, carouselCards[carouselIndex].card_id);
  }
  scheduleCarousel();
}

async function activateMode(nextMode) {
  cancelPageRestore();
  const resumeAmbient = autoplayEnabled && nextMode !== "chat";
  ambientRouting = resumeAmbient;
  if (nextMode !== mode) {
    cancelCardSubmission();
    $("#generate-button").disabled = false;
  }
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
  if (resumeAmbient && mode !== "chat") {
    ambientRouting = true;
    ambientModeIndex = ambientIndexAfter(mode);
    const visibleCard = carouselCards[carouselIndex];
    if (visibleCard) rememberAmbientCard(mode, carouselCards, visibleCard.card_id);
    scheduleCarousel();
  }
}

async function navigateModes(step) {
  const buttons = all(".mode");
  if (!buttons.length) return;
  const selectedIndex = buttons.findIndex((button) => button.dataset.mode === mode);
  const currentIndex = selectedIndex < 0 ? 0 : selectedIndex;
  const nextIndex = (currentIndex + step + buttons.length) % buttons.length;
  await activateMode(buttons[nextIndex].dataset.mode);
  if (!autoplayEnabled || mode === "chat") return;
  ambientRouting = true;
  ambientModeIndex = ambientIndexAfter(mode);
  const visibleCard = carouselCards[carouselIndex];
  if (visibleCard) rememberAmbientCard(mode, carouselCards, visibleCard.card_id);
  scheduleCarousel();
}

const ARROW_NAVIGATION = {
  ArrowUp: { axis: "mode", step: -1 },
  ArrowDown: { axis: "mode", step: 1 },
  ArrowLeft: { axis: "card", step: -1 },
  ArrowRight: { axis: "card", step: 1 },
};

function isEditingTarget(target) {
  return target instanceof Element && Boolean(
    target.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])"),
  );
}

function hasNavigationModifier(event) {
  return event.altKey || event.ctrlKey || event.metaKey || event.shiftKey;
}

function activeInnerSlideCount() {
  if (["answer", "question"].includes(mode)) return Math.max(1, sentenceSlides.length);
  if (["word", "root", "affix"].includes(mode)) return Math.max(1, graphFocusAreas.length);
  if (mode === "knowledge") {
    return Math.max(
      1,
      availableAlternateLanguages(activeCard).length,
    );
  }
  return 1;
}

function scheduleCarousel() {
  if (autoplayTimer) clearTimeout(autoplayTimer);
  if (!autoplayEnabled) return;
  const dwell = ambientRouting
    ? CARD_MIN_DWELL_MS
    : Math.max(CARD_MIN_DWELL_MS, activeInnerSlideCount() * INNER_SLIDE_DWELL_MS);
  if (ambientRouting) prefetchAmbientMode();
  autoplayTimer = setTimeout(() => {
    if (mode === "chat") return;
    if (ambientRouting) {
      runAmbientAdvance(userActivityRevision);
      return;
    }
    if (carouselCards.length > 1) navigateCards(1);
    else scheduleCarousel();
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

all(".mode").forEach((button) => button.addEventListener("click", () => activateMode(button.dataset.mode)));
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
  if (autoplayEnabled) {
    cancelPageRestore();
    ambientRouting = mode !== "chat";
    ambientModeIndex = ambientIndexAfter(mode);
  } else {
    ambientRouting = false;
    ambientPrefetch = null;
    window.clearInterval(sentenceSlideTimer);
    window.clearInterval(graphFocusTimer);
    window.clearInterval(alternateTimer);
  }
  updateCarouselChrome();
  scheduleCarousel();
  syncPageUrl();
});
$("#fullscreen-button").addEventListener("click", toggleFullscreen);
$("#settings-button").addEventListener("click", () => {
  syncSettingsControls();
  const dialog = $("#settings-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  noteActivity();
});
$("#settings-language-options").addEventListener("change", (event) => {
  if (event.target.matches("[data-setting-language]")) applySettingsControls();
});
all("[data-setting-mode]").forEach((input) => input.addEventListener("change", applySettingsControls));
$("#random-cards-setting").addEventListener("change", applySettingsControls);
$("#reset-settings").addEventListener("click", resetDisplaySettings);
$("#settings-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
document.addEventListener("fullscreenchange", () => {
  if (!document.fullscreenElement && !new URLSearchParams(location.search).has("display")) {
    document.body.classList.remove("display-mode");
  }
  scheduleGraphViewportFit(160);
});
// Motion reveals the controls but must not restart the 30-second card dwell.
document.addEventListener("pointermove", () => noteActivity(false), { passive: true });
document.addEventListener("pointerdown", () => noteActivity(true), { passive: true });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && new URLSearchParams(location.search).has("display")) {
    event.preventDefault();
    window.close();
    return;
  }
  const navigation = ARROW_NAVIGATION[event.key];
  if (navigation && !hasNavigationModifier(event) && !isEditingTarget(event.target)) {
    event.preventDefault();
    if (navigation.axis === "mode") void navigateModes(navigation.step);
    if (navigation.axis === "card") navigateCards(navigation.step);
  }
  noteActivity(true);
});
document.addEventListener("focusin", () => noteActivity(true));
window.addEventListener("resize", () => {
  scheduleGraphViewportFit(120);
  if (["root", "affix"].includes(activeCard?.mode) && originCy) {
    const needsCompactPages = window.innerWidth <= 620;
    const hasCompactPages = $("#origin-graph")?.classList.contains("compact-morphology-paged");
    const nextCapacity = needsCompactPages ? String(compactMorphologyPageCapacity()) : "0";
    if (needsCompactPages !== hasCompactPages
      || $("#origin-graph")?.dataset.compactMorphologyCapacity !== nextCapacity) {
      renderOriginGraph(recenteredGraphCard || activeCard);
    }
    return;
  }
  if (activeCard?.mode !== "word" || !originCy) return;
  const needsCompactPages = window.innerWidth <= COMPACT_GRAPH_TYPOGRAPHY_BREAKPOINT
    && originCy.nodes().length > 1;
  const hasCompactPages = $("#origin-graph")?.classList.contains("compact-origin-paged");
  if (needsCompactPages !== hasCompactPages) renderOriginGraph(recenteredGraphCard || activeCard);
  // Refit after the delayed viewport and typography passes at either size.
  scheduleCompactWordOriginReadability();
}, { passive: true });
$("#evidence-toggle").addEventListener("click", toggleMorphologyEvidencePanel);

const initialParameters = new URLSearchParams(location.search);
const initialMode = MODE_COPY[initialParameters.get("mode")]
  ? initialParameters.get("mode")
  : activeAmbientModeOrder()[0];
ambientRouting = !initialParameters.has("mode")
  || initialParameters.has("display")
  || initialParameters.has("loop");
if (ambientRouting) ambientModeIndex = ambientIndexAfter(initialMode);
setMode(initialMode);
if (initialParameters.has("display")) document.body.classList.add("display-mode");
loadHealth();
if (initialParameters.has("card")) void restorePageFromUrl();
else loadHistory();
window.addEventListener("popstate", () => { void restorePageFromUrl(); });
renderLabStarters();
loadObservations();
window.setTimeout(() => {
  void loadHealth();
  window.setInterval(loadHealth, 60000);
}, 20000);
window.setTimeout(() => {
  void syncAcceptedDeck();
  window.setInterval(syncAcceptedDeck, ACCEPTED_DECK_SYNC_MS);
}, 11000);
