import { DatabaseSync } from 'node:sqlite';
import { statSync, readFileSync } from 'node:fs';
import { resolve, basename, dirname } from 'node:path';

const parse = text => { try { return JSON.parse(text || '{}'); } catch { return {}; } };
const TYPE_TABLES = {
  term: 'terms', morpheme: 'morphemes', meaning: 'meanings',
  'historical-form': 'historical_forms', 'history-event': 'history_events',
  pronunciation: 'pronunciations', translation: 'translations',
  'grammar-analysis': 'grammar_analyses', 'content-item': 'content_items',
};

export class SnapshotStore {
  constructor({ manifest, database } = {}) {
    this.manifest = manifest;
    this.database = database;
    this.signature = '';
    this.db = null;
    this.graph = null;
  }

  open() {
    const manifest = this.database ? { file: this.database } : JSON.parse(readFileSync(this.manifest, 'utf8'));
    if (!this.database && (basename(manifest.file) !== manifest.file || !manifest.file.endsWith('.sqlite3'))) {
      throw new Error('Snapshot manifest must name a SQLite file inside data/.');
    }
    const path = this.database ? resolve(this.database) : resolve(dirname(this.manifest), manifest.file);
    const stat = statSync(path);
    const signature = `${path}:${stat.size}:${stat.mtimeMs}`;
    if (signature === this.signature && this.db) return this;
    const db = new DatabaseSync(path, { readOnly: true, timeout: 3000 });
    db.exec('PRAGMA query_only=ON');
    const tables = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => r.name));
    if (!tables.has('entities') || !tables.has('entity_edges')) { db.close(); throw new Error('Not an LKT knowledge snapshot.'); }
    this.db?.close();
    this.db = db;
    this.tables = tables;
    this.signature = signature;
    this.graph = null;
    this.info = { file: basename(path), copiedAt: manifest.copiedAt || stat.mtime.toISOString(),
      sha256: manifest.sha256 || '', bytes: stat.size, source: manifest.source || 'local SQLite snapshot' };
    return this;
  }

  network() {
    this.open();
    if (this.graph) return this.graph;
    const nodes = this.db.prepare('SELECT entity_id AS id,entity_type AS type,label,status,quality_score AS quality FROM entities ORDER BY entity_id').all();
    const byId = new Map(nodes.map(n => [n.id, n]));
    for (const [type, table] of Object.entries(TYPE_TABLES)) {
      if (!this.tables.has(table)) continue;
      const fields = {
        terms: 'entity_id,language,kind', morphemes: 'entity_id,language,kind,meaning',
        meanings: 'entity_id,language,definition AS meaning,part_of_speech AS kind',
        historical_forms: 'entity_id,language,period_label AS period,meaning',
        history_events: 'entity_id,language,period_label AS period,description AS meaning',
        pronunciations: 'entity_id,language,system AS kind,reading AS meaning',
        translations: 'entity_id,target_language AS language,transliteration AS reading',
        grammar_analyses: 'entity_id,language,analysis_type AS kind,summary AS meaning',
        content_items: 'entity_id,language,kind',
      }[table];
      for (const row of this.db.prepare(`SELECT ${fields} FROM ${table}`).all()) {
        const node = byId.get(row.entity_id);
        if (!node) continue;
        const { entity_id, ...properties } = row;
        Object.assign(node, properties);
        if (node.meaning) node.meaning = node.meaning.slice(0, 220);
      }
    }
    const bundles = new Map();
    let dangling = 0;
    const add = (row, materialized) => {
      if (!byId.has(row.source_entity_id) || !byId.has(row.target_entity_id)) { dangling++; return; }
      // Keep semantic relations and statuses distinct; bundle duplicate support
      // records rather than draw parallel arrows for the same relationship.
      const key = JSON.stringify([row.source_entity_id, row.target_entity_id, row.relation, row.status]);
      let link = bundles.get(key);
      if (!link) {
        link = { id: row.edge_id || row.assertion_id, source: row.source_entity_id,
          target: row.target_entity_id, relation: row.relation, status: row.status,
          bases: [], confidence: 0, materialized: false, records: 0 };
        bundles.set(key, link);
      }
      link.materialized ||= materialized;
      link.records++;
      link.confidence = Math.max(link.confidence, Number(row.confidence) || 0);
      if (!link.bases.includes(row.basis)) link.bases.push(row.basis);
    };
    for (const row of this.db.prepare('SELECT edge_id,source_entity_id,target_entity_id,relation,status,basis,confidence FROM entity_edges').all()) add(row, true);
    if (this.tables.has('relation_assertions')) {
      for (const row of this.db.prepare('SELECT assertion_id,source_entity_id,target_entity_id,relation,status,basis,confidence FROM relation_assertions').all()) add(row, false);
    }
    const links = [...bundles.values()];
    for (const n of nodes) { n.degree = 0; n.language ||= ''; n.kind ||= ''; }
    for (const l of links) {
      if (l.status !== 'accepted' || !l.materialized) continue;
      byId.get(l.source).degree++;
      if (l.source !== l.target) byId.get(l.target).degree++;
    }
    const counts = Object.fromEntries(['accepted', 'draft', 'archived', 'rejected'].map(status => [status, nodes.filter(n => n.status === status).length]));
    this.byId = byId;
    this.graph = { nodes, links, meta: { ...this.info, signature: this.signature,
      nodes: nodes.length, links: links.length, statusCounts: counts,
      materializedLinks: links.filter(l => l.materialized).length,
      assertionOnlyLinks: links.filter(l => !l.materialized).length, danglingRecords: dangling } };
    return this.graph;
  }

  detail(id) {
    this.network();
    const node = this.byId.get(id);
    if (!node) return null;
    const raw = this.db.prepare('SELECT * FROM entities WHERE entity_id=?').get(id);
    const table = TYPE_TABLES[node.type];
    const typed = table && this.tables.has(table) ? this.db.prepare(`SELECT * FROM ${table} WHERE entity_id=?`).get(id) : {};
    const relationships = this.graph.links.filter(l => l.source === id || l.target === id).map(l => ({ ...l,
      direction: l.source === id ? 'outgoing' : 'incoming',
      neighbor: this.byId.get(l.source === id ? l.target : l.source) }));
    let evidence = [];
    if (this.tables.has('entity_evidence') && this.tables.has('evidence_records')) {
      evidence = this.db.prepare(`SELECT DISTINCT er.* FROM evidence_records er
        JOIN entity_evidence ee USING(evidence_id) WHERE ee.entity_id=? LIMIT 40`).all(id);
      if (this.tables.has('assertion_evidence') && this.tables.has('relation_assertions')) {
        evidence.push(...this.db.prepare(`SELECT DISTINCT er.* FROM evidence_records er
          JOIN assertion_evidence ae USING(evidence_id) JOIN relation_assertions a USING(assertion_id)
          WHERE a.status='accepted' AND (a.subject_entity_id=? OR a.source_entity_id=? OR a.target_entity_id=?) LIMIT 40`).all(id, id, id));
      }
    }
    evidence = [...new Map(evidence.map(e => [e.evidence_id, { ...e, payload: parse(e.payload) }])).values()];
    let jobs = [], origin = null;
    if (this.tables.has('preparation_jobs')) {
      jobs = this.db.prepare('SELECT job_type,language,status,attempts,error,updated_at FROM preparation_jobs WHERE subject_entity_id=? ORDER BY updated_at DESC LIMIT 60').all(id);
      if (this.tables.has('job_artifacts')) {
        const record = this.db.prepare(`SELECT a.payload FROM job_artifacts a JOIN preparation_jobs j USING(job_id)
          WHERE j.subject_entity_id=? AND a.stage='accepted-origin-branches' AND a.validation_state='accepted'
          ORDER BY a.created_at DESC LIMIT 1`).get(id);
        if (record) origin = parse(record.payload);
      }
    }
    return { node, record: { ...raw, payload: parse(raw.payload), ...typed }, relationships,
      evidence, evidenceLimit: 80, jobs, jobLimit: 60, origin };
  }

  close() { this.db?.close(); this.db = null; this.graph = null; }
}
