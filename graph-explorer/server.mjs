import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve, dirname, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';
import { SnapshotStore } from './lib/snapshot.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.png': 'image/png' };

export function createAtlasServer({ store, dist = resolve(root, 'dist') }) {
  let cachedGraph = null, cachedJSON = null, cachedGzip = null;
  return createServer(async (request, response) => {
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; worker-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'");
    const send = (status, value) => {
      response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end(JSON.stringify(value));
    };
    let url;
    try { url = new URL(request.url, `http://${request.headers.host || 'localhost'}`); }
    catch { send(400, { error: 'Invalid URL.' }); return; }
    if (!['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) { send(403, { error: 'Loopback access only.' }); return; }
    if (!['GET', 'HEAD'].includes(request.method)) { send(405, { error: 'This explorer is read-only.' }); return; }
    if (request.headers.origin && request.headers.origin !== url.origin) { send(403, { error: 'Cross-origin access is disabled.' }); return; }
    try {
      if (url.pathname === '/api/network') {
        const graph = store.network();
        if (graph !== cachedGraph) {
          cachedGraph = graph; cachedJSON = Buffer.from(JSON.stringify(graph)); cachedGzip = gzipSync(cachedJSON);
        }
        const compressed = /\bgzip\b/.test(request.headers['accept-encoding'] || '');
        response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store',
          Vary: 'Accept-Encoding', ...(compressed ? { 'Content-Encoding': 'gzip' } : {}) });
        response.end(request.method === 'HEAD' ? undefined : compressed ? cachedGzip : cachedJSON);
        return;
      }
      if (url.pathname === '/api/health') { send(200, { status: 'ready', readOnly: true, snapshot: store.network().meta }); return; }
      if (url.pathname.startsWith('/api/node/')) {
        const detail = store.detail(decodeURIComponent(url.pathname.slice('/api/node/'.length)));
        send(detail ? 200 : 404, detail || { error: 'Node not found in this snapshot.' }); return;
      }
      if (url.pathname.startsWith('/api/')) { send(404, { error: 'Unknown endpoint.' }); return; }
      const decoded = decodeURIComponent(url.pathname);
      const path = resolve(dist, `.${decoded === '/' ? '/index.html' : decoded}`);
      if (!path.startsWith(dist + sep) || !mime[extname(path)]) { send(404, { error: 'Not found.' }); return; }
      await stat(path);
      response.writeHead(200, { 'Content-Type': mime[extname(path)],
        'Cache-Control': extname(path) === '.html' ? 'no-cache' : 'public, max-age=3600' });
      response.end(request.method === 'HEAD' ? undefined : await readFile(path));
    } catch (error) {
      if (response.headersSent) { response.end(); return; }
      const api = url.pathname.startsWith('/api/');
      send(api ? 503 : 404, { error: api ? 'Local snapshot unavailable. Run npm run sync, then reload.' : 'Build the app with npm run build.',
        detail: process.env.LKT_ATLAS_DEBUG === '1' ? error.message : undefined });
    }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.LKT_ATLAS_PORT || 8091);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid LKT_ATLAS_PORT.');
  const store = new SnapshotStore({ manifest: resolve(root, 'data/snapshot.json'), database: process.env.LKT_GRAPH_DB });
  const server = createAtlasServer({ store });
  server.listen(port, '127.0.0.1', () => console.log(`Lexicon Atlas: http://127.0.0.1:${port}/ (read-only, local snapshot)`));
  const close = () => server.close(() => { store.close(); process.exit(0); });
  process.on('SIGINT', close); process.on('SIGTERM', close);
}
