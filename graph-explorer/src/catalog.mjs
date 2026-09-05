export const CATEGORIES = {
  word: { label: 'Words', color: '#83d8d1' },
  root: { label: 'Roots', color: '#d5f879' },
  affix: { label: 'Affixes', color: '#ffa978' },
  meaning: { label: 'Meanings', color: '#86b7ff' },
  history: { label: 'History', color: '#ed9cc7' },
  sound: { label: 'Pronunciation', color: '#ddd1a1' },
  translation: { label: 'Translations', color: '#c5adff' },
  grammar: { label: 'Grammar', color: '#93b59e' },
  content: { label: 'Book cards', color: '#9babbc' },
};

export function category(node) {
  if (node.type === 'morpheme') return ['prefix', 'suffix', 'infix', 'affix', 'circumfix'].includes(node.kind) ? 'affix' : 'root';
  return ({ term: 'word', meaning: 'meaning', 'historical-form': 'history',
    'history-event': 'history', pronunciation: 'sound', translation: 'translation',
    'grammar-analysis': 'grammar', 'content-item': 'content' })[node.type] || 'content';
}

export function searchable(value) {
  return String(value ?? '').normalize('NFKC').toLocaleLowerCase().trim();
}

export function searchNodes(nodes, query, limit = 20) {
  const key = searchable(query);
  if (!key) return [];
  return nodes.filter(n => searchable(n.label).includes(key))
    .sort((a, b) => {
      const score = n => (searchable(n.label) === key ? 100 : searchable(n.label).startsWith(key) ? 50 : 0)
        + (n.status === 'accepted' ? 10 : 0) + (n.type === 'term' ? 5 : 0);
      return score(b) - score(a) || b.degree - a.degree || a.id.localeCompare(b.id);
    }).slice(0, limit);
}

export function makeIndex(graph) {
  const nodes = new Map(graph.nodes.map(n => [n.id, n]));
  const adjacency = new Map(graph.nodes.map(n => [n.id, []]));
  graph.links.forEach((link, index) => {
    if (!nodes.has(link.source) || !nodes.has(link.target)) return;
    adjacency.get(link.source).push(index);
    if (link.target !== link.source) adjacency.get(link.target).push(index);
  });
  return { nodes, adjacency };
}

export function neighborhood(graph, index, subject, depth = 1, linkAllowed = () => true) {
  const found = new Set(index.nodes.has(subject) ? [subject] : []);
  let frontier = [...found];
  for (let level = 0; level < depth; level++) {
    const next = [];
    for (const id of frontier) for (const i of index.adjacency.get(id) || []) {
      const edge = graph.links[i];
      if (!linkAllowed(edge)) continue;
      const other = edge.source === id ? edge.target : edge.source;
      if (!found.has(other)) { found.add(other); next.push(other); }
    }
    frontier = next;
  }
  return found;
}

export function filterGraph(graph, index, options) {
  const linkAllowed = l => (options.archived || !['archived', 'rejected'].includes(l.status))
    && (options.assertions || l.materialized)
    && (!options.bookOnly || l.bases.includes('book'));
  const scope = options.focus && options.selected
    ? neighborhood(graph, index, options.selected, options.depth, linkAllowed) : null;
  let nodes = graph.nodes.filter(n => options.categories.has(category(n))
    && (options.archived || !['archived', 'rejected'].includes(n.status))
    && (options.drafts || n.status !== 'draft')
    && (!options.language || n.language === options.language)
    && (!scope || scope.has(n.id)));
  let ids = new Set(nodes.map(n => n.id));
  const links = graph.links.filter(l => linkAllowed(l) && ids.has(l.source) && ids.has(l.target));
  if (!options.isolated) {
    const connected = new Set(links.flatMap(l => [l.source, l.target]));
    nodes = nodes.filter(n => connected.has(n.id) || n.id === options.selected);
    ids = new Set(nodes.map(n => n.id));
  }
  return { nodes, links, ids };
}

export function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
