import { forceSimulation, forceLink, forceManyBody, forceX, forceY, forceZ } from 'd3-force-3d';

let simulation, nodes = [], ticks = 0, total = 100, generation = 0, paused = false, timer;
const hash = text => { let h = 2166136261; for (const c of text) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return h >>> 0; };

function publish(done = false) {
  const positions = new Float32Array(nodes.length * 3);
  nodes.forEach((n, i) => { positions[i * 3] = n.x; positions[i * 3 + 1] = n.y; positions[i * 3 + 2] = n.z; });
  self.postMessage({ generation, positions, progress: ticks / total, done }, [positions.buffer]);
}

function step() {
  if (paused || !simulation) return;
  simulation.tick(4); ticks += 4;
  publish(ticks >= total);
  if (ticks < total) timer = setTimeout(step, 0);
}

self.onmessage = ({ data }) => {
  if (data.type === 'pause') { paused = true; clearTimeout(timer); return; }
  if (data.type === 'resume') { if (paused && ticks < total) { paused = false; step(); } return; }
  if (data.type !== 'layout') return;
  clearTimeout(timer); simulation?.stop();
  generation = data.generation; ticks = 0; paused = false;
  total = data.nodes.length > 5000 ? 84 : 124;
  nodes = data.nodes.map(n => ({ ...n }));
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const parent = nodes.map((_, i) => i), sizes = nodes.map(() => 1);
  const find = i => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
  const links = data.links.filter(l => index.has(l.source) && index.has(l.target));
  for (const l of links) {
    let a = find(index.get(l.source)), b = find(index.get(l.target));
    if (a === b) continue;
    if (sizes[a] < sizes[b]) [a, b] = [b, a];
    parent[b] = a; sizes[a] += sizes[b];
  }
  const groups = [...new Set(parent.map((_, i) => find(i)))].sort((a, b) => sizes[b] - sizes[a]);
  const centers = new Map(groups.map((group, rank) => {
    const radius = rank === 0 ? 0 : 160 + 30 * Math.sqrt(rank);
    const theta = rank * 2.399963229728653;
    const z = Math.sin(rank * 1.618) * radius * 0.65;
    return [group, [Math.cos(theta) * radius, Math.sin(theta) * radius * 0.7, z]];
  }));
  nodes.forEach((n, i) => {
    const center = centers.get(find(i)), seed = hash(n.id), theta = seed * 0.000013;
    const radius = 16 + Math.sqrt(sizes[find(i)]) * 3.2;
    const elevation = ((seed % 2001) / 1000 - 1) * radius;
    n.cx = center[0]; n.cy = center[1]; n.cz = center[2];
    n.x = n.cx + Math.cos(theta) * radius; n.y = n.cy + Math.sin(theta) * radius;
    n.z = n.cz + elevation;
  });
  simulation = forceSimulation(nodes, 3).stop()
    .alphaDecay(0.042).velocityDecay(0.35)
    .force('link', forceLink(links).id(n => n.id).distance(22).strength(0.36))
    .force('charge', forceManyBody().strength(-20).theta(1.15).distanceMax(210))
    .force('x', forceX(n => n.cx).strength(0.035))
    .force('y', forceY(n => n.cy).strength(0.035))
    .force('z', forceZ(n => n.cz).strength(0.035));
  publish(); step();
};
