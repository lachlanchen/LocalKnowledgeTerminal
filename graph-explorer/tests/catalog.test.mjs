import test from 'node:test';
import assert from 'node:assert/strict';
import { CATEGORIES, category, searchNodes, makeIndex, neighborhood, filterGraph, escapeHTML } from '../src/catalog.mjs';

const nodes = [
  { id: 'a', label: 'signal', type: 'term', language: 'en', status: 'accepted', degree: 2 },
  { id: 'b', label: 'sign', type: 'morpheme', kind: 'root', language: 'la', status: 'accepted', degree: 2 },
  { id: 'c', label: 'new signal', type: 'term', language: 'en', status: 'draft', degree: 1 },
  { id: 'd', label: 'old', type: 'term', language: 'en', status: 'archived', degree: 1 },
  { id: 'e', label: '\u4fe1\u53f7', type: 'term', language: 'zh', status: 'accepted', degree: 0 },
];
const edge = (source, target, extra = {}) => ({ source, target, relation: 'related-to', status: 'accepted', materialized: true, bases: ['model'], ...extra });
const graph = { nodes, links: [edge('a', 'b', { bases: ['book'] }), edge('b', 'c', { materialized: false }), edge('a', 'd'), edge('b', 'a')] };
const index = makeIndex(graph);
const options = () => ({ categories: new Set(Object.keys(CATEGORIES)), drafts: true, archived: false, assertions: false, isolated: true, bookOnly: false, language: '', focus: false, depth: 1, selected: null });

test('search ranks exact words first and preserves multilingual labels', () => {
  assert.equal(searchNodes(nodes, 'SIGNAL')[0].id, 'a');
  assert.equal(searchNodes(nodes, '\u4fe1')[0].id, 'e');
  assert.deepEqual(searchNodes(nodes, ''), []);
});

test('prefix and root are distinct categories', () => {
  assert.equal(category({ type: 'morpheme', kind: 'prefix' }), 'affix');
  assert.equal(category({ type: 'morpheme', kind: 'root' }), 'root');
  assert.equal(category({ type: 'term', label: 'pre' }), 'word');
});

test('neighborhood traversal terminates through cycles and honors edge policy', () => {
  assert.deepEqual([...neighborhood(graph, index, 'a', 1, l => l.materialized)].sort(), ['a', 'b', 'd']);
  assert.equal(neighborhood(graph, index, 'a', 3).size, 4);
  assert.equal(neighborhood(graph, index, 'missing', 3).size, 0);
});

test('default view excludes archived nodes and assertion-only links without removing the records', () => {
  const result = filterGraph(graph, index, options());
  assert.equal(result.nodes.length, 4);
  assert.equal(result.links.length, 2);
  assert.equal(graph.nodes.length, 5);
  const expanded = filterGraph(graph, index, { ...options(), archived: true, assertions: true });
  assert.equal(expanded.links.length, 4);
});

test('language, isolated and book filters do not leave dangling displayed edges', () => {
  assert.equal(filterGraph(graph, index, { ...options(), language: 'zh' }).nodes.length, 1);
  const result = filterGraph(graph, index, { ...options(), isolated: false, bookOnly: true });
  assert.deepEqual(result.nodes.map(n => n.id), ['a', 'b']);
  assert.equal(result.links.length, 1);
});

test('focus is an explicit subset, not a silent full-network limit', () => {
  const result = filterGraph(graph, index, { ...options(), focus: true, selected: 'a' });
  assert.deepEqual(result.nodes.map(n => n.id), ['a', 'b']);
});

test('book and model text cannot inject markup into UI templates', () => {
  assert.equal(escapeHTML('<img src=x onerror="x">&'), '&lt;img src=x onerror=&quot;x&quot;&gt;&amp;');
});
