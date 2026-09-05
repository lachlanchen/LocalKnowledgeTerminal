import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request } from 'node:http';
import { SnapshotStore } from '../lib/snapshot.mjs';
import { createAtlasServer } from '../server.mjs';

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'lkt-atlas-test-'));
  const database = join(directory, 'knowledge.sqlite3');
  const db = new DatabaseSync(database);
  db.exec(`
    CREATE TABLE entities(entity_id TEXT PRIMARY KEY,entity_type TEXT,label TEXT,status TEXT,quality_score REAL,payload TEXT);
    CREATE TABLE terms(entity_id TEXT PRIMARY KEY,language TEXT,kind TEXT);
    CREATE TABLE morphemes(entity_id TEXT PRIMARY KEY,language TEXT,kind TEXT,meaning TEXT);
    CREATE TABLE meanings(entity_id TEXT PRIMARY KEY,language TEXT,definition TEXT,part_of_speech TEXT);
    CREATE TABLE entity_edges(edge_id TEXT,source_entity_id TEXT,target_entity_id TEXT,relation TEXT,status TEXT,basis TEXT,confidence REAL);
    CREATE TABLE relation_assertions(assertion_id TEXT,subject_entity_id TEXT,source_entity_id TEXT,target_entity_id TEXT,relation TEXT,status TEXT,basis TEXT,confidence REAL);
    CREATE TABLE evidence_records(evidence_id TEXT,corpus_id TEXT,source_entry_id TEXT,source_hash TEXT,locator TEXT,excerpt TEXT,payload TEXT);
    CREATE TABLE entity_evidence(entity_id TEXT,evidence_id TEXT);
    CREATE TABLE assertion_evidence(assertion_id TEXT,evidence_id TEXT);
    CREATE TABLE preparation_jobs(job_id TEXT,subject_entity_id TEXT,job_type TEXT,language TEXT,status TEXT,attempts INTEGER,error TEXT,updated_at TEXT);
    CREATE TABLE job_artifacts(job_id TEXT,stage TEXT,validation_state TEXT,payload TEXT,created_at TEXT);
  `);
  const insert = db.prepare('INSERT INTO entities VALUES(?,?,?,?,?,?)');
  for (const row of [
    ['word', 'term', 'signal', 'accepted'], ['root', 'morpheme', 'sign', 'accepted'],
    ['draft', 'term', 'new-form', 'draft'], ['old', 'term', 'old-form', 'archived'],
    ['meaning', 'meaning', 'A test definition', 'accepted'], ['solo', 'term', 'alone', 'accepted'],
  ]) insert.run(...row, 0.7, '{}');
  db.exec(`
    INSERT INTO terms VALUES('word','en','word'),('draft','en','word'),('old','en','word'),('solo','en','word');
    INSERT INTO morphemes VALUES('root','la','root','mark');
    INSERT INTO meanings VALUES('meaning','en','A test definition','noun');
    INSERT INTO entity_edges VALUES('e1','word','root','has-component','accepted','book',0.8),
      ('e2','word','root','has-component','accepted','model',0.6),
      ('e3','word','meaning','has-meaning','accepted','reviewed',0.7),
      ('e4','word','old','derived-from','archived','model',0.4);
    INSERT INTO relation_assertions VALUES('a1','word','word','root','has-component','accepted','book',0.9),
      ('a2','word','root','draft','derived-from','accepted','model',0.5);
    INSERT INTO evidence_records VALUES('ev1','fixture-book','entry-1','fixture','page 1','Retrieved fixture text, not a generated citation.','{}');
    INSERT INTO entity_evidence VALUES('word','ev1');
    INSERT INTO assertion_evidence VALUES('a1','ev1');
    INSERT INTO preparation_jobs VALUES('j1','word','expand-origin-branches','en','complete',1,'','2026-09-06');
  `);
  db.prepare('INSERT INTO job_artifacts VALUES(?,?,?,?,?)').run('j1', 'accepted-origin-branches', 'accepted',
    JSON.stringify({ history_outcome: 'no-safe-history', omitted_steps: [{ form: 'prior-form', reason: 'invalid or missing historical language code' }] }), '2026-09-06');
  db.close();
  const store = new SnapshotStore({ database });
  t.after(() => { store.close(); rmSync(directory, { recursive: true, force: true }); });
  return { store, directory, database };
}

test('full graph has no card-sized node cap and bundles duplicate support without merging identities', t => {
  const { store } = fixture(t);
  const graph = store.network();
  assert.equal(graph.nodes.length, 6);
  assert.equal(graph.links.length, 4);
  const link = graph.links.find(l => l.source === 'word' && l.target === 'root');
  assert.equal(link.records, 3);
  assert.deepEqual(link.bases, ['book', 'model']);
  assert.equal(link.materialized, true);
  assert.equal(graph.links.find(l => l.target === 'draft').materialized, false);
  assert.equal(graph.nodes.find(n => n.id === 'root').language, 'la');
  assert.equal(graph.nodes.find(n => n.id === 'solo').degree, 0);
});

test('detail retains actual evidence and explains missing history without inventing a node', t => {
  const { store } = fixture(t);
  const result = store.detail('word');
  assert.equal(result.evidence.length, 1);
  assert.equal(result.evidence[0].corpus_id, 'fixture-book');
  assert.equal(result.origin.history_outcome, 'no-safe-history');
  assert.match(result.origin.omitted_steps[0].reason, /language code/);
  assert.equal(store.network().nodes.some(n => n.type === 'historical-form'), false);
});

test('store opens SQLite read-only and parameterizes node lookup', t => {
  const { store } = fixture(t);
  store.network();
  assert.throws(() => store.db.exec('DELETE FROM entities'), /readonly|read-only/i);
  assert.equal(store.detail("' OR 1=1 --"), null);
  assert.equal(store.network().nodes.length, 6);
});

test('snapshot manifest cannot escape the data directory', t => {
  const { directory } = fixture(t);
  const manifest = join(directory, 'snapshot.json');
  writeFileSync(manifest, JSON.stringify({ file: '../private.sqlite3' }));
  const store = new SnapshotStore({ manifest });
  assert.throws(() => store.open(), /inside data/);
});

test('API is loopback-only, read-only and never serves database files', async t => {
  const { store, directory } = fixture(t);
  const dist = join(directory, 'dist'); mkdirSync(dist); writeFileSync(join(dist, 'index.html'), '<h1>Atlas fixture</h1>');
  const server = createAtlasServer({ store, dist });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const base = `http://127.0.0.1:${server.address().port}`;
  const network = await fetch(base + '/api/network');
  assert.equal(network.status, 200); assert.equal((await network.json()).nodes.length, 6);
  assert.equal((await fetch(base + '/api/health')).status, 200);
  assert.equal((await fetch(base + '/api/node/word')).status, 200);
  assert.equal((await fetch(base + '/api/node/missing')).status, 404);
  assert.equal((await fetch(base + '/data/knowledge.sqlite3')).status, 404);
  assert.equal((await fetch(base + '/api/network', { method: 'POST' })).status, 405);
  assert.equal((await fetch(base + '/api/network', { headers: { Origin: 'https://example.com' } })).status, 403);
  const hostileHost = await new Promise((resolve, reject) => {
    const req = request(base + '/api/network', { headers: { Host: 'attacker.invalid' } }, response => {
      response.resume(); response.on('end', () => resolve(response.statusCode));
    }); req.on('error', reject); req.end();
  });
  assert.equal(hostileHost, 403);
  const html = await fetch(base + '/'); assert.equal(html.status, 200);
  assert.match(html.headers.get('content-security-policy'), /frame-ancestors 'none'/);
});
