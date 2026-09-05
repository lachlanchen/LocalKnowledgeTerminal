import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CATEGORIES, category } from './catalog.mjs';

export class AtlasScene {
  constructor(host, { onSelect, onStats, onError }) {
    this.host = host; this.onSelect = onSelect; this.onStats = onStats;
    this.positions = new Map(); this.visible = { nodes: [], links: [] };
    this.selected = null; this.labels = []; this.dimension = 3;
    this.reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(48, 1, 0.1, 20000);
    this.camera.position.set(560, 310, 950);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 1.6));
    this.renderer.setClearColor(0x08151d, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.25;
    const canvas = this.renderer.domElement;
    canvas.className = 'network-canvas'; canvas.tabIndex = 0;
    canvas.setAttribute('aria-label', '3D knowledge network. Drag to rotate, right-drag to pan, wheel to zoom. Use search for keyboard selection.');
    host.prepend(canvas);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true; this.controls.dampingFactor = 0.075;
    this.controls.minDistance = 8; this.controls.maxDistance = 9000;
    this.controls.autoRotateSpeed = 0.22;
    this.scene.add(new THREE.HemisphereLight(0xdaf8ff, 0x163344, 2));
    const light = new THREE.DirectionalLight(0xffffff, 2.5); light.position.set(400, 600, 900); this.scene.add(light);
    const rim = new THREE.DirectionalLight(0x74d7d0, 2); rim.position.set(-500, -100, -700); this.scene.add(rim);
    this.geometry = new THREE.IcosahedronGeometry(1, 1);
    this.material = new THREE.MeshStandardMaterial({ roughness: 0.4, metalness: 0.08 });
    this.lineMaterial = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.18, depthWrite: false });
    this.activeMaterial = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.92, depthWrite: false });
    this.raycaster = new THREE.Raycaster(); this.pointer = new THREE.Vector2(); this.object = new THREE.Object3D();
    this.labelHost = document.createElement('div'); this.labelHost.className = 'graph-labels'; host.append(this.labelHost);
    this.tooltip = document.createElement('div'); this.tooltip.className = 'node-tooltip'; this.tooltip.hidden = true; host.append(this.tooltip);
    this.halo = new THREE.Mesh(new THREE.RingGeometry(1.1, 1.22, 48), new THREE.MeshBasicMaterial({ color: 0xf0ffbc, side: THREE.DoubleSide, transparent: true, opacity: 0.9, depthTest: false }));
    this.halo.visible = false; this.halo.renderOrder = 5; this.scene.add(this.halo);
    this.resize = new ResizeObserver(() => {
      const width = host.clientWidth, height = host.clientHeight;
      if (!width || !height) return;
      this.camera.aspect = width / height; this.camera.updateProjectionMatrix(); this.renderer.setSize(width, height, false);
    });
    this.resize.observe(host);
    let down = null, lastPick = 0;
    canvas.addEventListener('pointerdown', event => { down = [event.clientX, event.clientY]; this.cameraTarget = null; });
    canvas.addEventListener('pointerup', event => {
      if (down && Math.hypot(event.clientX - down[0], event.clientY - down[1]) < 5 && event.button === 0) {
        const node = this.pick(event); if (node) onSelect(node.id);
      }
      down = null;
    });
    canvas.addEventListener('pointermove', event => {
      if (event.buttons || performance.now() - lastPick < 70) return;
      lastPick = performance.now(); const node = this.pick(event);
      canvas.style.cursor = node ? 'pointer' : 'grab';
      this.tooltip.hidden = !node;
      if (node) {
        this.tooltip.textContent = `${node.label}  /  ${node.language || node.kind || node.type}`;
        this.tooltip.style.left = `${Math.min(event.offsetX + 16, host.clientWidth - 220)}px`;
        this.tooltip.style.top = `${Math.max(10, event.offsetY - 42)}px`;
      }
    });
    canvas.addEventListener('pointerleave', () => { this.tooltip.hidden = true; });
    canvas.addEventListener('webglcontextlost', event => { event.preventDefault(); onError('The graphics context was interrupted. Reload to restore the atlas.'); });
    let frames = 0, since = performance.now(), last = 0;
    const animate = now => {
      this.frame = requestAnimationFrame(animate);
      if (now - last < 1000 / 45 || document.hidden) return;
      last = now;
      if (this.cameraTarget) {
        this.camera.position.lerp(this.cameraTarget.position, this.reducedMotion ? 1 : 0.08);
        this.controls.target.lerp(this.cameraTarget.target, this.reducedMotion ? 1 : 0.08);
        if (this.camera.position.distanceTo(this.cameraTarget.position) < 0.3) this.cameraTarget = null;
      }
      this.controls.update();
      if (this.halo.visible) this.halo.quaternion.copy(this.camera.quaternion);
      this.renderLabels(); this.renderer.render(this.scene, this.camera); frames++;
      if (now - since > 1000) { onStats({ fps: Math.round(frames * 1000 / (now - since)), drawCalls: this.renderer.info.render.calls }); frames = 0; since = now; }
    };
    this.frame = requestAnimationFrame(animate);
  }

  pick(event) {
    if (!this.mesh || !this.visible.nodes.length) return null;
    const box = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set((event.clientX - box.left) / box.width * 2 - 1, -(event.clientY - box.top) / box.height * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hit = this.raycaster.intersectObject(this.mesh, false)[0];
    return hit ? this.visible.nodes[hit.instanceId] : null;
  }

  setGraph(graph) {
    this.graph = graph;
    this.positions.clear();
    graph.nodes.forEach((n, i) => {
      const angle = i * 2.39996, radius = 80 + Math.sqrt(i) * 4;
      this.positions.set(n.id, new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius, Math.sin(i * 0.91) * radius * 0.6));
    });
  }

  setPositions(packed) {
    this.graph.nodes.forEach((n, i) => this.positions.get(n.id).set(packed[i * 3], packed[i * 3 + 1], packed[i * 3 + 2]));
    this.updateGeometry();
  }

  position(id) {
    const p = this.positions.get(id)?.clone() || new THREE.Vector3();
    if (this.dimension === 2) p.z = 0;
    return p;
  }

  radius(node) { return 1.6 + Math.min(4.8, Math.log2(1 + node.degree) * 0.7); }

  setVisible(view) {
    this.visible = view;
    if (this.mesh) { this.scene.remove(this.mesh); this.mesh.dispose(); }
    for (const key of ['lines', 'activeLines', 'arrows']) if (this[key]) {
      this.scene.remove(this[key]); this[key].geometry.dispose();
      if (key === 'arrows') { this[key].material.dispose(); this[key].dispose(); }
    }
    this.mesh = new THREE.InstancedMesh(this.geometry, this.material, Math.max(1, view.nodes.length));
    this.mesh.count = view.nodes.length; this.mesh.frustumCulled = false;
    this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage); this.scene.add(this.mesh);
    const pairs = new Map();
    for (const l of view.links) {
      if (l.source === l.target) continue;
      const key = JSON.stringify([l.source, l.target]);
      if (!pairs.has(key)) pairs.set(key, l);
    }
    this.visualLinks = [...pairs.values()];
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(this.visualLinks.length * 6), 3));
    const colors = new Float32Array(this.visualLinks.length * 6);
    this.visualLinks.forEach((l, i) => {
      const color = new THREE.Color(l.bases.includes('book') ? '#a1bf89' : '#467d8d');
      color.toArray(colors, i * 6); color.toArray(colors, i * 6 + 3);
    });
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    this.lines = new THREE.LineSegments(geometry, this.lineMaterial); this.lines.frustumCulled = false; this.scene.add(this.lines);
    this.updateSelection(); this.updateGeometry();
  }

  setSelected(id) { this.selected = id; this.updateSelection(); this.updateGeometry(); }

  updateSelection() {
    if (!this.mesh) return;
    const neighbors = new Set(this.selected ? [this.selected] : []);
    this.activeLinks = (this.visualLinks || []).filter(l => l.source === this.selected || l.target === this.selected);
    for (const l of this.activeLinks) { neighbors.add(l.source); neighbors.add(l.target); }
    this.visible.nodes.forEach((n, i) => {
      const color = new THREE.Color(CATEGORIES[category(n)].color);
      if (this.selected && !neighbors.has(n.id)) color.multiplyScalar(0.28);
      else if (n.status === 'draft') color.multiplyScalar(0.65);
      else if (['archived', 'rejected'].includes(n.status)) color.multiplyScalar(0.3);
      if (n.id === this.selected) color.set('#f0ffbc');
      this.mesh.setColorAt(i, color);
    });
    if (this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;
    if (this.activeLines) { this.scene.remove(this.activeLines); this.activeLines.geometry.dispose(); }
    if (this.arrows) { this.scene.remove(this.arrows); this.arrows.geometry.dispose(); this.arrows.material.dispose(); this.arrows.dispose(); }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(this.activeLinks.length * 6), 3));
    const colors = new Float32Array(this.activeLinks.length * 6);
    this.activeLinks.forEach((l, i) => {
      const color = new THREE.Color(l.bases.includes('book') ? '#e3fba5' : '#7cded5');
      color.toArray(colors, i * 6); color.toArray(colors, i * 6 + 3);
    });
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    this.activeLines = new THREE.LineSegments(geometry, this.activeMaterial); this.activeLines.frustumCulled = false; this.scene.add(this.activeLines);
    this.arrows = new THREE.InstancedMesh(new THREE.ConeGeometry(0.8, 3.4, 5), new THREE.MeshBasicMaterial({ color: '#d7efcd' }), Math.max(1, this.activeLinks.length));
    this.arrows.count = this.activeLinks.length; this.arrows.frustumCulled = false; this.scene.add(this.arrows);
    this.labelHost.replaceChildren();
    this.labels = this.visible.nodes.filter(n => neighbors.has(n.id))
      .sort((a, b) => Number(b.id === this.selected) - Number(a.id === this.selected) || b.degree - a.degree).slice(0, 15)
      .map(node => {
        const element = document.createElement('div'); element.className = 'graph-label' + (node.id === this.selected ? ' selected' : '');
        const badge = document.createElement('span'); badge.textContent = node.language || node.kind || node.type;
        const label = document.createElement('strong'); label.textContent = node.label;
        element.append(badge, label); element.style.setProperty('--node-color', CATEGORIES[category(node)].color);
        this.labelHost.append(element); return { node, element };
      });
  }

  updateGeometry() {
    if (!this.mesh) return;
    const visibleById = new Map(this.visible.nodes.map(n => [n.id, n]));
    this.visible.nodes.forEach((node, i) => {
      this.object.position.copy(this.position(node.id)); this.object.quaternion.identity();
      this.object.scale.setScalar(this.radius(node) * (node.id === this.selected ? 1.3 : 1));
      this.object.updateMatrix(); this.mesh.setMatrixAt(i, this.object.matrix);
    });
    this.mesh.instanceMatrix.needsUpdate = true; this.mesh.computeBoundingSphere();
    for (const [object, links] of [[this.lines, this.visualLinks], [this.activeLines, this.activeLinks]]) {
      if (!object) continue;
      const attribute = object.geometry.attributes.position;
      links.forEach((l, i) => { this.position(l.source).toArray(attribute.array, i * 6); this.position(l.target).toArray(attribute.array, i * 6 + 3); });
      attribute.needsUpdate = true;
    }
    const up = new THREE.Vector3(0, 1, 0);
    this.activeLinks?.forEach((l, i) => {
      const source = this.position(l.source), target = this.position(l.target);
      const direction = target.clone().sub(source).normalize();
      this.object.position.copy(target).addScaledVector(direction, -(this.radius(visibleById.get(l.target)) + 3));
      this.object.quaternion.setFromUnitVectors(up, direction); this.object.scale.setScalar(1);
      this.object.updateMatrix(); this.arrows.setMatrixAt(i, this.object.matrix);
    });
    if (this.arrows) this.arrows.instanceMatrix.needsUpdate = true;
    const node = visibleById.get(this.selected);
    this.halo.visible = !!node;
    if (node) { this.halo.position.copy(this.position(node.id)); this.halo.scale.setScalar(this.radius(node) * 1.7); }
  }

  renderLabels() {
    for (const { node, element } of this.labels) {
      const position = this.position(node.id).project(this.camera);
      const visible = position.z >= -1 && position.z <= 1 && Math.abs(position.x) < 0.97 && Math.abs(position.y) < 0.95;
      element.hidden = !visible;
      if (visible) {
        element.style.left = `${(position.x + 1) / 2 * this.host.clientWidth}px`;
        element.style.top = `${(-position.y + 1) / 2 * this.host.clientHeight - 20}px`;
      }
    }
  }

  fit(ids = null) {
    const nodes = ids ? this.visible.nodes.filter(n => ids.has(n.id)) : this.visible.nodes;
    if (!nodes.length) return;
    const box = new THREE.Box3(); nodes.forEach(n => box.expandByPoint(this.position(n.id)));
    const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const spread = Math.max(size.y, size.x / Math.max(0.35, this.camera.aspect), size.z * 0.7, 50);
    const distance = spread / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2))) * 1.3;
    const direction = this.dimension === 2 ? new THREE.Vector3(0, 0, 1) : new THREE.Vector3(0.35, 0.15, 1).normalize();
    this.cameraTarget = { target: center, position: center.clone().addScaledVector(direction, distance) };
  }

  focus(id) {
    const center = this.position(id);
    const direction = this.camera.position.clone().sub(this.controls.target).normalize();
    if (this.dimension === 2) direction.set(0, 0, 1);
    this.cameraTarget = { target: center, position: center.clone().addScaledVector(direction, 150) };
  }

  setDimension(value) {
    this.dimension = value; this.controls.enableRotate = value === 3;
    this.controls.autoRotate = false; this.updateGeometry(); this.fit();
  }
}
