import '@fontsource-variable/space-grotesk';
import '@fontsource/ibm-plex-mono/400.css';
import './style.css';
import { AtlasScene } from './scene.js';
import { CATEGORIES, category, makeIndex, searchNodes, filterGraph, escapeHTML as e } from './catalog.mjs';

const $ = selector => document.querySelector(selector);
const format = number => new Intl.NumberFormat('en').format(number);
const state = { graph: null, index: null, selected: null, focus: false, depth: 1,
  categories: new Set(Object.keys(CATEGORIES)), drafts: true, archived: false,
  assertions: false, isolated: true, bookOnly: false, language: '', paused: false };
let scene, view, worker, generation = 0, detailAbort, fitAfterLayout = true;

$('#app').innerHTML = `
  <header class="masthead">
    <a class="brand" href="/" aria-label="Lexicon Atlas home"><span class="brand-mark">LA<span></span></span><span>LEXICON <b>ATLAS</b><small>LOCAL KNOWLEDGE TERMINAL</small></span></a>
    <div class="edition"><span class="live-dot"></span> PRIVATE COLLECTION <i>/</i> <span id="snapshot-date">CONNECTING</span></div>
    <div class="header-actions"><button id="mobile-filters" class="mobile-only">Explore</button><button id="reload" class="quiet-button">Reload snapshot <span aria-hidden="true">&#8635;</span></button></div>
  </header>
  <main class="workspace">
    <aside class="navigation panel" id="navigation">
      <div class="eyebrow">01 / FIND A CONNECTION</div>
      <h1>A world<br>inside a word.</h1>
      <p class="intro">Follow the roots. Cross languages.<br>See knowledge taking shape.</p>
      <label class="search-box"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg><input id="search" type="search" placeholder="Word, root, meaning..." autocomplete="off" aria-label="Search every graph record"><kbd>/</kbd></label>
      <div id="search-results" class="search-results" aria-live="polite"></div>
      <div class="section-heading"><h2>Knowledge layers</h2><button id="all-layers" class="text-button">All / none</button></div>
      <div id="layers" class="layers"></div>
      <label class="language-select"><span>Language</span><select id="language"><option value="">Every language</option></select></label>
      <div class="section-heading"><h2>Record visibility</h2><span class="mini-label">RAW GRAPH</span></div>
      <div class="switches">
        <label><input id="drafts" type="checkbox" checked><span>Work in progress</span><small>drafts</small></label>
        <label><input id="isolated" type="checkbox" checked><span>Unconnected records</span></label>
        <label><input id="assertions" type="checkbox"><span>Unmaterialized assertions</span></label>
        <label><input id="archived" type="checkbox"><span>Archived / rejected</span></label>
        <label><input id="book-only" type="checkbox"><span>Book-backed links only</span></label>
      </div>
      <div class="collection-note"><span class="small-orbit" aria-hidden="true"></span><div><strong>Your books. Your machine.</strong><p>Read-only SQLite snapshot.<br>No cloud inference. No invented edges.</p></div></div>
      <button id="hide-filters" class="mobile-only quiet-button">Return to atlas</button>
    </aside>
    <section class="stage" id="stage" aria-label="Knowledge graph visualization">
      <div class="stage-atmosphere" aria-hidden="true"></div>
      <div class="stage-heading"><div><span class="eyebrow" id="view-caption">02 / THE FULL COLLECTION</span><h2 id="view-title">Language, connected.</h2></div><span class="dimension-badge">SPATIAL ATLAS</span></div>
      <div class="layout-state"><span id="layout-message">Opening your collection</span><span id="layout-percent"></span><div><i id="layout-progress"></i></div></div>
      <div id="empty" class="empty-state" hidden><span class="eyebrow">NOTHING IS LOST</span><h2>No records in this view.</h2><p>Try another layer or language, or restore the full collection.</p><button id="reset-filters" class="primary-button">Reset filters</button></div>
      <div id="error" class="error-state" hidden><span class="eyebrow">LOCAL SNAPSHOT REQUIRED</span><h2>Bring your knowledge home.</h2><p id="error-message"></p><code>npm run sync</code><button id="retry" class="primary-button">Try again</button></div>
      <div class="view-controls"><div class="segmented" aria-label="Graph dimensions"><button id="mode-3d" class="active" aria-pressed="true">3D</button><button id="mode-2d" aria-pressed="false">2D</button></div><button id="fit" title="Fit the current view (F)">Fit view <kbd>F</kbd></button><button id="orbit" aria-pressed="false">Orbit</button><button id="pause" aria-pressed="false">Pause layout</button></div>
      <div class="canvas-hint">DRAG TO ORBIT <span>/</span> SCROLL TO TRAVEL <span>/</span> CLICK TO DISCOVER</div>
    </section>
    <aside class="inspector panel" id="inspector">
      <div class="inspector-heading"><span class="eyebrow">03 / LOOK CLOSER</span><button id="close-inspector" class="icon-button" title="Clear selection" aria-label="Clear selection">&#215;</button></div>
      <div id="inspector-content"></div>
    </aside>
  </main>
  <footer class="statusbar"><div><span class="live-dot"></span><strong id="node-count">0</strong> <span>nodes</span><i>/</i><strong id="edge-count">0</strong><span>relationships</span><span id="hidden-count"></span></div><div class="runtime-status"><span id="fps">WEBGL</span><i>/</i><span>READ ONLY</span><button id="export" title="Download visible graph JSON">Export view &#8599;</button></div></footer>`;

function welcome() {
  const suggestions = state.graph?.nodes.filter(n => n.type === 'term' && n.status === 'accepted' && n.language === 'en')
    .sort((a, b) => b.degree - a.degree).slice(0, 4) || [];
  $('#inspector-content').innerHTML = `<div class="welcome"><div class="orbit-art" aria-hidden="true"><i></i><i></i><i></i><b></b><span></span></div><span class="eyebrow">A LIVING LANGUAGE LANDSCAPE</span><h2>Nothing exists<br>in isolation.</h2><p>Select a point to uncover its meanings, family, history and book evidence.</p><div class="section-heading"><h3>Start somewhere connected</h3></div><div class="suggestions">${suggestions.map(n => `<button data-node="${e(n.id)}"><span>${e(n.label)}</span><small>${format(n.degree)} links &#8599;</small></button>`).join('')}</div><div class="reading-guide"><h3>Reading this atlas</h3><p>Color identifies a knowledge layer. Brighter points have more connections. Muted records are still in preparation.</p><p>A lone point can mean missing history, unfinished extraction, or an active filter. Inspect its records to tell the difference.</p></div></div>`;
}

function updateView({ fit = false } = {}) {
  if (!state.graph || !scene) return;
  view = filterGraph(state.graph, state.index, state);
  scene.setVisible(view); scene.setSelected(state.selected);
  $('#node-count').textContent = format(view.nodes.length); $('#edge-count').textContent = format(view.links.length);
  $('#hidden-count').textContent = `${format(state.graph.nodes.length - view.nodes.length)} records hidden`;
  $('#empty').hidden = view.nodes.length > 0;
  $('#view-caption').textContent = state.focus ? `02 / ${state.depth}-HOP NEIGHBORHOOD` : '02 / THE FULL COLLECTION';
  $('#view-title').textContent = state.focus ? state.index.nodes.get(state.selected)?.label || 'Neighborhood' : 'Language, connected.';
  if (fit) scene.fit();
}

function renderSearch() {
  const results = state.graph ? searchNodes(state.graph.nodes, $('#search').value) : [];
  $('#search-results').innerHTML = results.map(n => `<button class="search-hit" data-node="${e(n.id)}" style="--node-color:${CATEGORIES[category(n)].color}"><i></i><span><strong>${e(n.label)}</strong><small>${e(n.language || n.type)} / ${e(n.kind || n.type)} / ${e(n.status)}</small></span><b>${format(n.degree)}</b></button>`).join('');
  if ($('#search').value && !results.length && state.graph) $('#search-results').textContent = 'No matching record in this snapshot.';
}

function resetFilters() {
  state.categories = new Set(Object.keys(CATEGORIES)); state.language = ''; state.focus = false;
  state.drafts = true; state.isolated = true; state.assertions = false; state.archived = false; state.bookOnly = false;
  $('#language').value = '';
  for (const [id, key] of [['drafts', 'drafts'], ['isolated', 'isolated'], ['assertions', 'assertions'], ['archived', 'archived'], ['book-only', 'bookOnly']]) $('#' + id).checked = state[key];
  document.querySelectorAll('[data-layer]').forEach(button => { button.classList.add('active'); button.setAttribute('aria-pressed', 'true'); });
  updateView({ fit: true });
}

function showError(message) { $('#error').hidden = false; $('#error-message').textContent = message; $('#layout-message').textContent = 'Connection paused'; }

async function loadGraph() {
  $('#error').hidden = true; $('#reload').disabled = true;
  try {
    const response = await fetch('/api/network'); const graph = await response.json();
    if (!response.ok) throw new Error(graph.error || 'Could not open the local snapshot.');
    if (!scene) scene = new AtlasScene($('#stage'), {
      onSelect: selectNode, onError: showError,
      onStats: ({ fps, drawCalls }) => { $('#fps').textContent = `${fps} FPS / ${drawCalls} DRAW CALLS`; },
    });
    state.graph = graph; state.index = makeIndex(graph); state.selected = null; state.focus = false;
    scene.setGraph(graph);
    const counts = new Map(); graph.nodes.forEach(n => counts.set(category(n), (counts.get(category(n)) || 0) + 1));
    $('#layers').innerHTML = Object.entries(CATEGORIES).map(([key, value]) => `<button data-layer="${key}" class="layer ${state.categories.has(key) ? 'active' : ''}" aria-pressed="${state.categories.has(key)}" style="--node-color:${value.color}"><i></i><span>${value.label}</span><small>${format(counts.get(key) || 0)}</small><b>+</b></button>`).join('');
    const languages = [...new Set(graph.nodes.map(n => n.language).filter(Boolean))].sort();
    $('#language').innerHTML = '<option value="">Every language</option>' + languages.map(l => `<option value="${e(l)}">${e(l.toUpperCase())}</option>`).join('');
    $('#language').value = state.language;
    $('#snapshot-date').textContent = new Date(graph.meta.copiedAt).toLocaleDateString('en', { day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase();
    $('#snapshot-date').title = `Snapshot: ${graph.meta.file}\nSHA-256: ${graph.meta.sha256 || 'not recorded'}`;
    welcome(); updateView({ fit: true }); renderSearch();
    worker?.terminate(); worker = new Worker(new URL('./layout.worker.js', import.meta.url), { type: 'module' });
    generation++; fitAfterLayout = true; state.paused = false;
    $('#pause').textContent = 'Pause layout'; $('#pause').setAttribute('aria-pressed', 'false');
    worker.onmessage = ({ data }) => {
      if (data.generation !== generation) return;
      scene.setPositions(data.positions);
      $('#layout-message').textContent = data.done ? 'Spatial layout settled' : 'Finding the shape of language';
      $('#layout-percent').textContent = `${Math.min(100, Math.round(data.progress * 100))}%`;
      $('#layout-progress').style.width = `${Math.min(100, data.progress * 100)}%`;
      if (data.done && fitAfterLayout) { scene.fit(); fitAfterLayout = false; }
    };
    worker.onerror = () => { $('#layout-message').textContent = 'Layout paused; initial map remains usable'; };
    worker.postMessage({ type: 'layout', generation, nodes: graph.nodes.map(n => ({ id: n.id })),
      links: graph.links.filter(l => l.status === 'accepted' && l.materialized).map(l => ({ source: l.source, target: l.target })) });
  } catch (error) { showError(error.message); }
  finally { $('#reload').disabled = false; }
}

async function selectNode(id) {
  const node = state.index?.nodes.get(id); if (!node) return;
  fitAfterLayout = false; state.selected = id;
  state.categories.add(category(node));
  if (node.status === 'draft') { state.drafts = true; $('#drafts').checked = true; }
  if (['archived', 'rejected'].includes(node.status)) { state.archived = true; $('#archived').checked = true; }
  if (state.language && state.language !== node.language) { state.language = ''; $('#language').value = ''; }
  const layer = document.querySelector(`[data-layer="${category(node)}"]`); layer?.classList.add('active'); layer?.setAttribute('aria-pressed', 'true');
  $('#navigation').classList.remove('open'); $('#inspector').classList.add('open');
  updateView(); scene.focus(id);
  $('#inspector-content').innerHTML = `<div class="node-identity"><span class="node-type" style="--node-color:${CATEGORIES[category(node)].color}">${e(node.kind || node.type)}</span><h2>${e(node.label)}</h2><p>Opening the record...</p></div>`;
  detailAbort?.abort(); detailAbort = new AbortController();
  try {
    const response = await fetch('/api/node/' + encodeURIComponent(id), { signal: detailAbort.signal });
    const detail = await response.json(); if (!response.ok) throw new Error(detail.error);
    if (state.selected !== id) return;
    renderDetail(detail);
  } catch (error) { if (error.name !== 'AbortError') $('#inspector-content').append(Object.assign(document.createElement('p'), { textContent: error.message })); }
}

function renderDetail(detail) {
  const { node, record, relationships, evidence, jobs, origin } = detail;
  const active = relationships.filter(l => !['archived', 'rejected'].includes(l.status));
  const meaning = record.definition || record.meaning || record.description || record.summary || node.meaning || '';
  const hasMissingHistory = origin?.history_outcome === 'no-safe-history';
  const omitted = origin?.omitted_steps || [];
  const failures = jobs.filter(j => j.status === 'failed');
  const pending = jobs.filter(j => ['queued', 'running'].includes(j.status));
  $('#inspector-content').innerHTML = `
    <div class="node-identity"><div class="node-badges"><span class="node-type" style="--node-color:${CATEGORIES[category(node)].color}">${e(node.kind || node.type)}</span><span>${e(node.language || 'UNSPECIFIED')}</span><span class="status-tag">${e(node.status)}</span></div><h2 dir="auto">${e(node.label)}</h2>${record.period_label ? `<span class="period-label">${e(record.period_label)}</span>` : ''}<p class="node-meaning" dir="auto">${e(meaning || 'No definition has been attached to this record yet.')}</p><div class="node-metrics"><div><strong>${format(active.length)}</strong><span>relationships</span></div><div><strong>${format(evidence.length)}</strong><span>source records</span></div><div><strong>${node.quality == null ? '--' : Math.round(node.quality * 100) + '%'}</strong><span>stored score</span></div></div></div>
    <div class="focus-actions"><button id="focus-neighborhood" class="primary-button">${state.focus ? 'Return to full atlas' : 'Isolate neighborhood'} <span>&#8599;</span></button><label>Depth <select id="depth"><option value="1" ${state.depth === 1 ? 'selected' : ''}>1 hop</option><option value="2" ${state.depth === 2 ? 'selected' : ''}>2 hops</option><option value="3" ${state.depth === 3 ? 'selected' : ''}>3 hops</option></select></label></div>
    ${hasMissingHistory || omitted.length || !active.length ? `<div class="gap-note"><span class="eyebrow">KNOWLEDGE GAP / NOT A CANVAS LIMIT</span><h3>${hasMissingHistory ? 'History has not been accepted yet.' : !active.length ? 'No active relationships recorded.' : 'Some history steps were omitted.'}</h3><p>${hasMissingHistory ? 'The saved origin result is no-safe-history. A 3D layout cannot supply missing historical facts.' : 'The atlas shows stored relationships, not inferred connections created by the renderer.'}</p>${omitted.map(step => `<p class="gap-reason"><b>${e(step.form || 'Historical step')}</b>: ${e(step.reason || 'Awaiting review')}</p>`).join('')}</div>` : ''}
    <div class="section-heading"><h3>Connections</h3><span class="mini-label">${format(relationships.length)} RECORD GROUPS</span></div>
    <div id="connections" class="connections"></div><button id="more-connections" class="text-button" hidden>Show more connections</button>
    <div class="section-heading"><h3>Book evidence</h3><span class="mini-label">STORED SOURCES</span></div>
    <div class="evidence-list">${evidence.length ? evidence.map((source, i) => `<details ${i === 0 ? 'open' : ''}><summary><span>${e(source.payload.source_title || source.corpus_id)}</span><small>${e(source.locator || source.source_entry_id)}</small></summary><p dir="auto">${e(source.excerpt)}</p><code>${e(source.evidence_id)}</code></details>`).join('') : '<p class="muted-copy">No direct source record is attached here. Model-authored connections are not presented as book quotations.</p>'}</div>
    <details class="preparation"><summary>Preparation status <span>${pending.length} pending / ${failures.length} failed</span></summary><p class="muted-copy">Snapshot state, not a live connection to the Pi. The explorer never starts inference.</p>${jobs.map(job => `<div class="job"><strong>${e(job.job_type)}</strong><span>${e(job.language)} / ${e(job.status)}</span>${job.error ? `<p>${e(job.error)}</p>` : ''}</div>`).join('')}${jobs.length >= detail.jobLimit ? '<p class="muted-copy">Showing the latest 60 jobs.</p>' : ''}</details>
    <details class="raw-record"><summary>Inspect stored record</summary><pre>${e(JSON.stringify(record, null, 2))}</pre></details>`;
  let shown = 0;
  const appendConnections = () => {
    const page = relationships.slice(shown, shown + 24); shown += page.length;
    for (const link of page) {
      const button = document.createElement('button'); button.className = 'connection'; button.dataset.node = link.neighbor.id;
      button.style.setProperty('--node-color', CATEGORIES[category(link.neighbor)].color);
      button.innerHTML = `<span class="connection-direction">${link.direction === 'outgoing' ? '&#8594;' : '&#8592;'}</span><span><strong dir="auto">${e(link.neighbor.label)}</strong><small>${e(link.relation)} / ${e(link.neighbor.language || link.neighbor.kind || link.neighbor.type)}</small><em>${e(link.bases.join(' + '))} / ${e(link.status)}${link.materialized ? '' : ' / assertion only'}</em></span>`;
      $('#connections').append(button);
    }
    $('#more-connections').hidden = shown >= relationships.length;
  };
  appendConnections(); $('#more-connections').onclick = appendConnections;
  $('#focus-neighborhood').onclick = () => { state.focus = !state.focus; updateView({ fit: true }); $('#focus-neighborhood').firstChild.textContent = state.focus ? 'Return to full atlas ' : 'Isolate neighborhood '; };
  $('#depth').onchange = event => { state.depth = Number(event.target.value); if (state.focus) updateView({ fit: true }); };
}

document.addEventListener('click', event => {
  const target = event.target.closest('[data-node]'); if (target) selectNode(target.dataset.node);
  const layer = event.target.closest('[data-layer]');
  if (layer) {
    const key = layer.dataset.layer;
    if (state.categories.has(key)) state.categories.delete(key); else state.categories.add(key);
    layer.classList.toggle('active', state.categories.has(key)); layer.setAttribute('aria-pressed', String(state.categories.has(key))); updateView();
  }
});
$('#search').addEventListener('input', renderSearch);
$('#search').addEventListener('keydown', event => {
  if (event.key === 'Enter') { const first = $('#search-results [data-node]'); if (first) selectNode(first.dataset.node); }
  if (event.key === 'ArrowDown') { event.preventDefault(); $('#search-results button')?.focus(); }
});
$('#all-layers').onclick = () => {
  state.categories = new Set(state.categories.size ? [] : Object.keys(CATEGORIES));
  document.querySelectorAll('[data-layer]').forEach(button => { button.classList.toggle('active', state.categories.has(button.dataset.layer)); button.setAttribute('aria-pressed', String(state.categories.has(button.dataset.layer))); });
  updateView();
};
$('#language').onchange = event => { state.language = event.target.value; updateView({ fit: true }); };
for (const [id, key] of [['drafts', 'drafts'], ['isolated', 'isolated'], ['assertions', 'assertions'], ['archived', 'archived'], ['book-only', 'bookOnly']]) $('#' + id).onchange = event => { state[key] = event.target.checked; updateView(); };
$('#reload').onclick = loadGraph; $('#retry').onclick = loadGraph; $('#reset-filters').onclick = resetFilters;
$('#fit').onclick = () => { fitAfterLayout = false; scene?.fit(); };
$('#pause').onclick = () => { state.paused = !state.paused; worker?.postMessage({ type: state.paused ? 'pause' : 'resume' }); $('#pause').textContent = state.paused ? 'Resume layout' : 'Pause layout'; $('#pause').setAttribute('aria-pressed', String(state.paused)); };
$('#orbit').onclick = () => { if (!scene || scene.dimension === 2) return; scene.controls.autoRotate = !scene.controls.autoRotate; $('#orbit').setAttribute('aria-pressed', String(scene.controls.autoRotate)); };
for (const dimension of [2, 3]) $('#mode-' + dimension + 'd').onclick = () => {
  if (!scene) return; scene.setDimension(dimension); $('#orbit').setAttribute('aria-pressed', 'false');
  for (const d of [2, 3]) { $('#mode-' + d + 'd').classList.toggle('active', d === dimension); $('#mode-' + d + 'd').setAttribute('aria-pressed', String(d === dimension)); }
};
$('#close-inspector').onclick = () => { detailAbort?.abort(); state.selected = null; state.focus = false; $('#inspector').classList.remove('open'); welcome(); updateView(); };
$('#mobile-filters').onclick = () => $('#navigation').classList.toggle('open');
$('#hide-filters').onclick = () => $('#navigation').classList.remove('open');
$('#export').onclick = () => {
  if (!view) return;
  const blob = new Blob([JSON.stringify({ meta: state.graph.meta, nodes: view.nodes, links: view.links }, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = 'lkt-atlas-visible-graph.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') { $('#navigation').classList.remove('open'); $('#close-inspector').click(); }
  if (event.target.matches('input,textarea,select')) return;
  if (event.key === '/') { event.preventDefault(); $('#navigation').classList.add('open'); $('#search').focus(); }
  if (event.key.toLowerCase() === 'f') $('#fit').click();
});
welcome(); loadGraph();
