import { spawn } from 'node:child_process';
import { mkdir, rename, writeFile, unlink } from 'node:fs/promises';
import { createWriteStream } from 'node:fs';
import { pipeline } from 'node:stream/promises';
import { createHash, randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source = process.env.LKT_PI_SSH || 'lachlan@192.168.70.131';
const sourceDB = process.env.LKT_PI_DATABASE || '/home/lachlan/LocalKnowledgeTerminal/data/knowledge.sqlite3';
if (!/^[\w.@:\[\]-]+$/.test(source) || source.startsWith('-')) throw new Error('Invalid SSH destination.');
const data = resolve(root, 'data');
await mkdir(data, { recursive: true });
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const file = `knowledge-${stamp}-${randomUUID().slice(0, 8)}.sqlite3`;
const temporary = resolve(data, file + '.partial');
const destination = resolve(data, file);
const script = `import sqlite3,tempfile,pathlib,os,sys,shutil
source=pathlib.Path(${JSON.stringify(sourceDB)})
if shutil.disk_usage(source.parent).free < source.stat().st_size + 128*1024*1024:
    raise RuntimeError('Insufficient temporary snapshot space on Pi')
fd,name=tempfile.mkstemp(prefix='lkt-atlas-',suffix='.sqlite3',dir=source.parent)
os.close(fd)
try:
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=30) as src, sqlite3.connect(name) as dst:
        src.backup(dst,pages=2048,sleep=0.05)
    with open(name,'rb') as snapshot:
        shutil.copyfileobj(snapshot,sys.stdout.buffer,length=1024*1024)
    sys.stdout.buffer.flush()
finally:
    pathlib.Path(name).unlink(missing_ok=True)
`;
const quote = text => "'" + text.replace(/'/g, "'\\''") + "'";
const sshArgs = JSON.parse(process.env.LKT_SSH_ARGS || '[]');
const child = spawn(process.env.LKT_SSH_COMMAND || 'ssh', [...sshArgs, '-o', 'ConnectTimeout=15', source, 'python3 -c ' + quote(script)],
  { stdio: ['inherit', 'pipe', 'inherit'] });
const digest = createHash('sha256');
let bytes = 0;
child.stdout.on('data', chunk => { digest.update(chunk); bytes += chunk.length; });
const completion = new Promise((resolveChild, reject) => {
  child.once('error', reject);
  child.once('close', code => code === 0 ? resolveChild() : reject(new Error(`SSH snapshot exited with ${code}.`)));
});
console.log('Copying a consistent SQLite snapshot. The Pi worker stays running.');
try {
  await Promise.all([completion, pipeline(child.stdout, createWriteStream(temporary, { flags: 'wx' }))]);
  const db = new DatabaseSync(temporary, { readOnly: true });
  try {
    const check = db.prepare('PRAGMA quick_check').get();
    if (Object.values(check)[0] !== 'ok') throw new Error('Snapshot integrity check failed.');
    if (!db.prepare("SELECT name FROM sqlite_master WHERE name='entities'").get()) throw new Error('Not an LKT snapshot.');
  } finally { db.close(); }
  await rename(temporary, destination);
  const manifest = { file, copiedAt: new Date().toISOString(), source, sourceDatabase: sourceDB,
    bytes, sha256: digest.digest('hex') };
  const pending = resolve(data, `snapshot-${randomUUID()}.json.tmp`);
  await writeFile(pending, JSON.stringify(manifest, null, 2) + '\n');
  await rename(pending, resolve(data, 'snapshot.json'));
  console.log(`Snapshot ready: ${file} (${(bytes / 1024 ** 2).toFixed(1)} MiB). Reload the atlas.`);
} catch (error) {
  child.kill(); await unlink(temporary).catch(() => {}); throw error;
}
